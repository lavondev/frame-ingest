"""`validate` and `scan`: check a finished document against the format, and flag injection
patterns in a document or a job.

Messages never quote the file's content (only keys, line numbers and counts), so pointing these
commands at an unrelated file cannot be used to read it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from frame_ingest.errors import FrameIngestError
from frame_ingest.guard.paths import PathRejected, read_bytes_nofollow, require_regular_file
from frame_ingest.guard.scan import scan_many
from frame_ingest.pipeline.assemble import BANNER, FRAMES_ONLY, check_timestamps
from frame_ingest.pipeline.timefmt import parse_ts

MAX_DOC_BYTES = 8 * 1024 * 1024
REQUIRED_FRONT = (
    "title",
    "source_file",
    "duration_seconds",
    "analyzed_at",
    "chapter_count",
    "trust",
    "mode",
    "injection_flags",
    "coverage",
)
COVERAGE_KEYS = (
    "audio",
    "audio_track",
    "transcript_source",
    "frames_analyzed",
    "chapters",
    "quotes_verified",
)
_RATIO = re.compile(r"^\d+/\d+$")
SECTIONS = (
    "## TL;DR {#tldr}",
    "## Abstract {#abstract}",
    "## Table of Contents {#toc}",
    "## Glossary {#glossary}",
    "## Entity Index {#entity-index}",
    "## Open Questions {#open-questions}",
    "## Appendix: Processing Notes {#appendix}",
)
_CHAPTER = re.compile(
    r"^## Chapter (\d+): .* \[(\d{2}:\d{2}:\d{2}) - (\d{2}:\d{2}:\d{2})\] \{#(ch-\d{2})\}$"
)
_HEADING_ID = re.compile(r"\{#([A-Za-z0-9_-]+)\}\s*$")
_ANCHOR = re.compile(r'<a id="(t-\d{6}(?:-\d+)?)"></a>')
_LINK = re.compile(r"(?<!\\)\]\(#([A-Za-z0-9_-]+)\)")
_RAW_HTML = re.compile(r"<[A-Za-z!/?]")


class DocumentUnreadable(FrameIngestError):
    code = "invalid_document"
    status = 400


def read_document(path: Path) -> str:
    try:
        require_regular_file(path, what="document")
        if path.stat().st_size > MAX_DOC_BYTES:
            raise DocumentUnreadable("The document is too large to validate.")
        return read_bytes_nofollow(path).decode("utf-8")
    except PathRejected as exc:
        raise DocumentUnreadable(exc.message) from None
    except (OSError, UnicodeDecodeError):
        raise DocumentUnreadable("The document could not be read as UTF-8 text.") from None


def _issue(code: str, message: str, line: int | None = None) -> dict[str, Any]:
    return {"code": code, "line": line, "message": message}


def _split_front(text: str) -> tuple[dict[str, Any] | None, str, int]:
    if not text.startswith("---\n"):
        return None, text, 0
    end = text.find("\n---\n", 4)
    if end < 0:
        return None, text, 0
    try:
        front = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None, text, 0
    body = text[end + 5 :]
    return (front if isinstance(front, dict) else None), body, text[: end + 5].count("\n")


def validate_document(text: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    front, body, offset = _split_front(text)
    if front is None:
        return [_issue("frontmatter", "missing or unparseable YAML frontmatter")]
    for key in REQUIRED_FRONT:
        if key not in front:
            issues.append(_issue("frontmatter", f"missing frontmatter key '{key}'"))
    if front.get("trust") != "untrusted-content":
        issues.append(_issue("trust", "frontmatter trust must be 'untrusted-content'"))
    if BANNER not in body:
        issues.append(_issue("banner", "the untrusted-content banner is missing"))
    issues += _check_coverage(front.get("coverage"), body)

    lines = body.splitlines()
    ids: dict[str, int] = {}
    chapters: list[tuple[int, float, float, str]] = []
    positions: dict[str, int] = {}
    for n, line in enumerate(lines, offset + 1):
        if line.startswith("## ") or line.startswith("### "):
            m = _HEADING_ID.search(line)
            if m:
                if m.group(1) in ids:
                    issues.append(_issue("duplicate_anchor", f"anchor '{m.group(1)}' repeats", n))
                ids[m.group(1)] = n
            cm = _CHAPTER.match(line)
            if cm:
                chapters.append(
                    (int(cm.group(1)), parse_ts(cm.group(2)), parse_ts(cm.group(3)), cm.group(4))
                )
            if line in SECTIONS:
                positions[line] = n
        for a in _ANCHOR.findall(line):
            if a in ids:
                issues.append(_issue("duplicate_anchor", f"anchor '{a}' repeats", n))
            ids[a] = n
        if _RAW_HTML.search(_ANCHOR.sub("", line)):  # only our own transcript anchors are HTML
            issues.append(_issue("raw_html", "raw HTML in the document body", n))

    for section in SECTIONS:
        if section not in positions:
            issues.append(_issue("section", f"missing section '{section[3:]}'"))
    if len(positions) == len(SECTIONS) and list(positions.values()) != sorted(positions.values()):
        issues.append(_issue("section_order", "top-level sections are out of order"))

    declared = front.get("chapter_count")
    if isinstance(declared, int) and declared != len(chapters):
        issues.append(
            _issue("chapter_count", f"frontmatter says {declared}, found {len(chapters)} chapters")
        )
    duration = front.get("duration_seconds")
    for i, (idx, start, end, cid) in enumerate(chapters):
        if idx != i + 1 or cid != f"ch-{i + 1:02d}":
            issues.append(_issue("chapter_index", f"chapter {i + 1} is numbered/anchored wrongly"))
        if end < start:
            issues.append(_issue("chapter_range", f"chapter {i + 1} ends before it starts"))
        if i and abs(start - chapters[i - 1][2]) > 1.0:
            issues.append(_issue("chapter_gap", f"gap or overlap before chapter {i + 1}"))
    if chapters and isinstance(duration, int | float):
        if chapters[0][1] > 1.0:
            issues.append(_issue("chapter_range", "the first chapter does not start at 00:00:00"))
        if abs(chapters[-1][2] - float(duration)) > 1.5:
            issues.append(_issue("chapter_range", "the last chapter does not end at the duration"))

    for n, line in enumerate(lines, offset + 1):
        for target in _LINK.findall(line):
            if target not in ids:
                issues.append(_issue("broken_link", f"link to missing anchor '{target}'", n))
    if isinstance(duration, int | float):
        for ts in check_timestamps(body, float(duration)):
            issues.append(_issue("timestamp", f"timestamp {ts} is past the video duration"))
    return issues


def _check_coverage(coverage: Any, body: str) -> list[dict[str, Any]]:
    if coverage is None:
        return []  # reported as a missing frontmatter key
    if not isinstance(coverage, dict):
        return [_issue("coverage", "frontmatter coverage must be a mapping")]
    issues = [
        _issue("coverage", f"coverage is missing '{key}'")
        for key in COVERAGE_KEYS
        if key not in coverage
    ]
    audio = coverage.get("audio")
    if audio not in ("yes", "no"):
        issues.append(_issue("coverage", "coverage.audio must be 'yes' or 'no'"))
    for key in ("frames_analyzed", "quotes_verified"):
        if key in coverage and not _RATIO.match(str(coverage[key])):
            issues.append(_issue("coverage", f"coverage.{key} must look like N/M"))
    has_banner = any(line.startswith(FRAMES_ONLY) for line in body.splitlines())
    if audio == "no" and not has_banner:
        issues.append(_issue("frames_only", "audio is 'no' but the Frames-only banner is missing"))
    if audio == "yes" and has_banner:
        issues.append(_issue("frames_only", "audio is 'yes' but the document says 'Frames only'"))
    return issues


def scan_document(text: str) -> dict[str, Any]:
    """Injection flags and the line numbers they occur on (never the matching text)."""
    _, body, offset = _split_front(text)
    per_line: dict[str, list[int]] = {}
    for n, line in enumerate(body.splitlines(), offset + 1):
        if line.startswith(">") and "Untrusted content" in line:
            continue  # our own banner
        for kind in scan_many([line]):
            per_line.setdefault(kind, []).append(n)
    flags = scan_many(
        [ln for ln in body.splitlines() if not (ln.startswith(">") and "Untrusted content" in ln)]
    )
    return {"flags": flags, "lines": {k: v[:50] for k, v in per_line.items()}}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    return []


def scan_job_files(job_dir: Path) -> dict[str, Any]:
    """Injection flags over the job's evidence pack, agent outputs and finished JSON."""
    candidates = [
        job_dir / "agent" / "transcript.json",
        *sorted((job_dir / "agent" / "out").rglob("*.json")),
        *sorted((job_dir / "out").glob("*.json")),
    ]
    per_file: dict[str, dict[str, int]] = {}
    texts: list[str] = []
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        strings = _strings(data)
        texts += strings
        if flags := scan_many(strings):
            per_file[str(path.relative_to(job_dir))] = flags
    return {"flags": scan_many(texts), "files": per_file}
