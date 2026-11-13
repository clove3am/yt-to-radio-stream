"""Tuning audio used while an on-demand station starts."""

from __future__ import annotations

import asyncio
import logging
import math
import sys
from array import array

from .config import Settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 44100
TUNING_CLIP_SECONDS = 2.0
CHORDS = (
    (261.63, 329.63, 392.00),  # C major
    (220.00, 261.63, 329.63),  # A minor
    (174.61, 220.00, 261.63),  # F major
    (196.00, 246.94, 293.66),  # G major
)


def _pcm_chord(frequencies: tuple[float, ...]) -> bytes:
    """Synthesize a soft, stereo 2-second chord with fades."""

    frames = int(SAMPLE_RATE * TUNING_CLIP_SECONDS)
    pcm = array("h")
    fade = 0.22
    for index in range(frames):
        time = index / SAMPLE_RATE
        envelope = min(1.0, time / fade, (TUNING_CLIP_SECONDS - time) / fade)
        envelope = max(0.0, envelope)
        tremolo = 0.86 + 0.14 * math.sin(2 * math.pi * 0.5 * time)

        left = 0.0
        right = 0.0
        for note, frequency in enumerate(frequencies):
            amplitude = (0.10, 0.075, 0.055)[note]
            phase = note * 0.12
            left += amplitude * math.sin(2 * math.pi * frequency * time + phase)
            right += amplitude * math.sin(
                2 * math.pi * frequency * 1.0025 * time + phase
            )

        bass = 0.045 * math.sin(2 * math.pi * (frequencies[0] / 2) * time)
        left = (left + bass) * envelope * tremolo
        right = (right + bass) * envelope * tremolo
        pcm.append(int(max(-1.0, min(1.0, left)) * 32767))
        pcm.append(int(max(-1.0, min(1.0, right)) * 32767))

    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm.tobytes()


def _pcm_silence() -> bytes:
    frames = int(SAMPLE_RATE * TUNING_CLIP_SECONDS)
    return b"\x00" * (frames * 2 * 2)


class TuningAudioCache:
    """Generate and cache two-second tuning clips per codec.

    The default is a gentle synthesized chord progression. Set
    TUNING_AUDIO=silence for silent tuning instead.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[str, tuple[bytes, ...]] = {}
        self._lock = asyncio.Lock()

    async def get(self, codec: str) -> tuple[bytes, ...]:
        cached = self._cache.get(codec)
        if cached is not None:
            return cached

        async with self._lock:
            cached = self._cache.get(codec)
            if cached is not None:
                return cached

            if self.settings.tuning_audio == "silence":
                sources = [_pcm_silence()]
            else:
                sources = [_pcm_chord(chord) for chord in CHORDS]

            clips = tuple(
                [await self._encode(codec, pcm) for pcm in sources]
            )
            clips = tuple(clip for clip in clips if clip)
            self._cache[codec] = clips
            logger.info(
                "Prepared %s tuning clip(s) for %s", len(clips), codec
            )
            return clips

    async def _encode(self, codec: str, pcm: bytes) -> bytes:
        if codec in {"mp3", "mp3_low"}:
            bitrate = 128 if codec == "mp3" else 64
            output_args = [
                "-c:a",
                "libmp3lame",
                "-b:a",
                f"{bitrate}k",
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
                "-f",
                "adts",
            ]

        command = [
            self.settings.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            "-i",
            "pipe:0",
            *output_args,
            "pipe:1",
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(pcm), timeout=15
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                raise RuntimeError("FFmpeg timed out generating tuning audio")

            if process.returncode != 0:
                detail = stderr.decode("utf-8", "replace").strip()
                raise RuntimeError(
                    f"FFmpeg tuning-audio generator exited with code "
                    f"{process.returncode}: {detail[:300]}"
                )
            return stdout
        except Exception as exc:  # noqa: BLE001 - fall back to silent wait
            logger.warning("Could not generate tuning audio: %s", exc)
            return b""
