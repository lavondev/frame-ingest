"""ffmpeg access. ffmpeg is NOT a system prerequisite: the binary ships in the imageio-ffmpeg
wheel. ffprobe is not bundled, so metadata is parsed from `ffmpeg -i` output (see pipeline.probe).
"""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass
from pathlib import Path

from frame_ingest.errors import FrameIngestError


@functools.lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    try:
        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:  # pragma: no cover - platform specific
        raise FrameIngestError(
            "The bundled ffmpeg binary could not be found. Reinstall frame-ingest (it ships ffmpeg "
            f"in the imageio-ffmpeg package). ({exc})",
            code="ffmpeg_missing",
        ) from exc


@dataclass
class FFResult:
    returncode: int
    stdout: bytes
    stderr: str


async def run_ffmpeg(
    args: list[str], *, timeout: float | None = None, loglevel: str = "error"
) -> FFResult:
    """Run ffmpeg without a shell. Kills the process on cancellation or timeout."""
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_exe(),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        loglevel,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    return FFResult(proc.returncode or 0, out, err.decode("utf-8", errors="replace"))


async def ffmpeg_version() -> str:
    res = await run_ffmpeg(["-version"], loglevel="info", timeout=15)
    first = res.stdout.decode("utf-8", errors="replace").splitlines()
    return first[0] if first else "unknown"


def file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)
