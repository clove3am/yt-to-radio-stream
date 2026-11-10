"""Tuning silence used while an on-demand station starts."""

from __future__ import annotations

import asyncio
import logging

from .config import Settings

logger = logging.getLogger(__name__)

SILENCE_SECONDS = 2.0


class SilenceCache:
    """Generate and cache a short silence clip per codec.

    On-demand streams send a little silence while yt-dlp/FFmpeg tune in, so
    players establish playback instead of timing out waiting for headers.
    HE-AAC stations are always-on, so this clip only needs AAC-LC and MP3.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def get(self, codec: str) -> bytes:
        cached = self._cache.get(codec)
        if cached is not None:
            return cached

        async with self._lock:
            cached = self._cache.get(codec)
            if cached is not None:
                return cached

            if codec in {"mp3", "mp3_low"}:
                bitrate = 128 if codec == "mp3" else 64
                output_args = [
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    f"{bitrate}k",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    "-write_xing",
                    "0",
                    "-id3v2_version",
                    "0",
                    "-f",
                    "mp3",
                ]
            else:
                output_args = [
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
                ]

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
                *output_args,
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

            self._cache[codec] = data
            return data
