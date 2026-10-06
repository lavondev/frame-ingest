"""Timestamp helpers. Every timestamp that reaches a document goes through fmt_ts."""

from __future__ import annotations

import math
import re

_TS_RE = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)(?:\.(\d+))?$")


def fmt_ts(seconds: float, duration: float | None = None) -> str:
    """HH:MM:SS (floored). When duration is given the value is clamped to [0, duration]."""
    if not math.isfinite(seconds):
        seconds = 0.0
    seconds = max(0.0, seconds)
    if duration is not None:
        seconds = min(seconds, max(0.0, duration))
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_range(start: float, end: float, duration: float | None = None) -> str:
    return f"{fmt_ts(start, duration)} - {fmt_ts(end, duration)}"


def parse_ts(text: str) -> float:
    """Parse HH:MM:SS[.fff] -> seconds. Raises ValueError on malformed input."""
    m = _TS_RE.match(text.strip())
    if not m:
        raise ValueError(f"not a HH:MM:SS timestamp: {text!r}")
    h, mi, s, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + (float(f"0.{frac}") if frac else 0.0)


def offset_segments_t(
    start: float, end: float, offset: float, duration: float
) -> tuple[float, float]:
    """Shift chunk-relative times to absolute video time, clamped to [0, duration]."""
    s = min(max(start + offset, 0.0), duration)
    e = min(max(end + offset, s), duration)
    return s, e


def ts_anchor(seconds: float, taken: set[str] | None = None) -> str:
    """Stable per-timestamp anchor id like t-000125 (HHMMSS). Suffix -2, -3… on collisions."""
    total = int(max(0.0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    base = f"t-{h:02d}{m:02d}{s:02d}"
    if taken is None:
        return base
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        candidate = f"{base}-{n}"
    taken.add(candidate)
    return candidate


def frame_filename(t: float) -> str:
    return f"frame_{t:07.2f}.jpg"


FRAME_NAME_RE = re.compile(r"^frame_\d{4,}\.\d{2}\.jpg$")
