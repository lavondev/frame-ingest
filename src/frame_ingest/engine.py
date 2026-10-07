"""Run the pipeline on a local video file. This is the library entry point the CLI wraps.

It replaces faircopy's FastAPI JobManager: no server, no event hub, no remote sync. A job is a
directory under <home>/jobs/<job_id>/ holding a copy of the input, job.json (status, per-stage
state, warnings, usage) and the stage caches. Running a job again resumes from the first stage
whose cache is missing, so nothing already paid for is repeated.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from frame_ingest.budget import Budget
from frame_ingest.capabilities import CapabilityMemo
from frame_ingest.config import AppConfig, Pricing, known_caps, load_pricing, resolve_settings
from frame_ingest.errors import FrameIngestError, redact
from frame_ingest.guard.limits import check_disk, check_size, check_video
from frame_ingest.guard.media import check_container
from frame_ingest.guard.paths import job_jail, open_source_nofollow, require_regular_file
from frame_ingest.models import (
    Estimate,
    EventType,
    Job,
    JobError,
    JobSettings,
    JobStatus,
    StageName,
    StageStatus,
    VideoInfo,
    new_stage_states,
    utcnow,
)
from frame_ingest.pipeline.context import PipelineContext
from frame_ingest.pipeline.estimate import build_estimate
from frame_ingest.pipeline.probe import probe_video
from frame_ingest.pipeline.runner import StageFailed, StageHooks, run_pipeline
from frame_ingest.providers.base import ProviderBundle, TranscriberCaps
from frame_ingest.storage import JobStore, safe_filename

log = logging.getLogger("frame_ingest.engine")
ProviderFactory = Callable[[], ProviderBundle]
EventCallback = Callable[[EventType, dict[str, Any]], None]
_COPY_CHUNK = 1024 * 1024


def _noop(_type: EventType, _data: dict[str, Any]) -> None:
    return None


class _Hooks(StageHooks):
    """Mirrors per-stage state into job.json as the pipeline runs."""

    def __init__(self, engine: Engine, job: Job, ctx: PipelineContext, emit: EventCallback):
        self.engine, self.job, self.ctx, self.emit = engine, job, ctx, emit

    def _publish(self, name: StageName) -> None:
        self.emit("stage", self.job.stages[name].model_dump(mode="json"))

    def started(self, stage: StageName) -> None:
        st = self.job.stages[stage]
        st.status, st.started_at, st.finished_at = StageStatus.RUNNING, utcnow(), None
        st.progress, st.units_done, st.units_total, st.error, st.cache_hit = 0.0, 0, 0, None, False
        self._publish(stage)
        self.engine.save(self.job)

    def finished(
        self, stage: StageName, *, cache_hit: bool, skipped: bool, message: str | None
    ) -> None:
        st = self.job.stages[stage]
        st.status = StageStatus.SKIPPED if skipped else StageStatus.DONE
        st.progress, st.cache_hit, st.finished_at = 1.0, cache_hit, utcnow()
        st.started_at = st.started_at or st.finished_at
        if message:
            st.message = message
        self.job.warnings = self.ctx.all_warnings()
        self.job.usage = self.ctx.all_usage()
        self._publish(stage)
        if cache_hit and not skipped:
            self.ctx.log(f"{stage.value}: reused cached result")
        self.engine.save(self.job)

    def failed(self, stage: StageName, code: str, message: str) -> None:
        st = self.job.stages[stage]
        st.status, st.error, st.finished_at = StageStatus.FAILED, message, utcnow()
        self.job.warnings = self.ctx.all_warnings() + self.ctx.current_warnings()
        self.job.usage = self.ctx.all_usage()
        self._publish(stage)

    def progress(self, stage: StageName, done: int, total: int, message: str | None) -> None:
        st = self.job.stages[stage]
        st.units_done, st.units_total = done, total
        st.progress = min(1.0, done / total) if total else 0.0
        if message:
            st.message = message


def _video(job: Job) -> VideoInfo:
    if job.video is None:
        raise FrameIngestError(f"Job '{job.id}' has no probed input video.", code="corrupt_job")
    return job.video


def _copy_and_hash(src: Path, dst: Path) -> tuple[int, str]:
    sha = hashlib.sha256()
    size = 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(open_source_nofollow(src), "rb") as fin, dst.open("xb") as fout:
        while chunk := fin.read(_COPY_CHUNK):
            sha.update(chunk)
            fout.write(chunk)
            size += len(chunk)
    return size, sha.hexdigest()


class Engine:
    def __init__(
        self,
        config: AppConfig,
        provider_factory: ProviderFactory | None = None,
        *,
        pricing: Pricing | None = None,
        memo: CapabilityMemo | None = None,
    ) -> None:
        self.config = config
        self.store = JobStore(config.jobs_dir)
        self.memo = memo or CapabilityMemo(config.data_root / "capabilities.json")
        self.pricing = pricing if pricing is not None else load_pricing(config.home)
        self._factory = provider_factory or self._openai_factory
        self._providers: ProviderBundle | None = None

    def _openai_factory(self) -> ProviderBundle:
        from frame_ingest.providers.openai_client import build_openai_providers

        return build_openai_providers(self.config, self.memo)

    # ── providers ───────────────────────────────────────────────────────────────
    def providers(self) -> ProviderBundle:
        if self._providers is None:
            self._providers = self._factory()
        return self._providers

    def caps_for(self, model: str) -> TranscriberCaps:
        if self._providers is not None:
            return self._providers.transcriber.caps_for(model)
        caps = known_caps(model)
        if not self.memo.get(self.config.base_url_for("transcribe"), model, "segment_timestamps"):
            caps.segment_timestamps = False
        return caps

    # ── jobs ────────────────────────────────────────────────────────────────────
    def save(self, job: Job) -> None:
        job.updated_at = utcnow()
        self.store.save_job(job)

    def load(self, job_id: str) -> Job:
        return self.store.load_job(job_id)

    def video_path(self, job: Job) -> Path:
        return self.store.video_path(job.id, _video(job).filename)

    async def create(
        self,
        source: Path,
        settings: JobSettings | None = None,
        *,
        filename: str | None = None,
        profile: str | None = None,
    ) -> Job:
        """Copy `source` into a new job directory, probe it and record the job.

        The pipeline only ever reads the copy inside the job directory.
        """
        require_regular_file(source, what="input")
        name = safe_filename(filename or source.name)
        check_size(source.stat().st_size, self.config)
        check_container(source, what=name)  # cheap early refusal before copying a large file
        check_disk(self.store.root, source.stat().st_size)
        job_id = self.store.new_id()
        dest = self.store.video_path(job_id, name)
        try:
            with job_jail(self.store.dir(job_id)):
                size, sha = await asyncio.to_thread(_copy_and_hash, source, dest)
                check_size(size, self.config)
                check_container(dest, what=name)  # the copy is what ffmpeg will read
                video = await probe_video(dest, filename=name, size_bytes=size, sha256=sha)
                check_video(video, self.config)
        except BaseException:
            shutil.rmtree(self.store.root / job_id, ignore_errors=True)
            raise
        now = utcnow()
        job = Job(
            id=job_id,
            created_at=now,
            updated_at=now,
            profile=profile,
            status=JobStatus.CREATED,
            settings=resolve_settings(self.config, settings),
            video=video,
            stages=new_stage_states(),
        )
        self.save(job)
        return job

    def update_settings(self, job_id: str, settings: JobSettings) -> Job:
        job = self.load(job_id)
        job.settings = resolve_settings(self.config, settings)
        job.estimate = None
        self.save(job)
        return job

    async def estimate(self, job_id: str, settings: JobSettings | None = None) -> Estimate:
        """Project frames, calls and tokens. Local only: no provider is contacted."""
        job = self.update_settings(job_id, settings) if settings else self.load(job_id)
        with job_jail(self.store.dir(job_id)):
            est = await build_estimate(
                self.config,
                job.settings,
                _video(job),
                self.store.dir(job_id),
                self.video_path(job),
                self.caps_for,
                self.pricing,
            )
        job.estimate = est
        self.save(job)
        return est

    async def run(
        self,
        job_id: str,
        *,
        force: set[StageName] | None = None,
        on_event: EventCallback | None = None,
        budget: Budget | None = None,
    ) -> Job:
        """Run (or resume) a job to completion. Returns the job with its final status.

        A failing stage leaves the job FAILED with `job.error` set; earlier stage results stay
        cached. Cancellation marks the job CANCELLED and re-raises.
        """
        emit = on_event or _noop
        job = self.load(job_id)
        providers = self.providers()  # FatalProviderError (e.g. missing key) propagates
        video = _video(job)
        job.status = JobStatus.RUNNING
        job.error = None
        job.warnings = []
        job.stages = new_stage_states()
        self.save(job)

        ctx = PipelineContext(
            job_id=job.id,
            job_dir=self.store.dir(job.id),
            config=self.config,
            settings=job.settings,
            video=video,
            video_path=self.video_path(job),
            providers=providers,
            budget=budget,
            emit_cb=emit,
        )
        hooks = _Hooks(self, job, ctx, emit)
        ctx.progress_cb = hooks.progress
        try:
            with job_jail(self.store.dir(job.id)):
                await run_pipeline(ctx, hooks, force=force or set())
            assemble_res = ctx.results[StageName.ASSEMBLE]
            job.status = JobStatus.COMPLETED
            job.outputs = {"md": assemble_res.md_file, "json": assemble_res.json_file}
            if assemble_res.diff_file:
                job.outputs["diff"] = assemble_res.diff_file
            job.chapter_count = len(ctx.results[StageName.SYNTHESIZE].chapters)
            job.warnings = ctx.all_warnings()
            job.usage = ctx.all_usage()
        except StageFailed as exc:
            job.status = JobStatus.FAILED
            job.error = JobError(stage=exc.stage, code=exc.code, message=exc.message)
            emit("error", job.error.model_dump(mode="json"))
        except asyncio.CancelledError:
            self._mark_cancelled(job)
            emit("done", {"status": job.status.value})
            raise
        except Exception as exc:
            log.exception("Job %s crashed", job.id)
            job.status = JobStatus.FAILED
            job.error = JobError(
                code="pipeline_failed",
                message=redact(
                    f"Unexpected error: {type(exc).__name__}: {exc}",
                    self.config.key_for("transcribe"),
                    self.config.key_for("vision"),
                    self.config.key_for("text"),
                ),
            )
            emit("error", job.error.model_dump(mode="json"))
        self.save(job)
        emit("done", {"status": job.status.value})
        return job

    def _mark_cancelled(self, job: Job) -> None:
        job.status = JobStatus.CANCELLED
        for st in job.stages.values():
            if st.status == StageStatus.RUNNING:
                st.status, st.error = StageStatus.FAILED, "Cancelled."
        self.save(job)

    def output_path(self, job: Job, key: str) -> Path:
        """Absolute path of a finished output ("md", "json" or "diff")."""
        rel = job.outputs.get(key)
        path = self.store.dir(job.id) / rel if rel else None
        if path is None or not path.is_file():
            raise FrameIngestError(f"Job '{job.id}' has no '{key}' output yet.", code="not_ready")
        return path

    def delete(self, job_id: str) -> None:
        self.store.delete(job_id)
