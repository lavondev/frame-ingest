import math

import pytest

from frame_ingest.pipeline.timefmt import (
    FRAME_NAME_RE,
    fmt_range,
    fmt_ts,
    frame_filename,
    offset_segments_t,
    parse_ts,
    ts_anchor,
)


def test_fmt_ts_basic_and_floor() -> None:
    assert fmt_ts(0) == "00:00:00"
    assert fmt_ts(59.99) == "00:00:59"
    assert fmt_ts(3661.2) == "01:01:01"
    assert fmt_ts(100 * 3600) == "100:00:00"


def test_fmt_ts_clamps_to_duration_and_zero() -> None:
    assert fmt_ts(500, duration=120) == "00:02:00"
    assert fmt_ts(-5) == "00:00:00"
    assert fmt_ts(math.nan) == "00:00:00"
    assert fmt_range(10, 999, duration=65) == "00:00:10 - 00:01:05"


def test_parse_ts_roundtrip_and_errors() -> None:
    assert parse_ts("01:02:03") == 3723
    assert parse_ts("00:00:01.5") == 1.5
    for bad in ("1:2", "aa:bb:cc", "00:61:00"):
        with pytest.raises(ValueError):
            parse_ts(bad)
    for t in (0, 59, 3600, 7322):
        assert parse_ts(fmt_ts(t)) == t


def test_offset_segments_t_offsets_and_clamps() -> None:
    assert offset_segments_t(1.0, 2.0, 600.0, 1000.0) == (601.0, 602.0)
    assert offset_segments_t(5.0, 9.0, 598.0, 600.0) == (600.0, 600.0)  # beyond end clamps
    s, e = offset_segments_t(-1.0, 0.5, 0.0, 10.0)
    assert (s, e) == (0.0, 0.5)


def test_ts_anchor_unique_on_collision() -> None:
    taken: set[str] = set()
    assert ts_anchor(125.4, taken) == "t-000205"
    assert ts_anchor(125.9, taken) == "t-000205-2"
    assert ts_anchor(125.9, taken) == "t-000205-3"
    assert ts_anchor(3725) == "t-010205"


def test_frame_filename_pattern() -> None:
    assert frame_filename(12.34) == "frame_0012.34.jpg"
    assert frame_filename(3600.5) == "frame_3600.50.jpg"
    assert FRAME_NAME_RE.match(frame_filename(0.5))
    assert not FRAME_NAME_RE.match("../etc/passwd")
    assert not FRAME_NAME_RE.match("frame_1.jpg")
