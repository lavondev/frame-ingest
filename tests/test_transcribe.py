import pytest

from frame_ingest.pipeline.audio import AudioChunk
from frame_ingest.pipeline.transcribe import (
    PROMPT_CHAR_BUDGET,
    parse_glossary,
    stitch,
    trim_repeated_prefix,
    truncate_prompt,
)
from frame_ingest.providers.base import RawSegment, RawTranscription


def chunk(i: int, s: float, e: float) -> AudioChunk:
    return AudioChunk(index=i, start=s, end=e, file=f"c{i}.ogg", size_bytes=1)


def raw(*segs: tuple[float, float, str]) -> RawTranscription:
    return RawTranscription(segments=[RawSegment(start=a, end=b, text=t) for a, b, t in segs])


def test_stitch_offsets_to_absolute_time() -> None:
    chunks = [chunk(0, 0, 600), chunk(1, 599, 1200)]
    results = {
        0: raw((0, 5, "hello there"), (590, 599.5, "end of first")),
        1: raw((2, 7, "second chunk starts"), (20, 25, "later words")),
    }
    segs = stitch(chunks, results, 1200)
    starts = [s.start for s in segs]
    assert starts == sorted(starts)
    assert segs[2].start == 601 and segs[2].end == 606  # offset by chunk.start=599
    assert [s.id for s in segs] == list(range(len(segs)))


def test_stitch_dedupes_overlap_region() -> None:
    # overlap is [599, 600]; both chunks hear the same words there -> only one copy kept
    chunks = [chunk(0, 0, 600), chunk(1, 599, 1200)]
    results = {
        0: raw((10, 14, "alpha beta"), (596, 599.9, "the quick brown")),
        1: raw((0.2, 1.0, "the quick brown"), (3, 8, "fox jumps over")),
    }
    segs = stitch(chunks, results, 1200)
    texts = [s.raw_text for s in segs]
    assert texts.count("the quick brown") == 1
    assert texts == ["alpha beta", "the quick brown", "fox jumps over"]


def test_stitch_trims_words_repeated_across_boundary() -> None:
    chunks = [chunk(0, 0, 100), chunk(1, 99, 200)]
    results = {
        0: raw((90, 99.4, "we should go to the market")),
        1: raw((0.4, 6, "go to the market and buy bread")),
    }
    segs = stitch(chunks, results, 200)
    assert [s.raw_text for s in segs] == ["we should go to the market", "and buy bread"]


def test_stitch_prefix_equals_prefix_of_full_result() -> None:
    chunks = [chunk(0, 0, 600), chunk(1, 599, 1200), chunk(2, 1199, 1500)]
    results = {
        0: raw((1, 5, "a"), (598.9, 599.8, "edge a")),
        1: raw((0.1, 0.9, "edge a"), (50, 55, "b")),
        2: raw((1, 4, "c")),
    }
    full = stitch(chunks, results, 1500)
    part = stitch(chunks, results, 1500, upto=2)
    assert [s.model_dump() for s in part] == [s.model_dump() for s in full[: len(part)]]


def test_stitch_chunk_mode_uses_boundaries() -> None:
    chunks = [chunk(0, 0, 45), chunk(1, 45, 90)]
    results = {0: raw((0, 45, "first chunk text")), 1: raw((0, 45, "second chunk text"))}
    segs = stitch(chunks, results, 90)
    assert [(s.start, s.end) for s in segs] == [(0, 45), (45, 90)]


def test_stitch_drops_empty_and_clamps_to_duration() -> None:
    chunks = [chunk(0, 0, 10)]
    segs = stitch(chunks, {0: raw((0, 4, "  "), (5, 14, "runs past the end"))}, 10)
    assert len(segs) == 1 and segs[0].end == 10


def test_trim_repeated_prefix() -> None:
    assert trim_repeated_prefix("one two three four", "three four five") == "five"
    assert trim_repeated_prefix("one two", "three four") == "three four"
    assert trim_repeated_prefix("a b", "b c") == "b c"  # a single repeated word is not trimmed


def test_parse_glossary_and_prompt_truncation() -> None:
    terms = parse_glossary("Kubernetes, Widget Frobnicator\n- OAuth2; kubernetes\n" + "x " * 40)
    assert terms == ["Kubernetes", "Widget Frobnicator", "OAuth2"]
    long = " ".join(f"term{i}," for i in range(500))
    out = truncate_prompt(long)
    assert len(out) <= PROMPT_CHAR_BUDGET and not out.endswith(",")
    assert truncate_prompt("short text") == "short text"


@pytest.mark.parametrize("n", [1, 2, 5])
def test_stitch_many_chunks_is_ordered_without_duplicates(n: int) -> None:
    chunks = [chunk(i, i * 100 - (1 if i else 0), (i + 1) * 100) for i in range(n)]
    results = {i: raw((1, 3, f"seg-{i}-a"), (50, 60, f"seg-{i}-b")) for i in range(n)}
    segs = stitch(chunks, results, n * 100)
    assert len({s.raw_text for s in segs}) == len(segs) == 2 * n
