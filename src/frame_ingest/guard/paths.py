"""The job-directory jail and symlink refusal.

The engine sets the jail to the job directory while it works. ffmpeg inputs and outputs, and
atomic writes, must resolve (realpath) inside it, and the final path component may never be a
symlink. With no jail set, nothing but pipes and synthetic sources is allowed.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from frame_ingest.errors import FrameIngestError

_JAIL: ContextVar[Path | None] = ContextVar("frame_ingest_jail", default=None)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class PathRejected(FrameIngestError):
    code = "path_rejected"
    status = 400


@contextmanager
def job_jail(root: Path) -> Iterator[None]:
    token = _JAIL.set(Path(os.path.realpath(root)))
    try:
        yield
    finally:
        _JAIL.reset(token)


def current_jail() -> Path | None:
    return _JAIL.get()


def is_within(path: Path, root: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
    except ValueError:
        return False
    return True


def require_inside(path: Path, root: Path | None, *, what: str = "path") -> Path:
    if root is None:
        raise PathRejected(f"Refusing {what}: no job directory is active.")
    if not is_within(path, root):
        raise PathRejected(f"Refusing {what} outside the job directory.")
    return path


def require_regular_file(path: Path, *, what: str = "file") -> Path:
    """A real file: not a symlink (even one pointing somewhere harmless), not a directory/device."""
    if path.is_symlink():
        raise PathRejected(f"Refusing to follow a symlink ({what}).")
    if not path.is_file():
        raise PathRejected(f"Not a regular file ({what}).")
    return path


def read_bytes_nofollow(path: Path) -> bytes:
    """Read a file inside the job directory, refusing a symlink planted in its place."""
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        raise PathRejected(f"Cannot read '{path.name}' (missing or a symlink).") from exc
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def open_source_nofollow(path: Path) -> int:
    """File descriptor for a user-supplied input, refusing a symlink at open time (no TOCTOU)."""
    try:
        return os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        raise PathRejected("Cannot open the input (missing, unreadable or a symlink).") from exc
