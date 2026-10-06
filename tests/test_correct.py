from frame_ingest.llm_schemas import CorrectedSegmentOut, CorrectionOut
from frame_ingest.models import Segment
from frame_ingest.pipeline.correct import accept_text, render_diff, validate_correction


def segs(n: int) -> list[Segment]:
    return [
        Segment(id=i + 10, start=i * 3.0, end=i * 3.0 + 3, raw_text=f"raw text {i}")
        for i in range(n)
    ]


def out(*pairs: tuple[int, str]) -> CorrectionOut:
    return CorrectionOut(segments=[CorrectedSegmentOut(id=i, corrected_text=t) for i, t in pairs])


def test_valid_ids_accepted_in_any_order() -> None:
    texts, err = validate_correction(segs(3), out((12, "c"), (10, "a"), (11, "b")))
    assert err is None and texts == {10: "a", 11: "b", 12: "c"}


def test_missing_extra_and_duplicate_ids_are_rejected() -> None:
    _, err = validate_correction(segs(3), out((10, "a"), (11, "b")))
    assert err and "missing ids [12]" in err
    _, err = validate_correction(segs(2), out((10, "a"), (11, "b"), (99, "z")))
    assert err and "unexpected ids [99]" in err
    _, err = validate_correction(segs(2), out((10, "a"), (10, "a")))
    assert err and "duplicate" in err


def test_whitespace_is_normalised() -> None:
    texts, err = validate_correction(segs(1), out((10, "  hello \n world  ")))
    assert err is None and texts[10] == "hello world"


def test_accept_text_guards_against_invention_and_deletion() -> None:
    assert accept_text("raw text here", "Raw text here.")
    assert not accept_text("raw text here", "")
    assert not accept_text("short", "a much much longer invented replacement sentence")
    assert not accept_text("a fairly long original sentence of words", "ok")


def test_diff_log_lists_only_changed_segments() -> None:
    s = segs(3)
    s[0].corrected_text = "Raw text 0."
    s[1].corrected_text = s[1].raw_text
    s[2].corrected_text = "raw text 2 fixed"
    diff, changed = render_diff(s, 100)
    assert changed == 2
    assert "- raw text 0" in diff and "+ Raw text 0." in diff
    assert "raw text 1" not in diff.split("\n\n", 1)[1]
    assert diff.startswith("# Transcript corrections: 2 of 3")
