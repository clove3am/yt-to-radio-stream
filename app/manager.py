"""Manage one yt-dlp -> ffmpeg -> Icecast pipeline per station."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from .config import Settings
from .db import Database
from .icecast import IcecastServer, fetch_icecast_stats
from .models import Station
from .utils import redact_secrets, strip_ansi, truncate

logger = logging.getLogger(__name__)

MAX_BACKOFF = 60.0
BASE_BACKOFF = 5.0


@dataclass
class Runtime:
    station: Station
    task: asyncio.Task[None] | None = None
    phase: str = "stopped"  # stopped | pipeline | backoff
    stop_requested: bool = False

    ytdlp: asyncio.subprocess.Process | None = None
    ffmpeg: asyncio.subprocess.Process | None = None
    pump_task: asyncio.Task[None] | None = None
    stderr_tasks: list[asyncio.Task[None]] = field(default_factory=list)

    started_at: float | None = None
    retry_at: float | None = None
    last_error: str | None = None
    listeners: int = 0
    icecast_connected: bool = False
    last_connected_at: float | None = None
    restart_requested: bool = False
    attempt: int = 0
    demand: int = 0
    idle_stop_at: float | None = None
    idle_task: asyncio.Task[None] | None = None

    attempt_errors: deque[str] = field(default_factory=lambda: deque(maxlen=40))


class ListenerLease:
    """Idempotent reference to one active stream connection."""

    def __init__(
        self,
        manager: "StationManager",
        station_id: int,
        client_key: str,
        runtime: "Runtime | None",
    ):
        self.manager = manager
        self.station_id = station_id
        self.client_key = client_key
        self.runtime = runtime
        self._released = False
        self._lock = asyncio.Lock()

    async def release(self) -> None:
        async with self._lock:
            if self._released:
                return
            await self.manager.release_listener(
                self.station_id, self.client_key, self.runtime
            )
            self._released = True


class StationManager:
    def __init__(self, settings: Settings, db: Database, icecast: IcecastServer):
        self.settings = settings
        self.db = db
        self.icecast = icecast
        self._runtimes: dict[int, Runtime] = {}
        self._poll_task: asyncio.Task[None] | None = None
        self._stats_client: httpx.AsyncClient | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._station_logs: dict[int, deque[str]] = {}
        self._connections: dict[int, int] = {}
        self._clients: dict[int, dict[str, int]] = {}
        self.icecast_ok = icecast.ready
        self.icecast_error: str | None = (
            None if icecast.ready else (icecast.error or "Starting stream engine…")
        )

    async def startup(self) -> None:
        cookies_file = self.settings.cookies_file
        if cookies_file is not None and not cookies_file.is_file():
            logger.warning(
                "YTDLP_COOKIES_FILE is set to %s but the file does not exist; "
                "continuing without cookies",
                cookies_file,
            )

        self._stats_client = httpx.AsyncClient(timeout=httpx.Timeout(3.0))
        self._poll_task = asyncio.create_task(
            self._poll_icecast(), name="icecast-stats"
        )
        for station in self.db.list_stations():
            if not station.desired_state:
                continue
            if station.on_demand:
                self._append_log_for(
                    station,
                    "Armed for on-demand streaming; waiting for a listener.",
                )
                continue
            await self.start(station)
            await asyncio.sleep(0.25)

    async def shutdown(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None

        station_ids = list(self._runtimes)
        if station_ids:
            await asyncio.gather(
                *(self.stop(station_id) for station_id in station_ids),
                return_exceptions=True,
            )

        if self._stats_client:
            await self._stats_client.aclose()
            self._stats_client = None

    @staticmethod
    def _runtime_is_active(runtime: Runtime | None) -> bool:
        return bool(
            runtime
            and runtime.task
            and not runtime.task.done()
            and not runtime.stop_requested
            and runtime.phase != "stopped"
        )

    async def start(self, station: Station) -> None:
        # On-demand stations are started by their first listener, never by the
        # boot/manual-start path.
        if station.on_demand:
            return

        async with self._lifecycle_lock:
            await self._start_locked(station, demand=0)

    async def _start_locked(self, station: Station, demand: int = 0) -> Runtime:
        runtime = self._runtimes.get(station.id)
        if self._runtime_is_active(runtime):
            runtime.demand = max(runtime.demand, demand)
            return runtime

        if runtime:
            runtime.stop_requested = True
            self._cancel_idle_locked(runtime)
            if runtime.task and not runtime.task.done():
                runtime.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runtime.task
            await self._terminate(runtime)

        runtime = Runtime(
            station=station,
            phase="pipeline",
            started_at=time.time(),
            demand=demand,
        )
        self._runtimes[station.id] = runtime
        runtime.task = asyncio.create_task(
            self._supervise(runtime), name=f"station-{station.slug}"
        )
        return runtime

    async def acquire_listener(
        self, station_id: int, client_key: str
    ) -> ListenerLease | None:
        """Register a stream connection and wake an on-demand station."""

        async with self._lifecycle_lock:
            station = self.db.get_station(station_id)
            if station is None or not station.desired_state:
                return None

            runtime = self._runtimes.get(station_id)
            if station.on_demand:
                if station.codec == "aac_he" or (
                    runtime and runtime.stop_requested
                ):
                    return None
                if self._runtime_is_active(runtime):
                    runtime.demand += 1
                else:
                    runtime = await self._start_locked(station, demand=1)
                self._cancel_idle_locked(runtime)
            elif not self._runtime_is_active(runtime):
                runtime = await self._start_locked(station, demand=0)

            self._register_listener_locked(station_id, client_key)
            return ListenerLease(self, station_id, client_key, runtime)

    async def release_listener(
        self,
        station_id: int,
        client_key: str,
        runtime: Runtime | None,
    ) -> None:
        async with self._lifecycle_lock:
            self._unregister_listener_locked(station_id, client_key)

            if runtime is None:
                return
            if self._runtimes.get(station_id) is not runtime:
                return
            if not runtime.station.on_demand:
                return
            runtime.demand = max(0, runtime.demand - 1)
            if runtime.demand == 0 and not runtime.stop_requested:
                self._schedule_idle_stop_locked(runtime)

    def _register_listener_locked(self, station_id: int, client_key: str) -> None:
        self._connections[station_id] = self._connections.get(station_id, 0) + 1
        clients = self._clients.setdefault(station_id, {})
        clients[client_key] = clients.get(client_key, 0) + 1

    def _unregister_listener_locked(
        self, station_id: int, client_key: str
    ) -> None:
        connections = self._connections.get(station_id, 0) - 1
        if connections > 0:
            self._connections[station_id] = connections
        else:
            self._connections.pop(station_id, None)

        clients = self._clients.get(station_id)
        if not clients:
            return
        count = clients.get(client_key, 0) - 1
        if count > 0:
            clients[client_key] = count
        else:
            clients.pop(client_key, None)
        if not clients:
            self._clients.pop(station_id, None)

    def _listener_stats(self, station_id: int) -> tuple[int, int]:
        unique_clients = len(self._clients.get(station_id, {}))
        connections = self._connections.get(station_id, 0)
        return unique_clients, connections

    def listener_allowed(self, runtime: Runtime) -> bool:
        return bool(
            self._runtimes.get(runtime.station.id) is runtime
            and runtime.station.on_demand
            and self._runtime_is_active(runtime)
        )

    def _cancel_idle_locked(self, runtime: Runtime) -> None:
        task = runtime.idle_task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        runtime.idle_task = None
        runtime.idle_stop_at = None

    def _schedule_idle_stop_locked(self, runtime: Runtime) -> None:
        self._cancel_idle_locked(runtime)
        deadline = time.time() + self.settings.on_demand_idle_seconds
        runtime.idle_stop_at = deadline
        runtime.idle_task = asyncio.create_task(
            self._idle_stop(runtime.station.id, deadline),
            name=f"idle-stop-{runtime.station.slug}",
        )

    async def _idle_stop(self, station_id: int, deadline: float) -> None:
        try:
            delay = max(0.0, deadline - time.time())
            if delay:
                await asyncio.sleep(delay)

            async with self._lifecycle_lock:
                runtime = self._runtimes.get(station_id)
                if runtime is None or not runtime.station.on_demand:
                    return
                if runtime.demand > 0 or runtime.stop_requested:
                    if runtime.idle_task is asyncio.current_task():
                        runtime.idle_task = None
                        runtime.idle_stop_at = None
                    return
                runtime.idle_task = None
                runtime.idle_stop_at = None
                self._append_log(
                    runtime,
                    "No listeners left; stopping on-demand stream.",
                )
                await self._stop_locked(station_id)
        except asyncio.CancelledError:
            pass

    async def stop(self, station_id: int) -> None:
        async with self._lifecycle_lock:
            await self._stop_locked(station_id)

    async def _stop_locked(self, station_id: int) -> None:
        runtime = self._runtimes.get(station_id)
        if not runtime:
            return

        self._cancel_idle_locked(runtime)
        runtime.stop_requested = True
        runtime.phase = "stopped"
        runtime.retry_at = None
        runtime.last_error = None
        runtime.listeners = 0
        runtime.icecast_connected = False

        if runtime.task:
            runtime.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime.task
        await self._terminate(runtime)
        self._runtimes.pop(station_id, None)
        logger.info("Stopped station %s", runtime.station.slug)

    async def restart(self, station: Station) -> None:
        await self.stop(station.id)
        if not station.on_demand:
            await self.start(station)

    def is_active(self, station_id: int) -> bool:
        return self._runtime_is_active(self._runtimes.get(station_id))

    def get_logs(self, station_id: int) -> list[str]:
        return list(self._station_logs.get(station_id, []))

    def clear_logs(self, station_id: int) -> None:
        self._station_logs.pop(station_id, None)

    def view(self, station: Station) -> dict[str, object]:
        runtime = self._runtimes.get(station.id)
        status = "stopped"
        listeners, connections = self._listener_stats(station.id)
        uptime = 0
        retry_in: int | None = None
        last_error: str | None = None
        idle_stop_in: int | None = None

        if station.on_demand and station.desired_state:
            status = "standby"

        if runtime is not None:
            pipeline_active = self._runtime_is_active(runtime)

            if station.on_demand:
                if not station.desired_state:
                    status = "stopped"
                elif not pipeline_active:
                    status = "standby"
                elif runtime.phase == "backoff":
                    status = "retrying"
                    if runtime.retry_at:
                        retry_in = max(
                            0, int(round(runtime.retry_at - time.time()))
                        )
                else:
                    status = "live" if runtime.icecast_connected else "starting"
                    if runtime.started_at:
                        uptime = max(0, int(time.time() - runtime.started_at))

                if (
                    pipeline_active
                    and runtime.demand == 0
                    and runtime.idle_stop_at
                ):
                    idle_stop_in = max(
                        0, int(round(runtime.idle_stop_at - time.time()))
                    )
            else:
                if runtime.phase == "pipeline":
                    status = "live" if runtime.icecast_connected else "starting"
                    if runtime.started_at:
                        uptime = max(0, int(time.time() - runtime.started_at))
                elif runtime.phase == "backoff":
                    status = "retrying"
                    if runtime.retry_at:
                        retry_in = max(
                            0, int(round(runtime.retry_at - time.time()))
                        )

                if not pipeline_active:
                    status = "stopped"

            if status in {"starting", "retrying"}:
                last_error = runtime.last_error
            elif status == "live":
                last_error = None

        return {
            "id": station.id,
            "name": station.name,
            "url": station.url,
            "slug": station.slug,
            "on_demand": station.on_demand,
            "stream_path": station.stream_path,
            "desired_state": station.desired_state,
            "created_at": station.created_at,
            "codec": station.codec,
            "status": status,
            "listeners": listeners,
            "connections": connections,
            "uptime_seconds": uptime,
            "retry_in_seconds": retry_in,
            "idle_stop_in_seconds": idle_stop_in,
            "last_error": last_error,
        }

    async def _supervise(self, runtime: Runtime) -> None:
        try:
            while not runtime.stop_requested:
                runtime.phase = "pipeline"
                runtime.attempt += 1
                runtime.started_at = time.time()
                runtime.retry_at = None
                runtime.last_error = None
                runtime.icecast_connected = False
                runtime.listeners = 0
                runtime.last_connected_at = None
                runtime.restart_requested = False
                runtime.attempt_errors.clear()
                self._append_log(
                    runtime,
                    f"Tuning in (attempt {runtime.attempt})…",
                )

                pipeline_started = time.monotonic()
                failure: str | None = None
                try:
                    failure = await self._run_pipeline(runtime)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - surfaced to the UI
                    logger.exception(
                        "Station %s pipeline crashed", runtime.station.slug
                    )
                    failure = f"{type(exc).__name__}: {exc}"
                finally:
                    await self._terminate(runtime)

                if runtime.stop_requested:
                    break

                if runtime.station.on_demand and runtime.demand == 0:
                    runtime.last_error = None
                    self._append_log(
                        runtime,
                        "No listeners; not reconnecting on-demand stream.",
                    )
                    break

                if time.monotonic() - pipeline_started >= 45:
                    runtime.attempt = 0

                runtime.last_error = failure or "The stream ended."
                runtime.phase = "backoff"
                delay = min(
                    MAX_BACKOFF,
                    BASE_BACKOFF * (2 ** max(0, min(runtime.attempt - 1, 4))),
                )
                runtime.retry_at = time.time() + delay
                self._append_log(
                    runtime,
                    f"{runtime.last_error} Reconnecting in {delay:.0f}s.",
                )
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        finally:
            runtime.phase = "stopped"
            runtime.icecast_connected = False
            runtime.listeners = 0
            await self._terminate(runtime)

    async def _run_pipeline(self, runtime: Runtime) -> str | None:
        station = runtime.station
        settings = self.settings
        codec = station.codec if station.codec in {"aac", "aac_he"} else "aac"

        if codec == "aac_he":
            format_selector = "worstaudio[ext=mp4]/worstaudio[ext=m4a]"
            content_type = "audio/aacp"
        else:
            format_selector = "bestaudio[ext=mp4]/bestaudio[ext=m4a]"
            content_type = "audio/aac"

        ytdlp_command = [
            settings.ytdlp_binary,
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--quiet",
            "--no-cache-dir",
            "--socket-timeout",
            "20",
            "-f",
            format_selector,
        ]
        if settings.cookies_file and settings.cookies_file.is_file():
            ytdlp_command += ["--cookies", str(settings.cookies_file)]
        ytdlp_command += settings.ytdlp_extra_args
        ytdlp_command += ["-o", "-", station.url]

        ffmpeg_command = [
            settings.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-re",
            "-i",
            "pipe:0",
            "-vn",
        ]

        ffmpeg_command += [
            "-c:a",
            "copy",
            "-flush_packets",
            "1",
            "-f",
            "adts",
            "-content_type",
            content_type,
        ]

        ffmpeg_command += [
            "-ice_name",
            station.name,
            "-ice_genre",
            "Live",
            "-ice_description",
            "YouTube Live radio",
            (
                "icecast://source:"
                f"{quote(settings.icecast_source_password, safe='')}"
                f"@{settings.icecast_host}:{settings.icecast_port}"
                f"{station.stream_path}"
            ),
        ]

        try:
            ytdlp = await asyncio.create_subprocess_exec(
                *ytdlp_command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "yt-dlp executable was not found in the container."
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"Could not start yt-dlp: {exc}") from exc

        runtime.ytdlp = ytdlp
        self._append_log(runtime, "Resolving the YouTube live stream…")

        try:
            ffmpeg = await asyncio.create_subprocess_exec(
                *ffmpeg_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            await self._terminate_process(ytdlp)
            raise RuntimeError(
                "ffmpeg executable was not found in the container."
            ) from exc
        except OSError as exc:
            await self._terminate_process(ytdlp)
            raise RuntimeError(f"Could not start ffmpeg: {exc}") from exc

        runtime.ffmpeg = ffmpeg
        assert ytdlp.stdout is not None
        assert ytdlp.stderr is not None
        assert ffmpeg.stdin is not None
        assert ffmpeg.stderr is not None

        runtime.stderr_tasks = [
            asyncio.create_task(
                self._drain_stderr(runtime, ytdlp.stderr, "yt-dlp"),
                name=f"ytdlp-stderr-{station.slug}",
            ),
            asyncio.create_task(
                self._drain_stderr(runtime, ffmpeg.stderr, "ffmpeg"),
                name=f"ffmpeg-stderr-{station.slug}",
            ),
        ]
        runtime.pump_task = asyncio.create_task(
            self._pump(ytdlp.stdout, ffmpeg.stdin),
            name=f"pipeline-{station.slug}",
        )
        codec_label = {
            "aac": "AAC (copy)",
            "aac_he": "HE-AAC (copy)",
        }[codec]
        self._append_log(
            runtime,
            f"Streaming {codec_label} to {station.stream_path}",
        )

        wait_ytdlp = asyncio.create_task(ytdlp.wait())
        wait_ffmpeg = asyncio.create_task(ffmpeg.wait())
        try:
            done, _ = await asyncio.wait(
                {wait_ytdlp, wait_ffmpeg},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wait_ffmpeg in done and ytdlp.returncode is None:
                await self._terminate_process(ytdlp)
            elif wait_ytdlp in done and ffmpeg.returncode is None:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(wait_ffmpeg), timeout=12
                    )
                except asyncio.TimeoutError:
                    await self._terminate_process(ffmpeg)
        finally:
            for task in (wait_ytdlp, wait_ffmpeg):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                wait_ytdlp, wait_ffmpeg, return_exceptions=True
            )

        if runtime.stderr_tasks:
            await asyncio.wait(runtime.stderr_tasks, timeout=1.0)

        if runtime.stop_requested:
            return None

        ytdlp_code = ytdlp.returncode
        ffmpeg_code = ffmpeg.returncode
        return self._error_summary(runtime, ytdlp_code, ffmpeg_code)

    async def _pump(
        self, source: asyncio.StreamReader, destination: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                chunk = await source.read(64 * 1024)
                if not chunk:
                    break
                destination.write(chunk)
                await destination.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("Pipeline pump stopped: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                destination.close()
            with contextlib.suppress(Exception):
                await destination.wait_closed()

    async def _drain_stderr(
        self,
        runtime: Runtime,
        stream: asyncio.StreamReader,
        label: str,
    ) -> None:
        while True:
            try:
                raw = await stream.readline()
            except asyncio.CancelledError:
                raise
            except Exception:
                break
            if not raw:
                break
            line = redact_secrets(strip_ansi(raw.decode("utf-8", "replace"))).strip()
            if not line:
                continue
            line = line[:1000]
            runtime.attempt_errors.append(line)
            self._append_log(runtime, f"[{label}] {line}")

    def _error_summary(
        self,
        runtime: Runtime,
        ytdlp_code: int | None,
        ffmpeg_code: int | None,
    ) -> str:
        lines = [
            redact_secrets(strip_ansi(line))
            for line in runtime.attempt_errors
            if line.strip()
        ]
        interesting = [
            line
            for line in reversed(lines)
            if any(
                keyword in line.lower()
                for keyword in (
                    "error",
                    "failed",
                    "unavailable",
                    "forbidden",
                    "refused",
                    "not found",
                    "timed out",
                )
            )
            and "last message repeated" not in line.lower()
        ]
        if interesting:
            return truncate(interesting[0])
        if lines:
            return truncate(lines[-1])
        if ytdlp_code is not None and ffmpeg_code is not None:
            return (
                "Stream ended "
                f"(yt-dlp exit {ytdlp_code}, ffmpeg exit {ffmpeg_code})."
            )
        return "Stream ended unexpectedly."

    async def _terminate_process(
        self, proc: asyncio.subprocess.Process | None
    ) -> None:
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2)

    async def _terminate(self, runtime: Runtime) -> None:
        processes = [p for p in (runtime.ytdlp, runtime.ffmpeg) if p is not None]
        for proc in processes:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)

        if processes:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(proc.wait() for proc in processes),
                        return_exceptions=True,
                    ),
                    timeout=4.0,
                )
            except asyncio.TimeoutError:
                for proc in processes:
                    if proc.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(proc.pid, signal.SIGKILL)
                await asyncio.gather(
                    *(proc.wait() for proc in processes),
                    return_exceptions=True,
                )

        if runtime.pump_task:
            if not runtime.pump_task.done():
                runtime.pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime.pump_task
            runtime.pump_task = None

        for task in runtime.stderr_tasks:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        runtime.stderr_tasks = []

        runtime.ytdlp = None
        runtime.ffmpeg = None

    async def _poll_icecast(self) -> None:
        assert self._stats_client is not None
        while True:
            try:
                stats = await fetch_icecast_stats(
                    self._stats_client, self.icecast.url
                )
                self.icecast_ok = True
                self.icecast_error = None
            except Exception as exc:  # noqa: BLE001 - keep the poller alive
                stats = {}
                self.icecast_ok = False
                self.icecast_error = f"Stream engine unavailable: {exc}"

            now = time.time()
            for runtime in self._runtimes.values():
                if runtime.phase != "pipeline":
                    continue
                source = stats.get(runtime.station.stream_path)
                if source is None:
                    if runtime.icecast_connected:
                        self._append_log(
                            runtime, "Lost the Icecast source; reconnecting…"
                        )
                    runtime.icecast_connected = False
                    runtime.listeners = 0

                    # Recover from a wedged pipeline (for example, a silent
                    # YouTube connection) instead of waiting forever.
                    reference = runtime.last_connected_at or runtime.started_at
                    limit = 30 if runtime.last_connected_at else 90
                    if (
                        self.icecast_ok
                        and reference
                        and now - reference > limit
                        and not runtime.restart_requested
                        and (not runtime.station.on_demand or runtime.demand > 0)
                    ):
                        runtime.restart_requested = True
                        self._append_log(
                            runtime,
                            "No audio reached Icecast; restarting the pipeline.",
                        )
                        asyncio.create_task(
                            self._terminate(runtime),
                            name=f"watchdog-{runtime.station.slug}",
                        )
                    continue

                if not runtime.icecast_connected:
                    self._append_log(runtime, "Live on Icecast.")
                runtime.icecast_connected = True
                runtime.last_connected_at = now
                runtime.last_error = None
                try:
                    runtime.listeners = int(source.get("listeners") or 0)
                except (TypeError, ValueError):
                    runtime.listeners = 0

            await asyncio.sleep(3)

    def _append_log(self, runtime: Runtime, message: str) -> None:
        self._append_log_for(runtime.station, message)

    def _append_log_for(self, station: Station, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self._station_logs.setdefault(
            station.id, deque(maxlen=200)
        ).append(f"{timestamp}  {message}")
        logger.info("[%s] %s", station.slug, message)
