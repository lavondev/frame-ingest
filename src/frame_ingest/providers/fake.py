"""Deterministic offline providers.

Used by the test-suite (no key, no network) and by the `fake` profile so the whole pipeline
can be exercised without an API key. They parse the machine-readable conventions in the stage
prompts (TARGET SEGMENTS blocks, WINDOW_RANGE / CHAPTER_RANGE headers) rather than guessing.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from frame_ingest.config import known_caps
from frame_ingest.errors import CapabilityChanged, ProviderError
from frame_ingest.llm_schemas import (
    ChapterBoundaryOut,
    ChapterDetailOut,
    ChapterProposalOut,
    CorrectedSegmentOut,
    CorrectionOut,
    EntityOut,
    GlobalSynthesisOut,
    GlossaryOut,
    QuoteOut,
    VisionBatchOut,
    VisionFrameOut,
)
from frame_ingest.models import SceneType, StageName
from frame_ingest.providers.base import (
    LLMResult,
    ProviderBundle,
    RawSegment,
    RawTranscription,
    TranscriberCaps,
    UsageDelta,
    VisionBatchRequest,
)

T = TypeVar("T", bound=BaseModel)

SCRIPT = [
    "Welcome to the Widjet Frobnicator walkthrough.",
    "Today we cover installation and configuration.",
    "First open the dashboard and click Settings.",
    "The Widjet Frobnicator stores every preference locally.",
    "Next we enable the sync option and restart.",
    "That is all you need for a working setup.",
    "In the second half we look at advanced tuning.",
    "Set the cache size to match your workload.",
    "Finally, export the report and share it with your team.",
    "Thanks for watching, see you in the next video.",
]


class FakeTranscriber:
    def __init__(
        self,
        *,
        delay: float = 0.0,
        caps: TranscriberCaps | None = None,
        reject_verbose_json: bool = False,
        seg_len: float = 4.0,
    ) -> None:
        self.delay = delay
        self._caps = caps
        self.reject_verbose_json = reject_verbose_json
        self.seg_len = seg_len
        self.calls: list[dict[str, Any]] = []
        self._no_segments: set[str] = set()

    def caps_for(self, model: str) -> TranscriberCaps:
        caps = self._caps.model_copy() if self._caps else known_caps(model)
        if model in self._no_segments:
            caps.segment_timestamps = False
        return caps

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
    ) -> RawTranscription:
        caps = self.caps_for(model)
        self.calls.append(
            {
                "audio": audio.name,
                "model": model,
                "duration": duration_s,
                "prompt": prompt,
                "keywords": keywords,
                "language": language,
                "diarize": diarize,
            }
        )
        if self.reject_verbose_json and caps.segment_timestamps:
            self._no_segments.add(model)
            raise CapabilityChanged("segment_timestamps")
        if self.delay:
            await asyncio.sleep(self.delay)
        usage = UsageDelta(audio_seconds=duration_s)
        n = max(1, int(duration_s // self.seg_len))
        index = int(re.sub(r"\D", "", audio.stem) or 0)
        lines = [SCRIPT[(index * 7 + i) % len(SCRIPT)] for i in range(n)]
        if not caps.segment_timestamps:
            return RawTranscription(
                segments=[RawSegment(start=0.0, end=duration_s, text=" ".join(lines))],
                language="en",
                precision="chunk",
                usage=usage,
            )
        segs = [
            RawSegment(
                start=i * self.seg_len,
                end=min(duration_s, (i + 1) * self.seg_len),
                text=line,
                speaker=("A" if i % 2 == 0 else "B") if diarize else None,
            )
            for i, line in enumerate(lines)
        ]
        return RawTranscription(segments=segs, language="en", precision="segment", usage=usage)


class FakeVision:
    def __init__(
        self,
        *,
        delay: float = 0.0,
        fail_when: Callable[[VisionBatchRequest], bool] | None = None,
    ) -> None:
        self.delay = delay
        self.fail_when = fail_when
        self.calls = 0
        self.batches: list[list[str]] = []

    async def analyze(self, req: VisionBatchRequest) -> LLMResult[VisionBatchOut]:
        idx = self.calls
        self.calls += 1
        self.batches.append([f.name for f in req.frames])
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_when is not None and self.fail_when(req):
            raise ProviderError("simulated vision failure")
        frames = [
            VisionFrameOut(
                frame=f.name,
                scene_description=f"A dashboard screen titled 'Widget Frobnicator' at {f.t:.0f}s.",
                on_screen_text=["Widget Frobnicator", f"Step {i + 1}"],
                change_from_previous="First frame"
                if (idx == 0 and i == 0)
                else "The panel changed.",
                entities=[EntityOut(name="Widget Frobnicator", kind="product")],
                scene_type=SceneType.SLIDE,
            )
            for i, f in enumerate(req.frames)
        ]
        return LLMResult(
            value=VisionBatchOut(frames=frames),
            usage=UsageDelta(input_tokens=150 * len(frames), output_tokens=60 * len(frames)),
        )


def _between(text: str, start: str, end: str | None) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    i += len(start)
    j = text.find(end, i) if end else -1
    return text[i : j if j >= 0 else len(text)]


def _ts_to_s(ts: str) -> float:
    h, m, s = (int(x) for x in ts.split(":"))
    return h * 3600 + m * 60 + s


class FakeText:
    def __init__(
        self,
        *,
        delay: float = 0.0,
        proposal_mode: str = "good",
        fail_stages: set[str] | None = None,
    ) -> None:
        self.delay = delay
        self.proposal_mode = proposal_mode  # good | gap | beyond | empty
        self.fail_stages = fail_stages or set()
        self.calls: list[str] = []

    async def complete_json(
        self, schema: type[T], *, system: str, user: str, stage: StageName, model: str
    ) -> LLMResult[T]:
        self.calls.append(schema.__name__)
        if self.delay:
            await asyncio.sleep(self.delay)
        if schema.__name__ in self.fail_stages:
            raise ProviderError(f"simulated failure for {schema.__name__}")
        usage = UsageDelta(input_tokens=len(user) // 4, output_tokens=120)
        if schema is CorrectionOut:
            target = _between(user, "TARGET SEGMENTS (correct these):\n", "\n\nCONTEXT AFTER")
            segs = [
                CorrectedSegmentOut(
                    id=int(m.group(1)), corrected_text=m.group(2).replace("Widjet", "Widget")
                )
                for m in re.finditer(r"^\[(\d+)\] (.*)$", target, re.M)
            ]
            return LLMResult(value=cast(T, CorrectionOut(segments=segs)), usage=usage)
        if schema is ChapterProposalOut:
            m = re.search(r"WINDOW_RANGE: ([\d.]+)-([\d.]+)", user)
            if not m:
                raise ValueError("chapter proposal prompt has no WINDOW_RANGE header")
            w0, w1 = float(m.group(1)), float(m.group(2))
            return LLMResult(value=cast(T, self._proposal(w0, w1)), usage=usage)
        if schema is ChapterDetailOut:
            return LLMResult(value=cast(T, self._detail(user)), usage=usage)
        if schema is GlobalSynthesisOut:
            return LLMResult(
                value=cast(
                    T,
                    GlobalSynthesisOut(
                        title="Widget Frobnicator Walkthrough",
                        tldr="A walkthrough of installing and tuning the Widget Frobnicator.",
                        abstract="The video demonstrates setup, configuration, advanced tuning "
                        "and exporting reports for the Widget Frobnicator.",
                        glossary=[
                            GlossaryOut(
                                term="Widget Frobnicator",
                                definition="The product being demonstrated.",
                                first_seen_s=0.0,
                            )
                        ],
                        open_questions=["Which cache size suits very large workloads?"],
                        tags=["Widget Frobnicator", "tutorial", "setup"],
                    ),
                ),
                usage=usage,
            )
        raise AssertionError(f"FakeText does not support {schema.__name__}")

    def _proposal(self, w0: float, w1: float) -> ChapterProposalOut:
        span = w1 - w0
        if self.proposal_mode == "empty":
            return ChapterProposalOut(chapters=[])
        if span < 20:
            return ChapterProposalOut(
                chapters=[ChapterBoundaryOut(title="Whole video", start=w0, end=w1)]
            )
        mid = w0 + span / 2
        if self.proposal_mode == "gap":
            return ChapterProposalOut(
                chapters=[
                    ChapterBoundaryOut(title="Introduction", start=w0 + 0.2, end=mid - 2),
                    ChapterBoundaryOut(title="Advanced tuning", start=mid + 3, end=w1 - 1),
                ]
            )
        if self.proposal_mode == "beyond":
            return ChapterProposalOut(
                chapters=[
                    ChapterBoundaryOut(title="Introduction", start=w0, end=mid),
                    ChapterBoundaryOut(title="Advanced tuning", start=mid, end=w1 + 500),
                ]
            )
        return ChapterProposalOut(
            chapters=[
                ChapterBoundaryOut(title="Introduction and setup", start=w0, end=mid),
                ChapterBoundaryOut(title="Advanced tuning", start=mid, end=w1),
            ]
        )

    def _detail(self, user: str) -> ChapterDetailOut:
        transcript = _between(user, "TRANSCRIPT:\n", "\n\nFRAME DESCRIPTIONS:")
        lines = re.findall(r"^\[(\d\d:\d\d:\d\d)\] (.*)$", transcript, re.M)
        quotes = [QuoteOut(t=_ts_to_s(ts), text=txt) for ts, txt in lines[:2]]
        quotes.append(QuoteOut(t=0.0, text="This sentence was never said in the video."))
        title = _between(user, "CHAPTER: ", "\n").strip()
        return ChapterDetailOut(
            summary=f"This chapter covers {title.lower()}.",
            key_points=["Open the dashboard", "Adjust the settings", "Restart to apply"],
            quotes=quotes,
            entities=[EntityOut(name="Widget Frobnicator", kind="product")],
            decisions_claims=["Settings are stored locally."],
            visual_summary="The dashboard of the Widget Frobnicator is shown throughout.",
        )


def build_fake_providers(delay: float = 0.0) -> ProviderBundle:
    return ProviderBundle(
        transcriber=FakeTranscriber(delay=delay),
        vision=FakeVision(delay=delay),
        text=FakeText(delay=delay),
    )
