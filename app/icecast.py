"""Icecast lifecycle, configuration and statistics helpers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
from string import Template
from typing import Any
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

_ACCESS_LOG_RE = re.compile(r' - - \[[^\]]*\] "')

CONFIG_TEMPLATE = Template(
    """<?xml version="1.0" encoding="UTF-8"?>
<icecast>
    <location>Internet</location>
    <admin>admin@localhost</admin>

    <limits>
        <clients>500</clients>
        <sources>64</sources>
        <queue-size>1048576</queue-size>
        <client-timeout>30</client-timeout>
        <header-timeout>15</header-timeout>
        <source-timeout>30</source-timeout>
        <burst-on-connect>1</burst-on-connect>
        <burst-size>65535</burst-size>
    </limits>

    <authentication>
        <source-password>$source_password</source-password>
        <relay-password>$relay_password</relay-password>
        <admin-user>admin</admin-user>
        <admin-password>$admin_password</admin-password>
    </authentication>

    <hostname>localhost</hostname>

    <listen-socket>
        <port>$port</port>
        <bind-address>127.0.0.1</bind-address>
    </listen-socket>

    <paths>
        <logdir>$logdir</logdir>
        <pidfile>$pidfile</pidfile>
        <webroot>$webroot</webroot>
        <adminroot>$adminroot</adminroot>
    </paths>

    <logging>
        <accesslog>-</accesslog>
        <errorlog>-</errorlog>
        <loglevel>2</loglevel>
        <logsize>10000</logsize>
        <logarchive>0</logarchive>
    </logging>

    <fileserve>1</fileserve>
    <security>
        <chroot>0</chroot>
    </security>
</icecast>
"""
)


def parse_icecast_stats(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return a mapping of mount path -> source stats from status-json.xsl."""

    icestats = payload.get("icestats")
    if not isinstance(icestats, dict):
        return {}

    raw_source = icestats.get("source")
    if isinstance(raw_source, dict):
        sources = [raw_source]
    elif isinstance(raw_source, list):
        sources = [item for item in raw_source if isinstance(item, dict)]
    else:
        return {}

    result: dict[str, dict[str, Any]] = {}
    for source in sources:
        mount = source.get("mount")
        if not isinstance(mount, str) or not mount:
            listen_url = source.get("listenurl")
            if isinstance(listen_url, str) and listen_url:
                mount = urlsplit(listen_url).path
        if mount:
            result[mount] = source
    return result


async def fetch_icecast_stats(
    client: httpx.AsyncClient, base_url: str
) -> dict[str, dict[str, Any]]:
    response = await client.get(f"{base_url}/status-json.xsl")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Unexpected Icecast status response")
    return parse_icecast_stats(payload)


class IcecastServer:
    """Runs and supervises the bundled Icecast process."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.ready = False
        self.error: str | None = "Icecast has not started yet"
        self._proc: asyncio.subprocess.Process | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None
        self._restart_delay = 5.0

    @property
    def url(self) -> str:
        return self.settings.icecast_url

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(2.5))

        if self.settings.icecast_managed:
            self._write_config()
            await self._spawn()
        else:
            logger.info("Icecast is managed externally at %s", self.url)

        await self.wait_until_ready(timeout=15)
        self._monitor_task = asyncio.create_task(
            self._monitor(), name="icecast-monitor"
        )

    async def wait_until_ready(self, timeout: float = 15.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if await self._probe():
                self.ready = True
                self.error = None
                logger.info("Icecast is ready at %s", self.url)
                return True
            await asyncio.sleep(0.3)

        self.ready = False
        self.error = self.error or "Icecast did not become ready in time"
        logger.error("Icecast did not become ready: %s", self.error)
        return False

    async def _probe(self) -> bool:
        if not self._client:
            return False
        try:
            await fetch_icecast_stats(self._client, self.url)
            return True
        except (httpx.HTTPError, ValueError) as exc:
            self.error = f"Icecast is not responding: {exc}"
            return False

    def _write_config(self) -> None:
        settings = self.settings
        config = CONFIG_TEMPLATE.substitute(
            source_password=escape(settings.icecast_source_password),
            relay_password=escape(settings.icecast_source_password),
            admin_password=escape(settings.icecast_admin_password),
            port=str(settings.icecast_port),
            logdir=escape(str(settings.data_dir / "logs")),
            pidfile=escape(str(settings.icecast_pid_path)),
            webroot=escape(settings.icecast_webroot),
            adminroot=escape(settings.icecast_adminroot),
        )
        settings.icecast_config_path.write_text(config, encoding="utf-8")
        try:
            settings.icecast_config_path.chmod(0o600)
        except OSError:
            pass

    async def _spawn(self) -> bool:
        if self._proc is not None and self._proc.returncode is None:
            return True

        with contextlib.suppress(FileNotFoundError):
            self.settings.icecast_pid_path.unlink()

        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.settings.icecast_binary,
                "-c",
                str(self.settings.icecast_config_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            self._proc = None
            self.error = f"Could not start Icecast: {exc}"
            logger.error("Could not start Icecast: %s", exc)
            return False

        assert self._proc.stderr is not None
        self._drain_task = asyncio.create_task(
            self._drain_logs(self._proc.stderr), name="icecast-log"
        )
        logger.info("Started Icecast process %s", self._proc.pid)
        return True

    async def _drain_logs(self, stream: asyncio.StreamReader) -> None:
        while True:
            try:
                raw = await stream.readline()
            except asyncio.CancelledError:
                raise
            except Exception:
                break
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line or "/status-json.xsl" in line:
                continue
            # Access log lines and expected on-demand 404 probes add noise.
            if _ACCESS_LOG_RE.search(line):
                continue
            if (
                "fserve_client_create req for file" in line
                and "/stream/" in line
            ):
                continue
            logger.info("icecast: %s", line)

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(4)
            if not self.settings.icecast_managed:
                self.ready = await self._probe()
                continue

            proc = self._proc
            if proc is not None and proc.returncode is None:
                was_ready = self.ready
                self.ready = await self._probe()
                if self.ready:
                    if not was_ready:
                        logger.info("Icecast is responding again")
                    self.error = None
                elif was_ready:
                    logger.warning("%s", self.error)
                continue

            if proc is not None:
                self.ready = False
                self.error = f"Icecast exited unexpectedly (code {proc.returncode})"
                logger.error(
                    "%s; restarting in %.0fs", self.error, self._restart_delay
                )
            elif self.ready:
                # A process outside our control is serving the status page.
                self.ready = await self._probe()
                continue

            await asyncio.sleep(self._restart_delay)
            self._restart_delay = min(60.0, self._restart_delay * 2)
            self._write_config()
            if await self._spawn():
                await self.wait_until_ready(timeout=10)
                if self.ready:
                    self._restart_delay = 5.0

    async def stop(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
            self._monitor_task = None

        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=3)

        self._proc = None
        if self._drain_task:
            self._drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain_task
            self._drain_task = None

        if self._client:
            await self._client.aclose()
            self._client = None
        self.ready = False
