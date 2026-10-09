"""Resource caps on inputs: size, duration, pixels and free disk."""

from __future__ import annotations

import shutil
from pathlib import Path

from frame_ingest.config import AppConfig
from frame_ingest.errors import MediaError
from frame_ingest.models import VideoInfo

_DISK_MARGIN = 1024**3  # leave 1 GiB free after audio, frames and outputs are written


def check_size(size_bytes: int, config: AppConfig) -> None:
    limit = int(config.max_file_mb * 1024 * 1024)
    if size_bytes > limit:
        raise MediaError(
            f"The file is {size_bytes / 1024**2:.0f} MB; the limit is {config.max_file_mb:.0f} MB "
            "(max_file_mb in config.yaml).",
            code="limit_exceeded",
            status=413,
        )


def check_disk(directory: Path, size_bytes: int) -> None:
    """The copy plus derived audio/frames need roughly twice the input size."""
    probe = directory
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < 2 * size_bytes + _DISK_MARGIN:
        raise MediaError(
            f"Not enough free disk space in {directory} ({free / 1024**3:.1f} GB free).",
            code="disk_full",
            status=507,
        )


def check_video(video: VideoInfo, config: AppConfig) -> None:
    if video.duration_s > config.max_duration_s:
        raise MediaError(
            f"The video is {video.duration_s / 3600:.1f} h long; the limit is "
            f"{config.max_duration_s / 3600:.1f} h (max_duration_s in config.yaml).",
            code="limit_exceeded",
            status=413,
        )
    if video.width and video.height and video.width * video.height > config.max_pixels:
        raise MediaError(
            f"Frames are {video.width}x{video.height}; the limit is {config.max_pixels} pixels "
            "(max_pixels in config.yaml).",
            code="limit_exceeded",
            status=413,
        )
