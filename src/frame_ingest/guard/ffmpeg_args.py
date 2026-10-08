"""The only way an ffmpeg argv is built (PLAN T3/T5).

Callers hand in the options they want; this module rebuilds the command from an allowlist:

* every option and its value must match a known pattern (unknown options are refused);
* an input is either a synthetic `lavfi` source from a short allowlist, or a regular, non-symlink
  file inside the job directory whose container is sniffed and whose demuxer is forced with `-f`
  (so a playlist or concat file can never be interpreted as one);
* every input gets `-protocol_whitelist file,pipe`, so ffmpeg cannot open a network URL;
* an output is `-` (a pipe) or a path inside the job directory;
* `-nostdin`, `-hide_banner` and the log level are always set by us.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path

from frame_ingest.errors import FrameIngestError
from frame_ingest.guard.media import check_container
from frame_ingest.guard.paths import is_within, require_regular_file

_NUM = re.compile(r"^\d{1,9}(\.\d{1,6})?$")
_INT = re.compile(r"^\d{1,9}$")
_LAVFI_SOURCE = re.compile(r"^(anullsrc|color|testsrc2?|sine)(=[A-Za-z0-9_=:.,\- ]*)?$")
_FILTER_CHARS = re.compile(r"^[A-Za-z0-9_=:,.'()*+<>/\- ]{1,2000}$")
_FILTER_DENY = re.compile(
    r"(?i)\b(movie|amovie|subtitles|ass|sendcmd|asendcmd|zmq|azmq|drawtext|textfile|readfile|"
    r"file|sidedata|reload)\b"
)
_LOGLEVELS = {"quiet", "error", "warning", "info"}


class ArgRejected(FrameIngestError):
    code = "argv_rejected"
    status = 400


def _one_of(*allowed: str) -> Callable[[str], bool]:
    return lambda v: v in allowed


def _matches(pattern: re.Pattern[str]) -> Callable[[str], bool]:
    return lambda v: pattern.match(v) is not None


def _filter_ok(value: str) -> bool:
    if value.startswith("-"):  # an option, not a filter graph (found by tests/test_fuzz.py)
        return False
    return _FILTER_CHARS.match(value) is not None and _FILTER_DENY.search(value) is None


FLAG_OPTIONS = frozenset({"-y", "-vn", "-an", "-sn", "-version"})
VALUE_OPTIONS: dict[str, Callable[[str], bool]] = {
    "-ss": _matches(_NUM),
    "-t": _matches(_NUM),
    "-ac": _matches(_INT),
    "-ar": _matches(_INT),
    "-q:v": _matches(_INT),
    "-frames:v": _matches(_INT),
    "-b:a": _matches(re.compile(r"^\d{1,4}k$")),
    "-c:a": _one_of("libopus", "libmp3lame", "flac"),
    "-application": _one_of("voip", "audio", "lowdelay"),
    "-map_metadata": _matches(re.compile(r"^-?\d{1,3}$")),
    "-avoid_negative_ts": _one_of("make_zero", "make_non_negative", "auto"),
    "-map": _matches(re.compile(r"^\d{1,3}:[av]:\d{1,3}$")),
    "-f": _one_of("lavfi", "null", "f32le"),  # f32le: raw 32-bit float samples, for local ASR
    "-vf": _filter_ok,
    "-af": _one_of("volumedetect"),  # loudness measurement only (the audio guarantee)
}


def _check_input_file(raw: str, jail: Path | None) -> tuple[str, str]:
    if jail is None:
        raise ArgRejected("Refusing a file input: no job directory is active.")
    if "://" in raw or raw.startswith("-") or "\x00" in raw or not os.path.isabs(raw):
        raise ArgRejected("Refusing an ffmpeg input that is not an absolute local path.")
    path = Path(raw)
    if not is_within(path, jail):
        raise ArgRejected("Refusing an ffmpeg input outside the job directory.")
    require_regular_file(path, what="ffmpeg input")
    container = check_container(path)
    return str(path), container.demuxer


def _check_output(raw: str, jail: Path | None) -> str:
    if raw == "-":
        return raw
    if jail is None:
        raise ArgRejected("Refusing a file output: no job directory is active.")
    if "://" in raw or raw.startswith("-") or "\x00" in raw or not os.path.isabs(raw):
        raise ArgRejected("Refusing an ffmpeg output that is not an absolute local path.")
    path = Path(raw)
    if path.is_symlink() or not is_within(path.parent, jail) or not is_within(path, jail):
        raise ArgRejected("Refusing an ffmpeg output outside the job directory.")
    return str(path)


def build_argv(args: Sequence[str], *, loglevel: str = "error", jail: Path | None) -> list[str]:
    """Validate `args` and return the full ffmpeg argument list (without the executable)."""
    if loglevel not in _LOGLEVELS:
        raise ArgRejected(f"Unsupported log level '{loglevel}'.")
    out: list[str] = ["-hide_banner", "-nostdin", "-loglevel", loglevel]
    fmt: str | None = None  # last `-f` seen since the previous input/output
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in FLAG_OPTIONS:
            out.append(arg)
            i += 1
        elif arg == "-i" or arg in VALUE_OPTIONS:
            if i + 1 >= len(args):
                raise ArgRejected(f"Option {arg} is missing its value.")
            value = args[i + 1]
            if arg == "-i":
                if fmt == "lavfi":
                    if _LAVFI_SOURCE.match(value) is None:
                        raise ArgRejected("Refusing a lavfi source that is not allowlisted.")
                    out += ["-protocol_whitelist", "file,pipe", "-f", "lavfi", "-i", value]
                else:
                    path, demuxer = _check_input_file(value, jail)
                    out += ["-protocol_whitelist", "file,pipe", "-f", demuxer, "-i", path]
                fmt = None
            else:
                if not VALUE_OPTIONS[arg](value):
                    raise ArgRejected(f"Refusing value for {arg}.")
                if arg == "-f":
                    fmt = value
                    if value != "lavfi":  # an output format; emitted here, before the output
                        out += [arg, value]
                else:
                    out += [arg, value]
            i += 2
        elif arg.startswith("-") and arg != "-":
            raise ArgRejected(f"Refusing unknown ffmpeg option '{arg[:40]}'.")
        else:
            out.append(_check_output(arg, jail))
            fmt = None
            i += 1
    return out
