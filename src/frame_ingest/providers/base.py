"""Provider interfaces. The pipeline only talks to these; tests and demo mode inject fakes,
and a self-hosted Whisper / vLLM endpoint only has to implement them."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel, Field

from frame_ingest.llm_schemas import VisionBatchOut
from frame_ingest.models import ImageDetail, StageName

T = TypeVar("T", bound=BaseModel)


class TranscriberCaps(BaseModel):
    segment_timestamps: bool = True
    keywords: bool = False
    prompt: bool = True
    diarization: bool = False
    deprecated: str | None = None


class UsageDelta(BaseModel):
    calls: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    audio_seconds: float = 0.0


class RawSegment(BaseModel):
    start: float
    end: float
    text: str
    speaker: str | None = None


class RawTranscription(BaseModel):
    """One audio chunk's transcription; timestamps are relative to the chunk start."""

    segments: list[RawSegment]
    language: str | None = None
    precision: Literal["segment", "chunk"] = "segment"
    usage: UsageDelta = Field(default_factory=UsageDelta)


class VisionFrame(BaseModel):
    name: str
    t: float
    path: Path
    label: str = ""


class VisionBatchRequest(BaseModel):
    """Prompt text is built by pipeline.vision; the provider only encodes images and calls."""

    system: str
    preamble: str
    frames: list[VisionFrame]
    context_frame: VisionFrame | None = None
    postamble: str = ""
    detail: ImageDetail = "auto"
    model: str


class LLMResult(BaseModel, Generic[T]):
    value: T
    usage: UsageDelta = Field(default_factory=UsageDelta)


class Transcriber(Protocol):
    """Audio chunk -> chunk-relative segments. Stitching, offsets and overlap dedupe live in
    pipeline/transcribe.py, so a self-hosted Whisper only has to implement this."""

    async def transcribe(
        self,
        audio: Path,
        *,
        model: str,
        duration_s: float,
        prompt: str | None,
        keywords: list[str],
        language: str | None,
        diarize: bool,
    ) -> RawTranscription: ...

    def caps_for(self, model: str) -> TranscriberCaps: ...


class VisionAnalyzer(Protocol):
    async def analyze(self, req: VisionBatchRequest) -> LLMResult[VisionBatchOut]: ...


class TextLLM(Protocol):
    async def complete_json(
        self, schema: type[T], *, system: str, user: str, stage: StageName, model: str
    ) -> LLMResult[T]: ...


@dataclass
class ProviderBundle:
    transcriber: Transcriber
    vision: VisionAnalyzer
    text: TextLLM
