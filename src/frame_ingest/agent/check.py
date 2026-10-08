"""`check`: validate one agent file (or all of a job's) in milliseconds, with the same checks
`assemble` runs, so the agent fixes problems one small file at a time.

A file is `ok`, `incomplete` (only `TODO:` placeholders left) or `invalid` (anything else).
Only files inside a job's `agent/out/` directory are read, so `check` cannot be pointed at an
unrelated file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frame_ingest.agent.audio import load_status
from frame_ingest.agent.checks import (
    Problem,
    check_corrections,
    check_synthesis,
    check_vision,
    find_placeholders,
    frame_names,
    missing_frames_problem,
    parse,
    problem,
    read_output,
    unverified_quotes,
)
from frame_ingest.agent.common import PrepareError, agent_dir, video_of
from frame_ingest.agent.prepare import load_registry, load_transcript
from frame_ingest.agent.schemas import AgentSynthesis
from frame_ingest.agent.templates import TemplateIndex
from frame_ingest.engine import Engine
from frame_ingest.errors import FrameIngestError
from frame_ingest.guard.paths import is_within
from frame_ingest.llm_schemas import CorrectionOut, VisionBatchOut
from frame_ingest.models import Job, Segment
from frame_ingest.storage import JOB_ID_RE

MAX_TODO_LISTED = 20
KINDS = {"corrections.json": "corrections", "synthesis.json": "synthesis"}


class CheckRefused(FrameIngestError):
    code = "invalid_input"
    status = 400


def kind_of(rel: str) -> str | None:
    if rel.startswith("vision/") and rel.endswith(".json") and rel.count("/") == 1:
        return "vision-batch"
    return KINDS.get(rel)


def locate(engine: Engine, raw: str) -> tuple[str, str]:
    """(job id, path relative to agent/out) for a file inside a job's agent/out directory."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.is_symlink():
        raise CheckRefused("Refusing to follow a symlink.")
    root = engine.store.root.resolve()
    resolved = path.resolve()
    if not is_within(resolved, root):
        raise CheckRefused("check only reads agent files inside a frame-ingest job (agent/out/).")
    parts = resolved.relative_to(root).parts
    if len(parts) < 4 or not JOB_ID_RE.match(parts[0]) or parts[1:3] != ("agent", "out"):
        raise CheckRefused("check only reads agent files inside a frame-ingest job (agent/out/).")
    rel = "/".join(parts[3:])
    if kind_of(rel) is None:
        raise CheckRefused(
            "Not an agent output: expected vision/<name>.json, corrections.json or synthesis.json."
        )
    return parts[0], rel


class _Job:
    """What the checks need from a prepared job, loaded once."""

    def __init__(self, engine: Engine, job: Job) -> None:
        self.job = job
        self.dir = engine.store.dir(job.id)
        self.adir = agent_dir(self.dir)
        self.out = self.adir / "out"
        registry = load_registry(self.dir)
        transcript = load_transcript(self.dir)
        if registry is None or transcript is None:
            raise PrepareError(
                f"Job '{job.id}' has no evidence pack. Run prepare (or ingest) first.",
                code="not_prepared",
            )
        self.frames = {f.name: f for f in registry.frames}
        self.segments = transcript.segments
        self.duration = video_of(job).duration_s
        self.max_chapters = engine.config.max_chapters
        self.index = TemplateIndex(self.adir)

    def corrected_segments(self) -> list[Segment]:
        """The transcript with the agent's corrections applied, when they currently pass."""
        path = self.out / "corrections.json"
        if not (self.segments and path.is_file()):
            return self.segments
        problems: list[Problem] = []
        raw = read_output(path, "corrections.json", problems)
        out = parse(raw, CorrectionOut, "corrections.json", problems) if raw is not None else None
        if out is None:
            return self.segments
        texts, found, _ = check_corrections(out, "corrections.json", self.segments)
        if texts is None or found:
            return self.segments
        return [s.model_copy(update={"corrected_text": texts[s.id]}) for s in self.segments]


