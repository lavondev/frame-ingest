"""LLM-facing response schemas.

Kept deliberately plain (no defaults, no value constraints) so they convert to OpenAI strict
JSON schemas (every property required, additionalProperties: false). Validation beyond shape
(ID sets, timestamp ranges, quote verbatim-ness) happens in the stages, not here.
"""

from __future__ import annotations

from pydantic import BaseModel

from frame_ingest.models import EntityKind, SceneType


class EntityOut(BaseModel):
    name: str
    kind: EntityKind


class VisionFrameOut(BaseModel):
    frame: str
    scene_description: str
    on_screen_text: list[str]
    change_from_previous: str
    entities: list[EntityOut]
    scene_type: SceneType


class VisionBatchOut(BaseModel):
    frames: list[VisionFrameOut]


class CorrectedSegmentOut(BaseModel):
    id: int
    corrected_text: str


class CorrectionOut(BaseModel):
    segments: list[CorrectedSegmentOut]


class ChapterBoundaryOut(BaseModel):
    title: str
    start: float
    end: float


class ChapterProposalOut(BaseModel):
    chapters: list[ChapterBoundaryOut]


class QuoteOut(BaseModel):
    t: float
    text: str


class ChapterDetailOut(BaseModel):
    summary: str
    key_points: list[str]
    quotes: list[QuoteOut]
    entities: list[EntityOut]
    decisions_claims: list[str]
    visual_summary: str


class GlossaryOut(BaseModel):
    term: str
    definition: str
    first_seen_s: float | None


class GlobalSynthesisOut(BaseModel):
    title: str
    tldr: str
    abstract: str
    glossary: list[GlossaryOut]
    open_questions: list[str]
    tags: list[str]
