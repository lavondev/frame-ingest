"""Small pieces shared by the agent-mode modules (`prepare`, the audio guarantee, `assemble`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from frame_ingest.engine import Engine
from frame_ingest.errors import FrameIngestError
from frame_ingest.models import Job, StageName, VideoInfo
from frame_ingest.pipeline.context import PipelineContext
from frame_ingest.profiles import fake_bundle


class PrepareError(FrameIngestError):
    code = "prepare_failed"
    status = 400


def agent_dir(job_dir: Path) -> Path:
    return job_dir / "agent"


def video_of(job: Job) -> VideoInfo:
    if job.video is None:
        raise PrepareError(f"Job '{job.id}' has no probed input video.", code="corrupt_job")
    return job.video


def agent_context(engine: Engine, job: Job, emit: Any = None) -> PipelineContext:
    """A pipeline context for running single stages outside the runner. Its providers are
    fakes: a stage that needs a real one gets it swapped in explicitly."""
    extra: dict[str, Any] = {"emit_cb": emit} if emit else {}
    return PipelineContext(
        job_id=job.id,
        job_dir=engine.store.dir(job.id),
        config=engine.config,
        settings=job.settings,
        video=video_of(job),
        video_path=engine.video_path(job),
        providers=fake_bundle(),
        current_stage=StageName.FRAMES,
        **extra,
    )
