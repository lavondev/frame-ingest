"""OS-level sandbox for ffmpeg, for the residual risk of decoder bugs.

ffmpeg already only sees allowlisted arguments and local files (`ffmpeg_args`), but a decoder
bug could still run attacker code. The sandbox limits what that code could do: no network, no
reading your home directory (except the job directory and ffmpeg's own files), and no writing
anywhere except the job directory.

* macOS: `sandbox-exec` with a deny-by-default profile.
* Linux: `bwrap` (bubblewrap) with every namespace unshared, a read-only view of the system, an
  empty home, and the job directory bound read-write.

Modes (config `sandbox`): `off`; `auto` (default: use it when it works, else run unsandboxed and
say so in `doctor`); `require` (refuse to run without it). Whether the sandbox works is learnt by
running a trivial command inside it once, not by looking for the binary.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from frame_ingest.errors import FrameIngestError
from frame_ingest.guard.subproc import Limits, run_process

Mode = Literal["off", "auto", "require"]
_state: dict[str, object] = {"mode": "auto", "backend": "unprobed"}
_SBPL_BAD = ('"', "\\", "\n", "\r", "\x00")


class SandboxUnavailable(FrameIngestError):
    code = "sandbox_unavailable"
    status = 501


def configure(mode: Mode) -> None:
    """Set the process-wide mode (the CLI does this once from config)."""
    _state["mode"] = mode


def mode() -> Mode:
    m = _state["mode"]
    if m == "off" or m == "require":
        return m
    return "auto"


def _real(p: Path | str) -> str:
    return os.path.realpath(p)


async def _works(argv: list[str]) -> bool:
    try:
        res = await run_process(argv, timeout=15, limits=Limits(max_stdout=4096, max_stderr=4096))
    except (OSError, FrameIngestError):
        return False
    return res.returncode == 0


async def backend() -> str | None:
    """`sandbox-exec`, `bwrap`, or None. Probed once per process."""
    cached = _state["backend"]
    if cached != "unprobed":
        return cached if isinstance(cached, str) else None
    found: str | None = None
    if sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec"):
        probe = "(version 1)(deny default)(allow process-exec*)(allow file-read*)"
        if await _works(["/usr/bin/sandbox-exec", "-p", probe, "/usr/bin/true"]):
            found = "sandbox-exec"
    elif sys.platform.startswith("linux") and (bwrap := shutil.which("bwrap")):
        if await _works([bwrap, "--unshare-all", "--ro-bind", "/", "/", "/usr/bin/true"]):
            found = "bwrap"
    _state["backend"] = found
    return found


def reset_probe() -> None:
    _state["backend"] = "unprobed"


def _sbpl(jail: str | None, exe_dir: str, home: str) -> str:
    for value in (jail or "", exe_dir, home):
        if any(c in value for c in _SBPL_BAD):
            raise SandboxUnavailable("A path contains characters the sandbox profile cannot hold.")
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec*)",
        "(allow process-fork)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow signal (target self))",
        "(allow file-read*)",  # a read-only view of the system (libraries, frameworks) ...
        f'(deny file-read* (subpath "{home}"))',  # ... except your home directory ...
        f'(allow file-read* (subpath "{exe_dir}"))',  # ... which still holds ffmpeg itself ...
        '(allow file-write* (literal "/dev/null"))',
    ]
    if jail:
        lines += [  # ... and the job directory, the only place anything may be written.
            f'(allow file-read* (subpath "{jail}"))',
            f'(allow file-write* (subpath "{jail}"))',
        ]
    return "\n".join(lines)  # no `(allow network*)`: the default deny covers the network


async def wrap(argv: Sequence[str], *, jail: Path | None, exe_dir: Path) -> list[str]:
    """`argv` unchanged (mode off, or auto without a working sandbox) or wrapped in the sandbox."""
    m = mode()
    if m == "off":
        return list(argv)
    kind = await backend()
    if kind is None:
        if m == "require":
            raise SandboxUnavailable(
                "sandbox: require is set, but no working sandbox was found "
                "(macOS: sandbox-exec; Linux: bubblewrap with user namespaces)."
            )
        return list(argv)
    jail_s = _real(jail) if jail else None
    exe_s = _real(exe_dir)
    home = _real(Path.home())
    if kind == "sandbox-exec":
        return ["/usr/bin/sandbox-exec", "-p", _sbpl(jail_s, exe_s, home), *argv]
    bwrap = shutil.which("bwrap") or "/usr/bin/bwrap"
    cmd = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",  # noqa: S108
        "--tmpfs",
        home,
        "--ro-bind",
        exe_s,
        exe_s,
    ]
    if jail_s:
        cmd += ["--bind", jail_s, jail_s, "--chdir", jail_s]
    return [*cmd, "--", *argv]
