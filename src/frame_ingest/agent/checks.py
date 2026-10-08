"""The checks on what the host agent writes, shared by `assemble` (all files at once, before
building) and `check` (one file, as soon as it is written).

Each problem names the file, the JSON path and the rule it broke. Templates written by `prepare`
are schema-valid but hold `TODO:` placeholder strings; a placeholder is never accepted, so a file
reports `incomplete` until every one is replaced.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from frame_ingest.agent.schemas import AgentSynthesis
from frame_ingest.guard.paths import PathRejected, read_bytes_nofollow
from frame_ingest.llm_schemas import CorrectionOut, VisionBatchOut
from frame_ingest.models import FrameAnalysis, FrameInfo, Quote, Segment
from frame_ingest.pipeline.correct import accept_text, validate_correction
from frame_ingest.pipeline.synthesize import chapter_problems, segments_in, verify_quotes
from frame_ingest.pipeline.vision import to_analysis

MAX_OUT_BYTES = 2 * 1024 * 1024
MAX_LISTED = 20
PLACEHOLDER = "TODO"
M = TypeVar("M", bound=BaseModel)
Problem = dict[str, str]


def problem(file: str, rule: str, message: str, path: str = "$") -> Problem:
    # `code` repeats `rule` for callers written against the earlier assemble output
    return {"file": file, "path": path, "rule": rule, "code": rule, "message": message}


def is_placeholder(value: str) -> bool:
    text = value.strip()
    return text == PLACEHOLDER or text.startswith(PLACEHOLDER + ":")


def find_placeholders(data: Any, path: str = "$") -> list[str]:
    """JSON paths of every string that is still a `TODO` placeholder."""
    if isinstance(data, str):
        return [path] if is_placeholder(data) else []
    if isinstance(data, list):
        return [p for i, v in enumerate(data) for p in find_placeholders(v, f"{path}[{i}]")]
    if isinstance(data, dict):
        return [p for k, v in data.items() for p in find_placeholders(v, f"{path}.{k}")]
    return []


def json_path(loc: Sequence[int | str]) -> str:
    return "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in loc)


def read_output(path: Path, rel: str, problems: list[Problem]) -> Any:
    """The parsed JSON of an agent file, or None (with a problem recorded)."""
    try:
        if path.stat().st_size > MAX_OUT_BYTES:
            problems.append(problem(rel, "too_large", f"file exceeds {MAX_OUT_BYTES} bytes"))
            return None
        return json.loads(read_bytes_nofollow(path).decode("utf-8"))
    except PathRejected as exc:
        problems.append(problem(rel, "bad_file", exc.message))
    except FileNotFoundError:
        problems.append(problem(rel, "missing", "the file does not exist"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        problems.append(problem(rel, "bad_json", f"not valid JSON: {type(exc).__name__}"))
    return None


def parse(raw: Any, model: type[M], rel: str, problems: list[Problem]) -> M | None:
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        for err in exc.errors()[:5]:
            where = json_path(err["loc"])
            problems.append(problem(rel, "bad_schema", f"{where}: {err['msg']}", where))
    return None


def placeholder_problem(rel: str, paths: list[str]) -> Problem:
    more = f" (and {len(paths) - 3} more)" if len(paths) > 3 else ""
    return problem(
        rel,
        "placeholder",
        f"{len(paths)} TODO placeholder(s) left to fill: {', '.join(paths[:3])}{more}",
        paths[0],
    )


# ── vision/batch-NN.json ────────────────────────────────────────────────────────
def frame_names(raw: Any) -> list[str]:
    """Frame names in a vision file, read leniently (used to spot duplicates across files)."""
    frames = raw.get("frames") if isinstance(raw, dict) else None
    if not isinstance(frames, list):
        return []
    return [f["frame"] for f in frames if isinstance(f, dict) and isinstance(f.get("frame"), str)]


def check_vision(
    batch: VisionBatchOut,
    rel: str,
    info: dict[str, FrameInfo],
    seen: dict[str, str],
    expected: list[str] | None = None,
) -> tuple[dict[str, FrameAnalysis], list[Problem]]:
    """One vision file. `seen` maps frames already analysed to the file that did it (updated in
    place); `expected` is the frame list of the template this file came from, if any."""
    problems: list[Problem] = []
    got: dict[str, FrameAnalysis] = {}
    missing = [n for n in expected or [] if n not in {i.frame for i in batch.frames}]
    for i, item in enumerate(batch.frames):
        at = f"$.frames[{i}]"
        if item.frame not in info:
            hint = ""
            if expected is not None and i < len(expected) and expected[i] in missing:
                hint = f"; the template had {expected[i]!r} here"
                missing.remove(expected[i])
            problems.append(
                problem(rel, "unknown_frame", f"unknown frame {item.frame[:60]!r}{hint}", at)
            )
        elif item.frame in seen:
            other = seen[item.frame]
            where = "twice in this file" if other == rel else f"already in {other}"
            problems.append(
                problem(rel, "duplicate_frame", f"{item.frame} analysed {where}", f"{at}.frame")
            )
        elif not item.scene_description.strip():
            problems.append(
                problem(rel, "empty", f"{item.frame}: scene_description is empty", f"{at}.frame")
            )
        else:
            seen[item.frame] = rel
            got[item.frame] = to_analysis(item, info[item.frame])
    for name in missing:
        problems.append(
            problem(rel, "missing_frame", f"{name} from the template is not analysed", "$.frames")
        )
    return got, problems


def missing_frames_problem(names: Iterable[str]) -> Problem | None:
    missing = list(names)
    if not missing:
        return None
    listed = ", ".join(missing[:MAX_LISTED]) + (" ..." if len(missing) > MAX_LISTED else "")
    return problem(
        "vision/", "missing_frames", f"{len(missing)} frame(s) not analysed: {listed}", "$"
    )


# ── corrections.json ────────────────────────────────────────────────────────────
def check_corrections(
    out: CorrectionOut, rel: str, segments: list[Segment]
) -> tuple[dict[int, str] | None, list[Problem], list[int]]:
    """(texts by id, problems, ids whose correction will be ignored as implausible)."""
    texts, err = validate_correction(segments, out)
    if err:
        want = {s.id for s in segments}
        first = next((i for i, c in enumerate(out.segments) if c.id not in want), None)
        path = f"$.segments[{first}].id" if first is not None else "$.segments"
        return None, [problem(rel, "bad_ids", err, path)], []
    implausible = [s.id for s in segments if not accept_text(s.raw_text, texts[s.id])]
    return texts, [], implausible


# ── synthesis.json ──────────────────────────────────────────────────────────────
def check_synthesis(
    syn: AgentSynthesis, rel: str, duration: float, max_chapters: int
) -> list[Problem]:
    bounds = [(c.title, c.start, c.end) for c in syn.chapters]
    problems = [
        problem(rel, "bad_chapters", message, f"$.{where}")
        for where, message in chapter_problems(bounds, duration)
    ]
    if len(syn.chapters) > max_chapters:
        problems.append(problem(rel, "too_many_chapters", f"at most {max_chapters}", "$.chapters"))
    for field in ("title", "tldr", "abstract"):
        if not getattr(syn, field).strip():
            problems.append(problem(rel, "empty", f"{field} is empty", f"$.{field}"))
    for i, c in enumerate(syn.chapters, 1):
        if not c.title.strip() or not c.summary.strip():
            problems.append(
                problem(rel, "empty", f"chapter {i}: title/summary empty", f"$.chapters[{i - 1}]")
            )
    return problems


def unverified_quotes(syn: AgentSynthesis, segments: list[Segment], duration: float) -> int:
    """How many proposed quotes are not verbatim in the transcript (they would be dropped)."""
    n = len(syn.chapters)
    dropped = 0
    for i, c in enumerate(syn.chapters):
        last = i == n - 1
        _, d = verify_quotes(
            [Quote(t=min(max(q.t, 0.0), duration), text=q.text) for q in c.quotes],
            segments_in(segments, c.start, c.end, last),
            c.start,
            c.end,
        )
        dropped += d
    return dropped
