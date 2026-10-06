import pytest

from frame_ingest.models import Quote, Segment
from frame_ingest.pipeline.synthesize import (
    clean_tags,
    fallback_chapters,
    repair_chapters,
    validate_chapters,
    verify_quotes,
)

D = 600.0


def test_valid_chapters_pass() -> None:
    b = [("A", 0.0, 200.0), ("B", 200.0, 450.0), ("C", 450.0, 600.0)]
    assert validate_chapters(b, D) == []


@pytest.mark.parametrize(
    ("bounds", "needle"),
    [
        ([], "no chapters"),
        ([("A", 5, 300), ("B", 300, 600)], "must start at 0"),
        ([("A", 0, 300), ("B", 300, 590)], "must end at"),
        ([("A", 0, 280), ("B", 300, 600)], "gap"),
        ([("A", 0, 320), ("B", 300, 600)], "overlap"),
        ([("A", 0, 300), ("B", 300, 900)], "exceeds"),
        ([("A", 0, 0), ("B", 0, 600)], "shorter"),
    ],
)
def test_invalid_chapters_are_detected(bounds, needle) -> None:
    assert any(needle in p for p in validate_chapters(bounds, D))


def test_repair_makes_any_proposal_valid_and_contiguous() -> None:
    messy = [("B", 305.0, 590.0), ("A", 0.4, 280.0), ("C", 900.0, 1000.0), ("Dup", 305.2, 310.0)]
    fixed = repair_chapters(messy, D)
    assert validate_chapters(fixed, D) == []
    assert [t for t, _, _ in fixed] == ["A", "B"]  # sorted; beyond-video and near-dup dropped
    assert fixed[0][1] == 0 and fixed[-1][2] == D


def test_repair_without_usable_chapters_raises() -> None:
    with pytest.raises(ValueError):
        repair_chapters([("X", 9999, 10000)], D)
    with pytest.raises(ValueError):
        repair_chapters([], D)


def test_fallback_chapters_are_valid() -> None:
    for dur in (3, 61, 4000):
        fb = fallback_chapters(dur, 24)
        assert validate_chapters(fb, dur) == []
    assert len(fallback_chapters(4000, 5)) == 5


def seg(i: int, t: float, text: str) -> Segment:
    return Segment(id=i, start=t, end=t + 4, raw_text=text, corrected_text=text)


def test_verify_quotes_keeps_only_verbatim_and_snaps_time() -> None:
    segs = [
        seg(0, 10, "We shipped the Widget on Friday."),
        seg(1, 14, "Customers loved it, honestly."),
    ]
    quotes = [
        Quote(t=99, text="we shipped the widget on friday"),  # case/punct tolerant, wrong time
        Quote(t=14, text="Customers loved it, honestly."),
        Quote(t=12, text="We invented a time machine."),  # not in transcript
    ]
    kept, dropped = verify_quotes(quotes, segs, 0, 60)
    assert dropped == 1 and len(kept) == 2
    assert kept[0].t == 10  # snapped to the segment that contains it
    assert kept[1].t == 14


def test_verify_quotes_spanning_segments_and_dedupes() -> None:
    segs = [seg(0, 0, "first half of a"), seg(1, 4, "sentence that continues")]
    q = [Quote(t=0, text="half of a sentence that"), Quote(t=0, text="Half of a sentence that")]
    kept, _ = verify_quotes(q, segs, 0, 10)
    assert len(kept) == 1


def test_clean_tags() -> None:
    assert clean_tags(["Machine Learning", "machine-learning", " AI/ML ", ""]) == [
        "machine-learning",
        "ai-ml",
    ]
