"""End-to-end through the Engine with fake providers on a real generated video.

Ported from faircopy's HTTP-level tests (tests/test_e2e.py). The server-only cases (SSE replay,
multipart upload limits, the job-concurrency semaphore, Range requests) were dropped with the
web stack; everything about the pipeline itself is kept.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from frame_ingest.config import AppConfig, load_config
from frame_ingest.engine import Engine
from frame_ingest.errors import FatalProviderError, JobNotFound, MediaError, ProviderError
from frame_ingest.models import JobSettings, JobStatus, ModelOverrides, StageName
from frame_ingest.pipeline.assemble import check_timestamps
from frame_ingest.pipeline.synthesize import validate_chapters
from frame_ingest.providers.base import ProviderBundle
from frame_ingest.providers.fake import FakeText, FakeTranscriber, FakeVision


def bundle(**kw: Any) -> ProviderBundle:
    return ProviderBundle(
        transcriber=kw.get("transcriber") or FakeTranscriber(),
        vision=kw.get("vision") or FakeVision(),
        text=kw.get("text") or FakeText(),
    )


def engine(config: AppConfig, providers: ProviderBundle | None = None) -> Engine:
    prov = providers or bundle()
    return Engine(config, lambda: prov)


async def run_new(eng: Engine, video: Path, settings: JobSettings | None = None) -> Any:
    job = await eng.create(video, settings)
    return await eng.run(job.id)


def result(eng: Engine, job: Any) -> tuple[str, dict[str, Any]]:
    md = eng.output_path(job, "md").read_text(encoding="utf-8")
    data = json.loads(eng.output_path(job, "json").read_text(encoding="utf-8"))
    return md, data


@pytest.fixture
def cfg(config: AppConfig) -> AppConfig:
    return config.model_copy(update={"vision_batch_size": 2, "api_concurrency": 2})


# ── the happy path ───────────────────────────────────────────────────────────
async def test_full_flow(cfg, sample_video) -> None:
    eng = engine(cfg)
    job = await eng.create(sample_video)
    assert job.status == JobStatus.CREATED and job.video and job.video.has_audio

    est = await eng.estimate(job.id)
    assert est.has_audio and est.scene_cuts == 3 and est.frames == 4
    assert est.total_api_calls > 0 and est.cost_usd is None  # no pricing.yaml

    events: list[str] = []
    job = await eng.run(job.id, on_event=lambda t, _d: events.append(t))
    assert job.status == JobStatus.COMPLETED, job.error
    assert all(s.status == "done" for s in job.stages.values())
    assert job.chapter_count == 2
    for needed in ("stage", "log", "transcript", "frame", "scene", "chapters", "synthesis"):
        assert needed in events, needed
    assert events[-1] == "done"

    md, data = result(eng, job)
    assert md.startswith("---\n") and "## Chapter 1:" in md and "## Chapter 2:" in md
    assert "Widjet" not in md and "Widget Frobnicator" in md  # correction applied
    assert "never said in the video" not in md  # non-verbatim quote dropped
    assert "quotes_dropped" in {w.code for w in job.warnings}

    chapters = data["chapters"]
    bounds = [(ch["title"], ch["start"], ch["end"]) for ch in chapters]
    assert validate_chapters(bounds, data["video"]["duration_s"]) == []
    assert chapters[0]["start"] == 0 and chapters[-1]["end"] == pytest.approx(
        data["video"]["duration_s"]
    )
    assert len(data["frames"]) == 4 and len(data["scenes"]) == 4
    segs = data["transcript"]["segments"]
    assert segs and all(s["end"] <= data["video"]["duration_s"] for s in segs)
    assert all(s["corrected_text"] for s in segs) and all(
        "Widjet" not in s["corrected_text"] for s in segs
    )
    assert any("Widjet" in s["raw_text"] for s in segs)  # raw preserved
    assert [s["id"] for s in segs] == list(range(len(segs)))
    assert check_timestamps(md, data["video"]["duration_s"]) == []

    assert md.count("## Chapter") == 2
    assert ".analysis.md" in eng.output_path(job, "md").name
    assert "Widget" in eng.output_path(job, "diff").read_text(encoding="utf-8")
    frames_dir = eng.store.frames_dir(job.id)
    assert (frames_dir / data["frames"][0]["name"]).is_file()

    eng.delete(job.id)
    assert not (cfg.jobs_dir / job.id).exists()
    with pytest.raises(JobNotFound):
        eng.load(job.id)


async def test_input_is_copied_into_the_job_directory(cfg, sample_video) -> None:
    eng = engine(cfg)
    job = await eng.create(sample_video)
    copy = eng.video_path(job)
    assert copy.is_file() and copy != sample_video
    assert copy.resolve().is_relative_to(eng.store.dir(job.id).resolve())
    assert copy.read_bytes() == sample_video.read_bytes()


# ── caching and resume ───────────────────────────────────────────────────────
async def test_rerun_repeats_no_paid_calls(cfg, sample_video) -> None:
    prov = bundle()
    eng = engine(cfg, prov)
    job = await run_new(eng, sample_video)
    before = (len(prov.transcriber.calls), prov.vision.calls, len(prov.text.calls))  # type: ignore[attr-defined]
    assert before[0] > 0 and before[1] > 0 and before[2] > 0
    job = await eng.run(job.id)
    assert job.status == JobStatus.COMPLETED
    assert (len(prov.transcriber.calls), prov.vision.calls, len(prov.text.calls)) == before  # type: ignore[attr-defined]
    assert all(s.cache_hit for s in job.stages.values())


async def test_changing_one_setting_reruns_only_its_dependents(cfg, sample_video) -> None:
    prov = bundle()
    eng = engine(cfg, prov)
    job = await run_new(eng, sample_video)
    t_calls, v_calls = len(prov.transcriber.calls), prov.vision.calls  # type: ignore[attr-defined]
    eng.update_settings(job.id, JobSettings(models=ModelOverrides(correct="another-model")))
    job = await eng.run(job.id)
    st = job.stages
    assert job.status == JobStatus.COMPLETED
    assert [
        st[StageName(s)].cache_hit for s in ("probe", "audio", "transcribe", "frames", "vision")
    ] == [True] * 5
    assert [st[StageName(s)].cache_hit for s in ("correct", "synthesize")] == [False, False]
    assert (len(prov.transcriber.calls), prov.vision.calls) == (t_calls, v_calls)  # type: ignore[attr-defined]
    assert job.settings.correct_model == "another-model"


async def test_force_stage_invalidates_downstream(cfg, sample_video) -> None:
    prov = bundle()
    eng = engine(cfg, prov)
    job = await run_new(eng, sample_video)
    n_vis = prov.vision.calls  # type: ignore[attr-defined]
    job = await eng.run(job.id, force={StageName.VISION})
    assert prov.vision.calls > n_vis  # type: ignore[attr-defined]
    assert job.stages[StageName.TRANSCRIBE].cache_hit is True


async def test_job_resumes_from_cached_stages_in_a_new_process(cfg, sample_video) -> None:
    job = await run_new(engine(cfg), sample_video)
    assert job.status == JobStatus.COMPLETED
    # simulate a crash mid-run: only some stage caches remain
    for stage in ("synthesize", "assemble"):
        shutil.rmtree(cfg.jobs_dir / job.id / "stages" / stage)
    (cfg.jobs_dir / job.id / "stages" / "correct").joinpath("result.json").unlink()
    fresh = bundle()
    job = await engine(cfg, fresh).run(job.id)  # a new Engine, as a new CLI process would be
    assert job.status == JobStatus.COMPLETED
    assert len(fresh.transcriber.calls) == 0 and fresh.vision.calls == 0  # type: ignore[attr-defined]
    assert job.stages[StageName.TRANSCRIBE].cache_hit and job.stages[StageName.VISION].cache_hit
    assert not job.stages[StageName.CORRECT].cache_hit
    # correct's *stage* result was lost but its per-window unit caches survived: no repaid calls
    assert "CorrectionOut" not in fresh.text.calls  # type: ignore[attr-defined]
    assert "ChapterProposalOut" in fresh.text.calls  # type: ignore[attr-defined]


# ── fail-soft and recovery ───────────────────────────────────────────────────
async def test_failed_vision_batch_is_flagged_not_fatal(cfg, sample_video) -> None:
    eng = engine(cfg, bundle(vision=FakeVision(fail_when=lambda req: req.frames[0].t < 1)))
    job = await run_new(eng, sample_video)
    assert job.status == JobStatus.COMPLETED
    assert "vision_gap" in {w.code for w in job.warnings}
    md, data = result(eng, job)
    assert len(data["scenes"]) == 2 and len(data["notes"]["failed_batches"]) == 1
    fb = data["notes"]["failed_batches"][0]
    assert fb["stage"] == "vision" and fb["t_start"] < 1
    assert "Failed batches (1)" in md and "simulated vision failure" in md


async def test_total_vision_failure_fails_the_stage_and_retry_recovers(cfg, sample_video) -> None:
    broken = bundle(vision=FakeVision(fail_when=lambda _r: True))
    job = await run_new(engine(cfg, broken), sample_video)
    assert job.status == JobStatus.FAILED and job.error and job.error.stage == StageName.VISION
    assert "every batch" in job.error.message
    assert job.stages[StageName.TRANSCRIBE].status == "done"
    assert job.stages[StageName.VISION].status == "failed"
    assert (cfg.jobs_dir / job.id / "stages" / "transcribe" / "result.json").is_file()
    # "fix" the provider and retry: transcription is not paid for again
    n_transcribe = len(broken.transcriber.calls)  # type: ignore[attr-defined]
    job = await engine(cfg, bundle(transcriber=broken.transcriber)).run(job.id)
    assert job.status == JobStatus.COMPLETED
    assert len(broken.transcriber.calls) == n_transcribe  # type: ignore[attr-defined]


async def test_provider_fatal_error_surfaces_message(cfg, sample_video) -> None:
    class Dead(FakeText):
        async def complete_json(self, *a: Any, **k: Any) -> Any:
            raise FatalProviderError(
                "OpenAI rejected the API key (invalid, revoked or for another endpoint).",
                code="invalid_api_key",
            )

    job = await run_new(engine(cfg, bundle(text=Dead())), sample_video)
    assert job.status == JobStatus.FAILED and job.error and job.error.code == "invalid_api_key"
    assert job.error.stage == StageName.CORRECT


@pytest.mark.parametrize(
    ("mode", "code"),
    [("gap", "chapters_repaired"), ("beyond", "chapters_repaired"), ("empty", "chapters_fallback")],
)
async def test_bad_chapter_proposals_are_repaired_or_replaced(
    cfg, sample_video, mode, code
) -> None:
    eng = engine(cfg, bundle(text=FakeText(proposal_mode=mode)))
    job = await run_new(eng, sample_video)
    assert job.status == JobStatus.COMPLETED
    assert code in {w.code for w in job.warnings}
    _, data = result(eng, job)
    bounds = [(ch["title"], ch["start"], ch["end"]) for ch in data["chapters"]]
    assert validate_chapters(bounds, data["video"]["duration_s"]) == []


async def test_failed_summary_calls_degrade_gracefully(cfg, sample_video) -> None:
    prov = bundle(
        text=FakeText(fail_stages={"ChapterDetailOut", "GlobalSynthesisOut", "CorrectionOut"})
    )
    eng = engine(cfg, prov)
    job = await run_new(eng, sample_video)
    assert job.status == JobStatus.COMPLETED
    codes = {w.code for w in job.warnings}
    assert {"correction_fallback", "chapter_summary_failed", "global_summary_failed"} <= codes
    _, data = result(eng, job)
    assert all(s["corrected_text"] == s["raw_text"] for s in data["transcript"]["segments"])


async def test_unhandled_stage_exception_is_reported_not_crashed(cfg, sample_video) -> None:
    class Boom(FakeVision):
        async def analyze(self, req: Any) -> Any:
            raise RuntimeError(f"bug with key {cfg.key_for('text')}")

    job = await run_new(engine(cfg, bundle(vision=Boom())), sample_video)
    assert job.status == JobStatus.FAILED and job.error and job.error.stage == StageName.VISION
    assert "RuntimeError" in job.error.message and "sk-test" not in job.model_dump_json()


# ── transcription capability fallback / no audio ─────────────────────────────
async def test_model_without_segment_timestamps_falls_back_to_chunk_mode(cfg, sample_video) -> None:
    eng = engine(cfg, bundle(transcriber=FakeTranscriber(reject_verbose_json=True)))
    job = await run_new(eng, sample_video)
    assert job.status == JobStatus.COMPLETED, job.error
    md, data = result(eng, job)
    assert data["transcript"]["timestamp_precision"] == "chunk"
    first = data["transcript"]["segments"][0]
    assert (first["start"], first["end"]) == (0.0, 24.0)
    assert "chunk boundaries" in md


async def test_video_without_audio_skips_transcription(cfg, silent_video) -> None:
    prov = bundle()
    eng = engine(cfg, prov)
    job = await run_new(eng, silent_video)
    assert job.status == JobStatus.COMPLETED, job.error
    st = job.stages
    assert [st[StageName(s)].status for s in ("audio", "transcribe", "correct")] == ["skipped"] * 3
    assert len(prov.transcriber.calls) == 0  # type: ignore[attr-defined]
    md, data = result(eng, job)
    assert "has_audio: false" in md and "no audio track" in md.lower()
    assert data["transcript"]["segments"] == [] and data["chapters"]


# ── inputs and settings ──────────────────────────────────────────────────────
async def test_corrupt_input_gives_a_clear_error_and_leaves_nothing(cfg, tmp_path) -> None:
    notes = tmp_path / "notes.mp4"
    notes.write_bytes(b"hello this is text" * 50)
    with pytest.raises(MediaError) as exc:
        await engine(cfg).create(notes)
    assert exc.value.code == "unsupported_container" and "notes.mp4" in exc.value.message
    assert list(cfg.jobs_dir.iterdir()) == []


async def test_truncated_media_gives_a_clear_error_and_leaves_nothing(cfg, tmp_path) -> None:
    broken = tmp_path / "broken.mp4"  # a valid MP4 signature, then nothing usable
    broken.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64)
    with pytest.raises(MediaError) as exc:
        await engine(cfg).create(broken)
    assert exc.value.code == "corrupt_media" and "broken.mp4" in exc.value.message
    assert list(cfg.jobs_dir.iterdir()) == []


async def test_filenames_are_sanitised(cfg, sample_video) -> None:
    eng = engine(cfg)
    job = await eng.create(sample_video, filename="../../etc/pass wd?.mp4")
    assert job.video
    name = job.video.filename
    assert "/" not in name and ".." not in name.replace("...", "")
    assert (cfg.jobs_dir / job.id / "upload" / name).is_file()


async def test_per_job_settings_are_resolved(cfg, sample_video) -> None:
    settings = JobSettings(
        frame_cap=3, context="Frobnicator", models=ModelOverrides(vision="my-vision")
    )
    job = await engine(cfg).create(sample_video, settings)
    s = job.settings
    assert s.frame_cap == 3 and s.vision_model == "my-vision" and s.context == "Frobnicator"
    assert s.transcribe_model == cfg.models.transcribe  # unspecified -> config default


async def test_frame_cap_is_respected(cfg, sample_video) -> None:
    eng = engine(cfg)
    job = await run_new(eng, sample_video, JobSettings(frame_cap=2))
    _, data = result(eng, job)
    assert len(data["frames"]) == 2 and data["frames"][0]["reason"] == "first"


async def test_unknown_and_unsafe_job_ids_are_not_found(cfg) -> None:
    eng = engine(cfg)
    for job_id in ("doesnotexist", "000000000000", "../../etc", "..%2Fjob"):
        with pytest.raises(JobNotFound):
            eng.load(job_id)


async def test_outputs_are_not_ready_before_a_run(cfg, sample_video) -> None:
    eng = engine(cfg)
    job = await eng.create(sample_video)
    with pytest.raises(Exception, match="no 'md' output") as exc:
        eng.output_path(job, "md")
    assert getattr(exc.value, "code", None) == "not_ready"


# ── cancellation ─────────────────────────────────────────────────────────────
async def test_cancel_stops_the_job_and_it_can_be_resumed(cfg, sample_video) -> None:
    prov = bundle(vision=FakeVision(delay=2.0))
    eng = engine(cfg, prov)
    job = await eng.create(sample_video)
    task = asyncio.create_task(eng.run(job.id))
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert eng.load(job.id).status == JobStatus.CANCELLED
    prov.vision.delay = 0  # type: ignore[attr-defined]
    job = await eng.run(job.id)
    assert job.status == JobStatus.COMPLETED
    assert job.stages[StageName.TRANSCRIBE].cache_hit


# ── key handling ─────────────────────────────────────────────────────────────
async def test_missing_key_is_a_clear_cold_error(tmp_path, sample_video) -> None:
    home = tmp_path / "h"
    home.mkdir()
    nokey = load_config(home, env={})
    eng = Engine(nokey)  # default factory: OpenAI providers
    job = await eng.create(sample_video)
    est = await eng.estimate(job.id)  # estimate is free/local and works without a key
    assert est.frames == 4
    with pytest.raises(FatalProviderError) as exc:
        await eng.run(job.id)
    assert exc.value.code == "missing_api_key" and "OPENAI_API_KEY" in exc.value.message


async def test_secret_never_appears_in_outputs_logs_or_files(cfg, sample_video, caplog) -> None:
    key = cfg.key_for("text")
    assert key
    caplog.set_level("DEBUG")
    eng = engine(cfg)
    job = await run_new(eng, sample_video)
    assert job.status == JobStatus.COMPLETED
    assert key not in job.model_dump_json() and "sk-test" not in cfg.model_dump_json()
    for f in cfg.data_root.rglob("*"):
        if f.is_file() and f.suffix in {".json", ".md", ".jsonl", ".diff", ".txt"}:
            assert key not in f.read_text(errors="ignore"), f
    assert key not in caplog.text


def test_job_status_enum_matches_contract() -> None:
    assert {s.value for s in JobStatus} == {
        "created",
        "queued",
        "running",
        "completed",
        "failed",
        "cancelled",
    }
    assert ProviderError.status == 502


def test_log_redaction_keeps_structured_records_intact(config) -> None:
    import logging

    from frame_ingest.errors import install_log_redaction

    key = config.key_for("text")
    original = logging.getLogRecordFactory()
    try:
        install_log_redaction([key])
        make = logging.getLogRecordFactory()
        # formatters that read record.args: records without secrets must keep them
        plain = make(
            "access", 20, "p", 1, '%s - "%s %s" %d', ("127.0.0.1:1", "POST", "/x", 201), None
        )
        assert plain.args == ("127.0.0.1:1", "POST", "/x", 201)
        leaky = make("x", 40, "p", 1, "auth failed for %s", (key,), None)
        assert key not in leaky.getMessage() and "[redacted]" in leaky.getMessage()
    finally:
        logging.setLogRecordFactory(original)
