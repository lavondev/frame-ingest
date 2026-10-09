"""The only place a child process is started (AGENTS.md rule 3).

argv list, never a shell; scrubbed environment (no API keys or other secrets reach a child);
own session so a timeout kills the whole process group; wall-clock timeout; output caps; and on
POSIX, resource limits (no core dumps, a cap on written file size, and on Linux an address-space
cap).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frame_ingest.errors import FrameIngestError

_SAFE_PATH = "/usr/bin:/bin"


class ProcessTimeout(FrameIngestError, TimeoutError):
    code = "process_timeout"
    status = 504


class OutputLimitExceeded(FrameIngestError):
    code = "output_limit"
    status = 500


class ProcessRejected(FrameIngestError):
    code = "process_rejected"
    status = 400


@dataclass(frozen=True)
class Limits:
    max_stdout: int = 64 * 1024 * 1024
    max_stderr: int = 32 * 1024 * 1024
    max_file_bytes: int = 16 * 1024**3  # RLIMIT_FSIZE: largest file the child may write
    max_memory_bytes: int = 8 * 1024**3  # RLIMIT_AS, Linux only


@dataclass
class ProcResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def scrubbed_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Only what a media tool needs. Nothing inherited, so no secret can leak to a child."""
    env = {"PATH": _SAFE_PATH, "LC_ALL": "C", "LANG": "C"}
    env.update(extra or {})
    return env


def _limit_setter(limits: Limits) -> Callable[[], None] | None:
    if os.name != "posix":  # pragma: no cover - Windows has no rlimits
        return None
    import resource

    def apply() -> None:  # runs in the child between fork and exec
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limits.max_file_bytes, limits.max_file_bytes))
        if sys.platform.startswith("linux"):  # macOS does not enforce RLIMIT_AS
            resource.setrlimit(resource.RLIMIT_AS, (limits.max_memory_bytes,) * 2)

    return apply


async def _drain(stream: asyncio.StreamReader | None, cap: int, label: str) -> bytes:
    if stream is None:
        return b""
    buf = bytearray()
    while chunk := await stream.read(65536):
        buf.extend(chunk)
        if len(buf) > cap:
            raise OutputLimitExceeded(f"The process wrote more than {cap} bytes to {label}.")
    return bytes(buf)


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


async def _discard(stream: asyncio.StreamReader | None) -> None:
    if stream is not None:
        while await stream.read(65536):
            pass


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Kill the group and collect the process. The pipes must be read to EOF first: asyncio does
    not report the exit while a paused, unread pipe is still open (a chatty child would hang)."""
    _kill_group(proc)
    with contextlib.suppress(TimeoutError):  # SIGKILL to the group leaves nothing to wait for
        await asyncio.wait_for(
            asyncio.gather(_discard(proc.stdout), _discard(proc.stderr), proc.wait()), 10
        )


async def run_process(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    limits: Limits | None = None,
    env: Mapping[str, str] | None = None,
) -> ProcResult:
    """Run `argv` (argv[0] must be an absolute path) and return its captured output."""
    if not argv or not os.path.isabs(argv[0]):
        raise ProcessRejected("The executable must be given as an absolute path.")
    if any(not isinstance(a, str) or "\x00" in a for a in argv):
        raise ProcessRejected("Process arguments must be strings without NUL bytes.")
    lim = limits or Limits()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=scrubbed_env(env),
        start_new_session=os.name == "posix",
        preexec_fn=_limit_setter(lim),
    )
    t_out = asyncio.ensure_future(_drain(proc.stdout, lim.max_stdout, "stdout"))
    t_err = asyncio.ensure_future(_drain(proc.stderr, lim.max_stderr, "stderr"))
    t_wait = asyncio.ensure_future(proc.wait())
    tasks: list[asyncio.Future[Any]] = [t_out, t_err, t_wait]

    async def abort() -> None:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)  # no reader left on the pipes
        await _reap(proc)

    try:
        out, err, _ = await asyncio.wait_for(asyncio.gather(t_out, t_err, t_wait), timeout)
    except TimeoutError as exc:
        await abort()
        raise ProcessTimeout(f"The process did not finish within {timeout:.0f} s.") from exc
    except BaseException:
        await asyncio.shield(abort())
        raise
    return ProcResult(proc.returncode or 0, out, err)
