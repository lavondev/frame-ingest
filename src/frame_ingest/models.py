"""All persistent pydantic models (job state, analysis, document data).

LLM-facing (strict-schema) response models live next to the stage that uses them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Seconds = Annotated[float, Field(ge=0)]
ImageDetail = Literal["low", "high", "auto"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class StageName(StrEnum):
    PROBE = "probe"
    AUDIO = "audio"
    TRANSCRIBE = "transcribe"
    FRAMES = "frames"
    VISION = "vision"
    CORRECT = "correct"
    SYNTHESIZE = "synthesize"
    ASSEMBLE = "assemble"


STAGE_ORDER: list[StageName] = list(StageName)


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class JobStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ── settings ────────────────────────────────────────────────────────────────
class ModelOverrides(BaseModel):
    transcribe: str | None = None
    vision: str | None = None
    correct: str | None = None
    synthesize: str | None = None


class JobSettings(BaseModel):
    """Per-job options sent by the client; every field optional (falls back to AppConfig)."""

    frame_cap: int | None = Field(None, ge=1, le=500)
    scene_threshold: float | None = Field(None, gt=0, lt=1)
    min_interval_s: float | None = Field(None, ge=1)
    image_detail: ImageDetail | None = None
    context: str = Field("", max_length=20_000)
    language: str | None = Field(None, max_length=16)
    diarize: bool | None = None
    models: ModelOverrides = Field(default_factory=ModelOverrides)


class ResolvedSettings(BaseModel):
    """JobSettings merged over AppConfig; frozen at job creation and used in cache keys."""

    model_config = ConfigDict(frozen=True)

    frame_cap: int
    scene_threshold: float
    min_interval_s: float
    image_detail: ImageDetail
    context: str
    language: str | None
    diarize: bool
    transcribe_model: str
    vision_model: str
    correct_model: str
    synthesize_model: str


# ── media ───────────────────────────────────────────────────────────────────
class VideoInfo(BaseModel):
    filename: str
    size_bytes: int
    sha256: str
    duration_s: Seconds
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    has_audio: bool = False
    audio_codec: str | None = None


# ── transcript ──────────────────────────────────────────────────────────────
class Segment(BaseModel):
    id: int
    start: Seconds
    end: Seconds
    raw_text: str
    corrected_text: str | None = None
    speaker: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Segment:
        if self.end < self.start:
            raise ValueError("segment end precedes start")
        return self

    @property
    def text(self) -> str:
        return self.corrected_text if self.corrected_text is not None else self.raw_text


class Transcript(BaseModel):
    model: str | None = None
    language: str | None = None
    timestamp_precision: Literal["segment", "chunk", "none"] = "none"
    chunk_count: int = 0
    segments: list[Segment] = Field(default_factory=list)


# ── frames / vision ─────────────────────────────────────────────────────────
class SceneType(StrEnum):
    SLIDE = "slide"
    SCREEN_RECORDING = "screen_recording"
    TALKING_HEAD = "talking_head"
    WHITEBOARD = "whiteboard"
    DEMO = "demo"
    DIAGRAM = "diagram"
    CHART = "chart"
    TITLE_CARD = "title_card"
    B_ROLL = "b_roll"
    OTHER = "other"


EntityKind = Literal["person", "organization", "product", "technology", "place", "concept", "other"]


class Entity(BaseModel):
    name: str
    kind: EntityKind = "other"


class FrameInfo(BaseModel):
    name: str
    t: Seconds
    reason: Literal["first", "scene", "interval"]
    scene_score: float | None = None
    width: int
    height: int


class FrameAnalysis(BaseModel):
    frame: str
    t: Seconds
    scene_description: str
    on_screen_text: list[str] = Field(default_factory=list)
    change_from_previous: str = ""
    entities: list[Entity] = Field(default_factory=list)
    scene_type: SceneType = SceneType.OTHER


# ── synthesis ───────────────────────────────────────────────────────────────
class Quote(BaseModel):
    t: Seconds
    text: str


class Chapter(BaseModel):
    index: int
    id: str
    title: str
    start: Seconds
    end: Seconds
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    decisions_claims: list[str] = Field(default_factory=list)
    visual_description: str = ""
    on_screen_text: list[str] = Field(default_factory=list)
    frames: list[str] = Field(default_factory=list)


class GlossaryEntry(BaseModel):
    term: str
    definition: str
    first_seen: Seconds | None = None


class EntityMention(BaseModel):
    t: Seconds
    chapter_id: str
    source: Literal["transcript", "visual", "chapter"]


class EntityIndexEntry(BaseModel):
    """Computed deterministically (never LLM-authored)."""

    name: str
    kind: str
    count: int
    mentions: list[EntityMention]


class VideoSynthesis(BaseModel):
    title: str = ""
    tldr: str = ""
    abstract: str = ""
    glossary: list[GlossaryEntry] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


# ── processing record ───────────────────────────────────────────────────────
class TokenUsage(BaseModel):
    stage: StageName
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    audio_seconds: float = 0.0


class ProcessingWarning(BaseModel):
    stage: StageName
    code: str
    message: str
    t_start: Seconds | None = None
    t_end: Seconds | None = None


class FailedBatch(BaseModel):
    stage: StageName
    index: int
    t_start: Seconds
    t_end: Seconds
    error: str


class ProcessingNotes(BaseModel):
    started_at: datetime
    finished_at: datetime | None = None
    stages_run: list[StageName] = Field(default_factory=list)
    stages_cached: list[StageName] = Field(default_factory=list)
    stages_skipped: list[StageName] = Field(default_factory=list)
    models: dict[str, str] = Field(default_factory=dict)
    frame_count: int = 0
    corrections_changed: int = 0
    usage: list[TokenUsage] = Field(default_factory=list)
    warnings: list[ProcessingWarning] = Field(default_factory=list)
    failed_batches: list[FailedBatch] = Field(default_factory=list)


class Analysis(BaseModel):
    """The JSON sidecar (result.json)."""

    schema_version: Literal["1"] = "1"
    analyzed_at: datetime
    video: VideoInfo
    settings: ResolvedSettings
    transcript: Transcript
    frames: list[FrameInfo]
    scenes: list[FrameAnalysis]
    chapters: list[Chapter]
    synthesis: VideoSynthesis
    entity_index: list[EntityIndexEntry]
    notes: ProcessingNotes


# ── job state / estimate / events ───────────────────────────────────────────
class StageState(BaseModel):
    name: StageName
    status: StageStatus = StageStatus.PENDING
    progress: float = Field(0, ge=0, le=1)
    units_done: int = 0
    units_total: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cache_hit: bool = False
    message: str | None = None
    error: str | None = None


class JobError(BaseModel):
    stage: StageName | None = None
    code: str
    message: str


class StageEstimate(BaseModel):
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    audio_minutes: float = 0.0


class Estimate(BaseModel):
    duration_s: float
    has_audio: bool
    scene_cuts: int
    frames: int
    stages: dict[StageName, StageEstimate]
    total_api_calls: int
    total_input_tokens: int
    total_output_tokens: int
    cost_usd: float | None = None
    cost_note: str | None = None
    assumptions: list[str] = Field(default_factory=list)


class Job(BaseModel):
    id: str
    created_at: datetime
    updated_at: datetime
    status: JobStatus
    settings: ResolvedSettings
    video: VideoInfo | None = None
    stages: dict[StageName, StageState]
    warnings: list[ProcessingWarning] = Field(default_factory=list)
    usage: list[TokenUsage] = Field(default_factory=list)
    estimate: Estimate | None = None
    error: JobError | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    chapter_count: int | None = None


EventType = Literal[
    "job",
    "stage",
    "log",
    "transcript",
    "frame",
    "scene",
    "chapters",
    "synthesis",
    "warning",
    "error",
    "done",
]


def new_stage_states() -> dict[StageName, StageState]:
    return {s: StageState(name=s) for s in STAGE_ORDER}