def _status(problems: list[Problem], todo: list[str]) -> str:
    return "invalid" if problems else "incomplete" if todo else "ok"


def _check(j: _Job, rel: str) -> dict[str, Any]:
    kind = kind_of(rel)
    problems: list[Problem] = []
    warnings: list[str] = []
    todo: list[str] = []
    raw = read_output(j.out / rel, rel, problems)
    if raw is not None:
        todo = find_placeholders(raw)
        if kind == "vision-batch":
            batch = parse(raw, VisionBatchOut, rel, problems)
            if batch is not None:
                seen: dict[str, str] = {}
                for other in sorted((j.out / "vision").glob("*.json")):
                    other_rel = f"vision/{other.name}"
                    if other_rel != rel and not other.is_symlink():
                        try:
                            names = frame_names(json.loads(other.read_text(encoding="utf-8")))
                        except (OSError, ValueError):
                            continue
                        seen.update({n: other_rel for n in names})
                expected = j.index.vision.get(rel)
                _, found = check_vision(batch, rel, j.frames, seen, expected)
                problems += found
        elif kind == "corrections":
            if not j.segments:
                problems.append(problem(rel, "unexpected", "there is no transcript"))
            out = parse(raw, CorrectionOut, rel, problems)
            if out is not None and j.segments:
                _, found, implausible = check_corrections(out, rel, j.segments)
                problems += found
                if implausible:
                    warnings.append(
                        f"{len(implausible)} correction(s) change the length implausibly and "
                        f"will be ignored (ids {implausible[:10]})"
                    )
        else:
            syn = parse(raw, AgentSynthesis, rel, problems)
            if syn is not None:
                problems += check_synthesis(syn, rel, j.duration, j.max_chapters)
                dropped = unverified_quotes(syn, j.corrected_segments(), j.duration)
                if dropped:
                    warnings.append(
                        f"{dropped} quote(s) are not verbatim in the transcript and will be dropped"
                    )
    return {
        "file": str(j.out / rel),
        "kind": kind,
        "status": _status(problems, todo),
        "problems": problems,
        "todo": {"count": len(todo), "paths": todo[:MAX_TODO_LISTED]},
        "warnings": warnings,
    }


def check_file(engine: Engine, raw: str) -> dict[str, Any]:
    job_id, rel = locate(engine, raw)
    report = _check(_Job(engine, engine.load(job_id)), rel)
    return {"ok": report["status"] == "ok", "job_id": job_id, **report}


def expected_files(j: _Job) -> list[str]:
    """Every agent file this job needs (templates and any other vision files)."""
    vision = {f"vision/{p.name}" for p in (j.out / "vision").glob("*.json")}
    vision |= set(j.index.vision)
    files = sorted(vision)
    if j.segments and (j.out / "corrections.json").exists():
        files.append("corrections.json")
    files.append("synthesis.json")
    return files


def check_job(engine: Engine, job_id: str) -> dict[str, Any]:
    """Every agent file of a job, plus the checks that span files (every frame analysed)."""
    j = _Job(engine, engine.load(job_id))
    files = [_check(j, rel) for rel in expected_files(j)]
    problems: list[Problem] = []
    covered: set[str] = set()
    for rel in expected_files(j):
        if rel.startswith("vision/") and (j.out / rel).is_file():
            try:
                covered.update(frame_names(json.loads((j.out / rel).read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
    missing = missing_frames_problem(n for n in j.frames if n not in covered)
    if missing:
        problems.append(missing)
    audio = load_status(j.dir)
    if audio is not None and audio.status == "needs_decision":
        problems.append(problem("audio.json", "needs_decision", audio.message))
    statuses = [f["status"] for f in files]
    status = (
        "invalid"
        if problems or "invalid" in statuses
        else "incomplete"
        if "incomplete" in statuses
        else "ok"
    )
    return {
        "ok": status == "ok",
        "job_id": job_id,
        "status": status,
        "files": files,
        "problems": problems,
    }
