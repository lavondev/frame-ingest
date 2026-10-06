from pathlib import Path

import imagehash
from PIL import Image, ImageDraw

from frame_ingest.pipeline.frames import (
    Candidate,
    SceneCut,
    dedupe_hashes,
    detect_scenes,
    parse_scene_output,
    plan_candidates,
    thin_to_cap,
)

SHOWINFO = """\
[Parsed_metadata_2 @ 0x1] lavfi.scene_score=0.703431
[Parsed_showinfo_3 @ 0x2] n:   0 pts:  30720 pts_time:3       duration:   1024 fmt:yuv420p
[Parsed_showinfo_3 @ 0x2] color_range:unknown
[Parsed_metadata_2 @ 0x1] lavfi.scene_score=0.885629
[Parsed_showinfo_3 @ 0x2] n:   1 pts:  61440 pts_time:6.25    duration:   1024 fmt:yuv420p
[Parsed_showinfo_3 @ 0x2] config in time_base: 1/10240, frame_rate: 10/1
"""


def test_parse_scene_output_reads_timestamps_and_scores() -> None:
    cuts = parse_scene_output(SHOWINFO)
    assert [(c.t, c.score) for c in cuts] == [(3.0, 0.703431), (6.25, 0.885629)]


async def test_detect_scenes_finds_the_real_cuts(sample_video: Path, tmp_path: Path) -> None:
    cuts = await detect_scenes(tmp_path, sample_video, 0.3)
    times = [round(c.t) for c in cuts]
    assert times == [6, 12, 18]
    # cached: same threshold reads the file, a different one recomputes
    assert (tmp_path / "frames" / "scenecuts.json").is_file()
    assert [round(c.t) for c in await detect_scenes(tmp_path, sample_video, 0.3)] == [6, 12, 18]


def test_plan_candidates_first_cuts_and_interval_fill() -> None:
    cands = plan_candidates(60, [SceneCut(t=20), SceneCut(t=20.5), SceneCut(t=55)], min_interval=10)
    assert cands[0].reason == "first" and cands[0].t <= 0.5
    ts = [c.t for c in cands]
    assert ts == sorted(ts)
    assert sum(1 for c in cands if c.reason == "scene") == 2  # 20.5 collapses into 20
    gaps = [b - a for a, b in zip(ts, ts[1:], strict=False)]
    assert max(gaps) <= 10.5  # static stretches still get coverage
    assert any(c.reason == "interval" for c in cands)
    assert ts[-1] < 60


def test_plan_candidates_short_video() -> None:
    cands = plan_candidates(2.0, [], 10)
    assert len(cands) == 1 and cands[0].t == 0.5


def test_thin_to_cap_keeps_first_and_spreads_evenly() -> None:
    cands = [Candidate(float(t), "interval") for t in range(0, 200, 2)]  # 100 evenly spaced
    cands[10] = Candidate(20.0, "scene", 0.9)
    out = thin_to_cap(cands, 10, 200)
    assert len(out) == 10 and out[0].t == 0
    ts = [c.t for c in out]
    assert max(b - a for a, b in zip(ts, ts[1:], strict=False)) < 45  # no big holes
    assert any(c.reason == "scene" for c in out)  # scene frames are preferred survivors


def test_thin_to_cap_noop_below_cap() -> None:
    cands = [Candidate(float(t), "interval") for t in range(5)]
    assert thin_to_cap(cands, 10, 100) == cands


def _img(shape: str) -> Image.Image:
    im = Image.new("RGB", (128, 128), "white")
    d = ImageDraw.Draw(im)
    if shape == "circle":
        d.ellipse((20, 20, 108, 108), fill="black")
    elif shape == "bars":
        for x in range(0, 128, 16):
            d.rectangle((x, 0, x + 7, 127), fill="black")
    return im


def test_dedupe_hashes_drops_near_duplicates_keeps_changes() -> None:
    a1, a2 = _img("circle"), _img("circle")
    a2.putpixel((3, 3), (0, 0, 0))  # tiny change
    hashes = [imagehash.phash(i) for i in (a1, a2, _img("bars"), _img("bars"), _img("circle"))]
    assert dedupe_hashes(hashes, 5) == [True, False, True, False, True]
    assert dedupe_hashes(hashes, 0)[0] is True
