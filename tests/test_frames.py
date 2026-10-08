from pathlib import Path

import imagehash
from PIL import Image, ImageDraw, ImageFont

from frame_ingest.agent.common import agent_context
from frame_ingest.config import AppConfig
from frame_ingest.engine import Engine
from frame_ingest.pipeline import frames as frames_stage
from frame_ingest.pipeline.frames import (
    Candidate,
    SceneCut,
    dedupe_frames,
    dedupe_hashes,
    detect_scenes,
    parse_scene_output,
    plan_candidates,
    signature,
    thin_to_cap,
)
from frame_ingest.profiles import fake_bundle
from tests.conftest import TEXT_SLIDES

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


FREEZE = """\
[freezedetect @ 0x3] lavfi.freezedetect.freeze_start: 0
[freezedetect @ 0x3] lavfi.freezedetect.freeze_duration: 2.9
[freezedetect @ 0x3] lavfi.freezedetect.freeze_end: 2.9
[Parsed_metadata_2 @ 0x1] lavfi.scene_score=0.703431
[Parsed_showinfo_3 @ 0x2] n:   0 pts:  30720 pts_time:3       duration:   1024 fmt:yuv420p
[freezedetect @ 0x3] lavfi.freezedetect.freeze_start: 3.1
[freezedetect @ 0x3] lavfi.freezedetect.freeze_end: 9.2
[freezedetect @ 0x3] lavfi.freezedetect.freeze_start: 9.2
"""


def test_parse_scene_output_adds_still_screens() -> None:
    cuts = parse_scene_output(FREEZE)
    # the still stretch at 0 is the video start and the one at 3.1 is the scene cut at 3
    assert [(c.t, c.kind) for c in cuts] == [(3.0, "scene"), (9.2, "still")]
    assert cuts[1].score is None


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


async def test_detect_scenes_still_screens_add_nothing_to_moving_video(
    sample_video: Path, quiet_video: Path, tmp_path: Path
) -> None:
    cuts = await detect_scenes(tmp_path / "a", sample_video, 0.3, still_min_s=2.0)
    assert [(round(c.t), c.kind) for c in cuts] == [(6, "scene"), (12, "scene"), (18, "scene")]
    assert await detect_scenes(tmp_path / "b", quiet_video, 0.3, still_min_s=2.0) == []


async def test_detect_scenes_sees_text_only_changes(
    text_slides_video: Path, tmp_path: Path
) -> None:
    assert await detect_scenes(tmp_path / "a", text_slides_video, 0.3) == []  # the old blind spot
    cuts = await detect_scenes(tmp_path / "b", text_slides_video, 0.3, still_min_s=2.0)
    assert [(round(c.t), c.kind) for c in cuts] == [(6, "still"), (12, "still")]


async def test_text_only_slides_get_one_frame_each(
    config: AppConfig, text_slides_video: Path
) -> None:
    eng = Engine(config, fake_bundle)
    job = await eng.create(text_slides_video)
    res = await frames_stage.run(agent_context(eng, job))
    slide_s = 6
    assert len(res.frames) == len(TEXT_SLIDES)
    assert [int(f.t // slide_s) for f in res.frames] == list(range(len(TEXT_SLIDES)))


def test_plan_candidates_samples_near_the_end() -> None:
    cands = plan_candidates(24, [], min_interval=10)
    assert [(c.t, c.reason) for c in cands] == [
        (0.5, "first"),
        (10.5, "interval"),
        (23.5, "interval"),
    ]
    # a tail shorter than the interval is already covered by the last frame
    assert [c.t for c in plan_candidates(24, [SceneCut(t=16)], 10)] == [0.5, 10.5, 16.15]


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


def _slide(lines: list[str]) -> Image.Image:
    im = Image.new("RGB", (640, 360), (16, 24, 32))
    d = ImageDraw.Draw(im)
    for i, line in enumerate(lines):
        d.text((40, 40 + i * 70), line, fill="white", font=ImageFont.load_default(size=28))
    return im


def _camera() -> Image.Image:
    """Camera-like footage: textured everywhere, no flat background."""
    im = Image.new("RGB", (64, 36))
    im.putdata(
        [
            ((x * 37 + y * 101) % 256, (x * y) % 256, (x + y * 7) % 256)
            for y in range(36)
            for x in range(64)
        ]
    )
    return im.resize((640, 360), Image.Resampling.BILINEAR)


def test_dedupe_frames_sees_text_changes_phash_misses() -> None:
    a, a2 = (
        _slide(["Cache settings", "Default size: 256 MB"]),
        _slide(["Cache settings", "Default size: 256 MB"]),
    )
    b = _slide(["Cache settings", "Default size: 512 MB"])
    sigs = [signature(i) for i in (a, a2, b)]
    assert all(s.gray is not None for s in sigs)  # slides are screen-like
    assert sigs[0].phash - sigs[2].phash <= 5  # pHash alone calls them duplicates
    assert dedupe_frames(sigs, 5) == [True, False, False]
    assert dedupe_frames(sigs, 5, pixel_fraction=0.0008) == [True, False, True]


def test_dedupe_frames_camera_footage_uses_phash_alone() -> None:
    cam = _camera()
    shifted = cam.copy()
    shifted.paste(cam.crop((0, 0, 640, 359)), (0, 1))  # a one-pixel move: not a new shot
    sigs = [signature(cam), signature(shifted)]
    assert all(s.gray is None for s in sigs)
    assert dedupe_frames(sigs, 5, pixel_fraction=0.0008) == [True, False]
