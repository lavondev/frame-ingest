"""`assemble` for agent mode: validate what the agent wrote, then build the document in code.

Validation reuses the pipeline's guards (exact frame and segment id sets, length guard on
corrections, verbatim quotes, contiguous chapters covering [0, duration]). Problems that the
agent can fix are returned as a list and nothing is written; softer issues (a non-verbatim
quote, an implausible correction) are dropped with a warning, exactly as in pipeline mode.
"""

from __future__ import annotations

from pathlib import Path

from frame_ingest.agent.audio import frames_only_note, load_status
from frame_ingest.agent.checks import (
    M,
    Problem,
    check_corrections,
    check_synthesis,
    check_vision,
    find_placeholders,
    missing_frames_problem,
    parse,
    placeholder_problem,
    problem,
    read_output,
)
from frame_ingest.agent.prepare import (
    agent_dir,
    load_registry,
    load_transcript,
)
from frame_ingest.agent.schemas import AgentSynthesis
from frame_ingest.engine import Engine
from frame_ingest.errors import FrameIngestError
from frame_ingest.guard.paths import job_jail
from frame_ingest.llm_schemas import CorrectionOut, VisionBatchOut
from frame_ingest.models import (
    Chapter,
    Entity,
    FrameAnalysis,
    FrameInfo,
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
    render_diff,
)
from frame_ingest.pipeline.frames import FramesResult
from frame_ingest.pipeline.synthesize import (
    SynthesisResult,
    clean_tags,
    frames_in,
    merge_entities,
    segments_in,
    verify_quotes,
)
from frame_ingest.pipeline.textutil import dedupe_keep_order
from frame_ingest.pipeline.transcribe import TranscribeResult
from frame_ingest.pipeline.vision import VisionResult
from frame_ingest.profiles import fake_bundle


class ValidationFailed(FrameIngestError):
    code = "validation_failed"
    status = 422

    def __init__(self, problems: list[Problem]) -> None:
        super().__init__(f"{len(problems)} problem(s) in the agent outputs; fix them and re-run.")
        self.problems = problems


def _load(path: Path, model: type[M], rel: str, problems: list[Problem]) -> M | None:
    raw = read_output(path, rel, problems)
    if raw is None:
        return None
    todo = find_placeholders(raw)
    if todo:
        problems.append(placeholder_problem(rel, todo))
    return parse(raw, model, rel, problems)


def _check_vision(
    out_dir: Path, registry_frames: list[FrameInfo], problems: list[Problem]
) -> list[FrameAnalysis]:
    info = {f.name: f for f in registry_frames}
    files = sorted((out_dir / "vision").glob("*.json"))
    if not files:
        problems.append(problem("vision/", "missing", "no vision/*.json files were written"))
        return []
    got: dict[str, FrameAnalysis] = {}
    seen: dict[str, str] = {}
    for path in files:
        rel = f"vision/{path.name}"
        batch = _load(path, VisionBatchOut, rel, problems)
        if batch is None:
            continue
        found, file_problems = check_vision(batch, rel, info, seen)
        got.update(found)
        problems += file_problems
    missing = missing_frames_problem(n for n in info if n not in got)
    if missing:
        problems.append(missing)
    return [got[n] for n in info if n in got]


def _check_corrections(
    out_dir: Path,
    segments: list[Segment],
    ctx: PipelineContext,
    problems: list[Problem],
) -> CorrectionResult:
    ctx.current_stage = StageName.CORRECT
    segs = [s.model_copy() for s in segments]
    path = out_dir / "corrections.json"
    if not segs:
        if path.exists():
            problems.append(problem("corrections.json", "unexpected", "there is no transcript"))
        return CorrectionResult()
    if not path.exists():
        ctx.warn("corrections_skipped", "No corrections were submitted; raw transcript kept.")
        for s in segs:
            s.corrected_text = s.raw_text
    else:
        out = _load(path, CorrectionOut, "corrections.json", problems)
        if out is not None:
            texts, found, implausible = check_corrections(out, "corrections.json", segs)
            problems += found
            if texts is not None:
                for s in segs:
                    s.corrected_text = s.raw_text if s.id in implausible else texts[s.id]
                if implausible:
                    ctx.warn(
                        "correction_rejected",
                        f"{len(implausible)} implausible correction(s) ignored.",
                    )
    diff, changed = render_diff(segs, ctx.video.duration_s)
    warnings, _, _ = ctx.drain()
    return CorrectionResult(segments=segs, changed=changed, diff_log=diff, warnings=warnings)


def _check_synthesis(
    out_dir: Path,
    segments: list[Segment],
    scenes: list[FrameAnalysis],
    ctx: PipelineContext,
    problems: list[Problem],
) -> SynthesisResult | None:
    ctx.current_stage = StageName.SYNTHESIZE
    syn = _load(out_dir / "synthesis.json", AgentSynthesis, "synthesis.json", problems)
    if syn is None:
        return None
    d = ctx.video.duration_s
    problems += check_synthesis(syn, "synthesis.json", d, ctx.config.max_chapters)
    if problems:
        return None

    n = len(syn.chapters)
    chapters: list[Chapter] = []
    quotes_dropped = 0
    for i, c in enumerate(syn.chapters):
        last = i == n - 1
        cscenes = frames_in(scenes, c.start, c.end, last)
        quotes, dropped = verify_quotes(
            [Quote(t=min(max(q.t, 0.0), d), text=q.text) for q in c.quotes],
            segments_in(segments, c.start, c.end, last),
            c.start,
            c.end,
        )
        quotes_dropped += dropped
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
    return SynthesisResult(
        chapters=chapters, synthesis=synthesis, warnings=warnings, quotes_dropped=quotes_dropped
    )


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
    audio = load_status(job_dir)
    if audio is not None and audio.status == "needs_decision":
        raise FrameIngestError(
            f"Job '{job.id}' is waiting for the user to decide what to do about the audio: "
            f"{audio.message} Run prepare (or ingest) again with --captions, "
            "--allow-frames-only, or --cloud-speech --allow-egress, as the user chooses.",
            code="needs_decision",
        )
    no_transcript_note = (
        frames_only_note(audio) if audio is not None and audio.status == "frames_only" else None
    )
    out_dir = agent_dir(job_dir) / "out"
    transcribed = transcript.source == "asr"
    settings = job.settings.model_copy(
        update={
            "transcribe_model": (transcript.model or transcript.source)
            if transcript.source != "none"
            else "none",
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
                    model=transcript.model if transcript.source != "none" else None,
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
        if transcribed:
            ctx.ran.insert(1, StageName.AUDIO)
            ctx.ran.insert(2, StageName.TRANSCRIBE)
        else:
            ctx.skipped = [StageName.AUDIO, StageName.TRANSCRIBE]
        ctx.current_stage = StageName.ASSEMBLE
        res = await assemble_stage.run(ctx, mode="agent", no_transcript_note=no_transcript_note)

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
