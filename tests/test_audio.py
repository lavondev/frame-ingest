from pathlib import Path

import pytest

from frame_ingest.errors import FrameIngestError
from frame_ingest.models import StageName
from frame_ingest.pipeline.audio import AudioResult, plan_chunks, split_window
from frame_ingest.pipeline.audio import run as audio_run
from frame_ingest.pipeline.context import PipelineContext
from frame_ingest.pipeline.probe import probe_video
from frame_ingest.providers.fake import build_fake_providers


def test_plan_single_chunk() -> None:
    assert plan_chunks(30, 600, 1) == [(0.0, 30)]
    assert plan_chunks(600, 600, 1) == [(0.0, 600)]
    assert plan_chunks(0, 600, 1) == []


def test_plan_covers_everything_with_overlap() -> None:
    w = plan_chunks(1500, 600, 1.0)
    assert w == [(0.0, 600.0), (599.0, 1200.0), (1199.0, 1500.0)]
    assert w[0][0] == 0 and w[-1][1] == 1500
    for (_, e1), (s2, _) in zip(w, w[1:], strict=False):
        assert s2 < e1  # overlap
        assert e1 - s2 == pytest.approx(1.0)


def test_plan_merges_tiny_tail() -> None:
    w = plan_chunks(1203, 600, 1.0)  # tail would be 3 s
    assert w == [(0.0, 600.0), (599.0, 1203.0)]


def test_plan_no_overlap_short_mode() -> None:
    w = plan_chunks(100, 45, 0.0)
    assert w == [(0.0, 45.0), (45.0, 90.0), (90.0, 100.0)]


def test_split_window_overlaps() -> None:
    a, b = split_window(0, 100, 1.0)
    assert a == (0, 50) and b == (49, 100)


def make_ctx(config, tmp_path: Path, video: Path, info) -> PipelineContext:
    from frame_ingest.config import resolve_settings

    job_dir = tmp_path / "job"
    job_dir.mkdir(exist_ok=True)
    ctx = PipelineContext(
        job_id="a" * 12,
        job_dir=job_dir,
        config=config,
        settings=resolve_settings(config),
        video=info,
        video_path=video,
        providers=build_fake_providers(),
    )
    ctx.current_stage = StageName.AUDIO
    return ctx


async def test_audio_stage_chunks_have_correct_duration_and_size(
    config, long_audio_video, tmp_path
) -> None:
    config = config.model_copy(update={"chunk_minutes": 0.6})  # 36 s chunks
    info = await probe_video(long_audio_video, filename="long.mp4", size_bytes=1, sha256="x")
    assert info.has_audio and 79 < info.duration_s < 81
    ctx = make_ctx(config, tmp_path, long_audio_video, info)
    res = await audio_run(ctx)
    assert isinstance(res, AudioResult) and len(res.chunks) == 3
    assert res.chunks[0].start == 0 and res.chunks[-1].end == pytest.approx(info.duration_s)
    for c in res.chunks:
        f = ctx.job_dir / c.file
        assert f.is_file() and f.suffix == ".ogg"
        assert c.size_bytes < 25 * 1024 * 1024
    assert not (ctx.job_dir / "audio" / "full.flac").exists()  # temp file cleaned up
    assert ctx.current_warnings() == []  # duration metadata matched for every chunk


async def test_audio_stage_resplits_oversized_chunks(config, long_audio_video, tmp_path) -> None:
    config = config.model_copy(update={"chunk_minutes": 0.6, "max_chunk_mb": 0.12})
    info = await probe_video(long_audio_video, filename="long.mp4", size_bytes=1, sha256="x")
    ctx = make_ctx(config, tmp_path, long_audio_video, info)
    res = await audio_run(ctx)
    assert len(res.chunks) > 3
    assert all(c.size_bytes <= 0.12 * 1024 * 1024 for c in res.chunks)
    assert res.chunks[0].start == 0 and res.chunks[-1].end == pytest.approx(info.duration_s)
    # contiguous coverage: each chunk starts at/before the previous one ends
    for a, b in zip(res.chunks, res.chunks[1:], strict=False):
        assert b.start <= a.end


async def test_audio_stage_fails_clearly_if_cannot_shrink(
    config, long_audio_video, tmp_path
) -> None:
    config = config.model_copy(update={"chunk_minutes": 0.6, "max_chunk_mb": 0.001})
    info = await probe_video(long_audio_video, filename="long.mp4", size_bytes=1, sha256="x")
    ctx = make_ctx(config, tmp_path, long_audio_video, info)
    with pytest.raises(FrameIngestError, match="larger than"):
        await audio_run(ctx)


async def test_audio_stage_short_mode_uses_short_chunks(config, long_audio_video, tmp_path) -> None:
    from frame_ingest.providers.base import TranscriberCaps
    from frame_ingest.providers.fake import FakeTranscriber

    info = await probe_video(long_audio_video, filename="long.mp4", size_bytes=1, sha256="x")
    ctx = make_ctx(config, tmp_path, long_audio_video, info)
    ctx.providers.transcriber = FakeTranscriber(caps=TranscriberCaps(segment_timestamps=False))
    res = await audio_run(ctx)
    assert res.short_mode and len(res.chunks) == 2  # 80 s / 45 s
    assert res.chunks[0].end == 45 and res.chunks[1].start == 45  # no overlap in chunk mode


async def test_no_audio_track_skips(config, silent_video, tmp_path) -> None:
    info = await probe_video(silent_video, filename="s.mp4", size_bytes=1, sha256="x")
    assert info.has_audio is False
    ctx = make_ctx(config, tmp_path, silent_video, info)
    assert (await audio_run(ctx)).chunks == []
