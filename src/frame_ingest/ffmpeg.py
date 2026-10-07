"""ffmpeg access. ffmpeg is NOT a system prerequisite: the binary ships in the imageio-ffmpeg
wheel. ffprobe is not bundled, so metadata is parsed from `ffmpeg -i` output (see pipeline.probe).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

from frame_ingest.errors import FrameIngestError
from frame_ingest.guard import sandbox
from frame_ingest.guard.ffmpeg_args import build_argv
from frame_ingest.guard.paths import current_jail
from frame_ingest.guard.subproc import Limits, run_process


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
    args: list[str],
    *,
    timeout: float = 3600,
    loglevel: str = "error",
    max_stdout: int | None = None,
) -> FFResult:
    """Run ffmpeg through the guard layer: the argv is rebuilt from an allowlist (inputs and
    outputs must be inside the active job directory) and the process runs with a scrubbed
    environment, limits, a timeout and output caps. Killed on cancellation or timeout."""
    jail = current_jail()
    argv = build_argv(args, loglevel=loglevel, jail=jail)
    exe = ffmpeg_exe()
    cmd = await sandbox.wrap([exe, *argv], jail=jail, exe_dir=Path(exe).parent)
    limits = Limits(max_stdout=max_stdout) if max_stdout else None
    res = await run_process(cmd, timeout=timeout, cwd=jail, limits=limits)
    return FFResult(res.returncode, res.stdout, res.stderr.decode("utf-8", errors="replace"))


async def ffmpeg_version() -> str:
    res = await run_ffmpeg(["-version"], loglevel="info", timeout=15)
    first = res.stdout.decode("utf-8", errors="replace").splitlines()
    return first[0] if first else "unknown"


def file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)
