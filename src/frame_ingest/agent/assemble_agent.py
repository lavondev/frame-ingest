"""`assemble` for agent mode: validate what the agent wrote, then build the document in code.

Validation reuses the pipeline's guards (exact frame and segment id sets, length guard on
corrections, verbatim quotes, contiguous chapters covering [0, duration]). Problems that the
agent can fix are returned as a list and nothing is written; softer issues (a non-verbatim
quote, an implausible correction) are dropped with a warning, exactly as in pipeline mode.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from frame_ingest.agent.prepare import (
    agent_dir,
    load_registry,
    load_transcript,
)
from frame_ingest.agent.schemas import AgentSynthesis
from frame_ingest.engine import Engine
from frame_ingest.errors import FrameIngestError
from frame_ingest.guard.paths import PathRejected, job_jail, read_bytes_nofollow
from frame_ingest.llm_schemas import CorrectionOut, VisionBatchOut
from frame_ingest.models import (
    Chapter,
    Entity,
    FrameAnalysis,
    GlossaryEntry,
    Job,
    JobStatus,
    Quote,
    Segment,
    StageName,
    StageStatus,
    Transcript,
    VideoSynthesis,
)
from frame_ingest.pipeline import assemble as assemble_stage
from frame_ingest.pipeline.context import PipelineContext
from frame_ingest.pipeline.correct import (
    CorrectionResult,
    accept_text,
    render_diff,
    validate_correction,
)
from frame_ingest.pipeline.frames import FramesResult
from frame_ingest.pipeline.synthesize import (
    SynthesisResult,
    clean_tags,
    frames_in,
    merge_entities,
    segments_in,
    validate_chapters,
    verify_quotes,
)
from frame_ingest.pipeline.textutil import dedupe_keep_order
from frame_ingest.pipeline.transcribe import TranscribeResult
from frame_ingest.pipeline.vision import VisionResult, to_analysis
from frame_ingest.profiles import fake_bundle

MAX_OUT_BYTES = 2 * 1024 * 1024
MAX_LISTED = 20
M = TypeVar("M", bound=BaseModel)


class ValidationFailed(FrameIngestError):
    code = "validation_failed"
    status = 422

    def __init__(self, problems: list[dict[str, str]]) -> None:
        super().__init__(f"{len(problems)} problem(s) in the agent outputs; fix them and re-run.")
        self.problems = problems


def _problem(file: str, code: str, message: str) -> dict[str, str]:
    return {"file": file, "code": code, "message": message}


def _load(path: Path, model: type[M], rel: str, problems: list[dict[str, str]]) -> M | None:
    try:
        if path.stat().st_size > MAX_OUT_BYTES:
            problems.append(_problem(rel, "too_large", f"file exceeds {MAX_OUT_BYTES} bytes"))
            return None
        raw = json.loads(read_bytes_nofollow(path).decode("utf-8"))
        return model.model_validate(raw)
    except PathRejected as exc:
        problems.append(_problem(rel, "bad_file", exc.message))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        problems.append(_problem(rel, "bad_json", f"not valid JSON: {type(exc).__name__}"))
    except ValidationError as exc:
        for err in exc.errors()[:5]:
            where = ".".join(str(p) for p in err["loc"]) or "(root)"
            problems.append(_problem(rel, "bad_schema", f"{where}: {err['msg']}"))
    return None


def _check_vision(
    out_dir: Path, registry_frames: list[Any], problems: list[dict[str, str]]
) -> list[FrameAnalysis]:
    info = {f.name: f for f in registry_frames}
    files = sorted((out_dir / "vision").glob("*.json"))
    if not files:
        problems.append(_problem("vision/", "missing", "no vision/*.json files were written"))
        return []
    got: dict[str, FrameAnalysis] = {}
    for path in files:
        rel = f"vision/{path.name}"
        batch = _load(path, VisionBatchOut, rel, problems)
        if batch is None:
            continue
        for item in batch.frames:
            if item.frame not in info:
                problems.append(
                    _problem(rel, "unknown_frame", f"unknown frame {item.frame[:60]!r}")
                )
            elif item.frame in got:
                problems.append(_problem(rel, "duplicate_frame", f"{item.frame} analysed twice"))
            elif not item.scene_description.strip():
                problems.append(_problem(rel, "empty", f"{item.frame}: scene_description is empty"))
            else:
                got[item.frame] = to_analysis(item, info[item.frame])
    missing = [n for n in info if n not in got]
    if missing:
        listed = ", ".join(missing[:MAX_LISTED]) + (" ..." if len(missing) > MAX_LISTED else "")
        problems.append(
            _problem("vision/", "missing_frames", f"{len(missing)} frame(s) not analysed: {listed}")
        )
    return [got[n] for n in info if n in got]


def _check_corrections(
    out_dir: Path,
    segments: list[Segment],
    ctx: PipelineContext,
    problems: list[dict[str, str]],
) -> CorrectionResult:
    ctx.current_stage = StageName.CORRECT
    segs = [s.model_copy() for s in segments]
    path = out_dir / "corrections.json"
    if not segs:
        if path.exists():
            problems.append(_problem("corrections.json", "unexpected", "there is no transcript"))
        return CorrectionResult()
    if not path.exists():
        ctx.warn("corrections_skipped", "No corrections were submitted; raw transcript kept.")
        for s in segs:
            s.corrected_text = s.raw_text
    else:
        out = _load(path, CorrectionOut, "corrections.json", problems)
        if out is not None:
            texts, err = validate_correction(segs, out)
            if err:
                problems.append(_problem("corrections.json", "bad_ids", err))
            else:
                rejected = 0
                for s in segs:
                    if accept_text(s.raw_text, texts[s.id]):
                        s.corrected_text = texts[s.id]
                    else:
                        s.corrected_text = s.raw_text
                        rejected += 1
                if rejected:
                    ctx.warn(
                        "correction_rejected", f"{rejected} implausible correction(s) ignored."
                    )
    diff, changed = render_diff(segs, ctx.video.duration_s)
    warnings, _, _ = ctx.drain()
    return CorrectionResult(segments=segs, changed=changed, diff_log=diff, warnings=warnings)


def _check_synthesis(
    out_dir: Path,
    segments: list[Segment],
    scenes: list[FrameAnalysis],
    ctx: PipelineContext,
    problems: list[dict[str, str]],
) -> SynthesisResult | None:
    ctx.current_stage = StageName.SYNTHESIZE
    syn = _load(out_dir / "synthesis.json", AgentSynthesis, "synthesis.json", problems)
    if syn is None:
        return None
    d = ctx.video.duration_s
    bounds = [(c.title, c.start, c.end) for c in syn.chapters]
    for msg in validate_chapters(bounds, d):
        problems.append(_problem("synthesis.json", "bad_chapters", msg))
    if len(syn.chapters) > ctx.config.max_chapters:
        problems.append(
            _problem("synthesis.json", "too_many_chapters", f"at most {ctx.config.max_chapters}")
        )
    for field in ("title", "tldr", "abstract"):
        if not getattr(syn, field).strip():
            problems.append(_problem("synthesis.json", "empty", f"{field} is empty"))
    for i, c in enumerate(syn.chapters, 1):
        if not c.title.strip() or not c.summary.strip():
            problems.append(
                _problem("synthesis.json", "empty", f"chapter {i}: title/summary empty")
            )
    if problems:
        return None

    n = len(syn.chapters)
    chapters: list[Chapter] = []
    for i, c in enumerate(syn.chapters):
        last = i == n - 1
        cscenes = frames_in(scenes, c.start, c.end, last)
        quotes, dropped = verify_quotes(
            [Quote(t=min(max(q.t, 0.0), d), text=q.text) for q in c.quotes],
            segments_in(segments, c.start, c.end, last),
            c.start,
            c.end,
        )
        if dropped:
            ctx.warn(
                "quotes_dropped",
                f"{dropped} non-verbatim quote(s) removed from chapter {i + 1}.",
                c.start,
                c.end,
            )
        chapters.append(
            Chapter(
                index=i + 1,
                id=f"ch-{i + 1:02d}",
                title=c.title.strip(),
                start=c.start,
                end=c.end,
                summary=c.summary.strip(),
                key_points=[k.strip() for k in c.key_points if k.strip()][:8],
                quotes=quotes,
                entities=merge_entities(
                    [
                        Entity(name=e.name.strip(), kind=e.kind)
                        for e in c.entities
                        if e.name.strip()
                    ],
                    [e for sc in cscenes for e in sc.entities],
                ),
                decisions_claims=[x.strip() for x in c.decisions_claims if x.strip()],
                visual_description=c.visual_summary.strip()
                or " ".join(sc.scene_description for sc in cscenes[:3]),
                on_screen_text=dedupe_keep_order(t for sc in cscenes for t in sc.on_screen_text),
                frames=[sc.frame for sc in cscenes],
            )
        )
    synthesis = VideoSynthesis(
        title=syn.title.strip(),
        tldr=syn.tldr.strip(),
        abstract=syn.abstract.strip(),
        glossary=[
            GlossaryEntry(
                term=g.term.strip(),
                definition=g.definition.strip(),
                first_seen=min(max(g.first_seen_s, 0.0), d) if g.first_seen_s is not None else None,
            )
            for g in syn.glossary
            if g.term.strip()
        ],
        open_questions=[q.strip() for q in syn.open_questions if q.strip()],
        tags=clean_tags(syn.tags),
    )
    warnings, _, _ = ctx.drain()
    return SynthesisResult(chapters=chapters, synthesis=synthesis, warnings=warnings)


async def assemble_job(engine: Engine, job_id: str, *, metrics: bool = False) -> Job:
    """Validate the agent outputs of a job and write the document. Raises ValidationFailed."""
    job = engine.load(job_id)
    job_dir = engine.store.dir(job.id)
    registry = load_registry(job_dir)
    transcript = load_transcript(job_dir)
    if registry is None or transcript is None or job.video is None:
        raise FrameIngestError(
            f"Job '{job.id}' has no evidence pack. Run prepare first.", code="not_prepared"
        )
    out_dir = agent_dir(job_dir) / "out"
    captions = transcript.source in {"captions", "auto-captions"}
    settings = job.settings.model_copy(
        update={
            "transcribe_model": transcript.source if captions else "none",
            "vision_model": "agent",
            "correct_model": "agent",
            "synthesize_model": "agent",
        }
    )
    ctx = PipelineContext(
        job_id=job.id,
        job_dir=job_dir,
        config=engine.config,
        settings=settings,
        video=job.video,
        video_path=engine.video_path(job),
        providers=fake_bundle(),
        source_url=job.source_url,
        retrieved_at=job.retrieved_at,
        metrics=metrics,
    )
    problems: list[dict[str, str]] = []
    with job_jail(job_dir):
        scenes = _check_vision(out_dir, registry.frames, problems)
        corrected = _check_corrections(out_dir, transcript.segments, ctx, problems)
        segments = corrected.segments or transcript.segments
        synth = _check_synthesis(out_dir, segments, scenes, ctx, problems)
        if problems or synth is None:
            raise ValidationFailed(problems)

        ctx.results = {
            StageName.TRANSCRIBE: TranscribeResult(
                transcript=Transcript(
                    model=transcript.source if captions else None,
                    timestamp_precision=transcript.timestamp_precision,
                    source=transcript.source,
                    segments=transcript.segments,
                )
            ),
            StageName.FRAMES: FramesResult(
                frames=registry.frames,
                scene_cuts=registry.scene_cuts,
                candidates=len(registry.frames),
                warnings=registry.warnings,
            ),
            StageName.VISION: VisionResult(scenes=scenes),
            StageName.CORRECT: corrected,
            StageName.SYNTHESIZE: synth,
        }
        ctx.ran = [StageName.PROBE, StageName.FRAMES, StageName.VISION]
        ctx.ran += [StageName.CORRECT, StageName.SYNTHESIZE, StageName.ASSEMBLE]
        ctx.skipped = [StageName.AUDIO] + ([] if captions else [StageName.TRANSCRIBE])
        ctx.current_stage = StageName.ASSEMBLE
        res = await assemble_stage.run(ctx, mode="agent")

    job.status = JobStatus.COMPLETED
    job.error = None
    job.outputs = {"md": res.md_file, "json": res.json_file}
    if res.diff_file:
        job.outputs["diff"] = res.diff_file
    job.chapter_count = len(synth.chapters)
    job.warnings = ctx.all_warnings() + ctx.current_warnings()
    for name in ctx.ran:
        job.stages[name].status = StageStatus.DONE
    for name in ctx.skipped:
        job.stages[name].status = StageStatus.SKIPPED
    engine.save(job)
    return job
