"""Tuning silence used while an on-demand station starts."""

from __future__ import annotations

import asyncio
import logging

from .config import Settings

logger = logging.getLogger(__name__)

SILENCE_SECONDS = 2.0


class SilenceCache:
    """Generate and cache a short AAC-LC silence clip.

    On-demand streams send a little silence while yt-dlp/FFmpeg tune in, so
    players establish playback instead of timing out waiting for headers.
    HE-AAC stations are always-on, so this clip only needs to match AAC-LC.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._data: bytes | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> bytes:
        if self._data is not None:
            return self._data

        async with self._lock:
            if self._data is not None:
                return self._data

            command = [
                self.settings.ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=stereo",
                "-t",
                f"{SILENCE_SECONDS:g}",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-f",
                "adts",
                "pipe:1",
            ]
            data = b""
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=10
                    )
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    raise RuntimeError("FFmpeg timed out generating silence") from None

                if process.returncode != 0:
                    detail = stderr.decode("utf-8", "replace").strip()
                    raise RuntimeError(
                        f"FFmpeg silence generator exited with code "
                        f"{process.returncode}: {detail[:300]}"
                    )
                data = stdout
            except Exception as exc:  # noqa: BLE001 - fall back to silent wait
                logger.warning("Could not generate tuning silence: %s", exc)

            self._data = data
            return data
