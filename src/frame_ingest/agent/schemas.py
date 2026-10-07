"""What the host agent writes in agent mode. The first two reuse the pipeline's model-facing
schemas, so a document made through an agent has passed the same shape checks as one made by
a provider; the synthesis folds the pipeline's three synthesis calls into one file.

Published as JSON Schemas under skills/frame-ingest/references/schemas/ (a test keeps them in
sync with these models).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from frame_ingest.llm_schemas import (
    CorrectionOut,
    EntityOut,
    GlossaryOut,
    QuoteOut,
    VisionBatchOut,
)


class AgentChapter(BaseModel):
    title: str
    start: float
    end: float
    summary: str
    key_points: list[str]
    quotes: list[QuoteOut]
    entities: list[EntityOut]
    decisions_claims: list[str]
    visual_summary: str


class AgentSynthesis(BaseModel):
    title: str
    tldr: str
    abstract: str
    glossary: list[GlossaryOut]
    open_questions: list[str]
    tags: list[str]
    chapters: list[AgentChapter]


SCHEMAS: dict[str, type[BaseModel]] = {
    "vision-batch": VisionBatchOut,
    "corrections": CorrectionOut,
    "synthesis": AgentSynthesis,
}


def schema_text(name: str) -> str:
    return json.dumps(SCHEMAS[name].model_json_schema(), indent=2, sort_keys=True) + "\n"


def write_schemas(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in SCHEMAS:
        (directory / f"{name}.schema.json").write_text(schema_text(name), encoding="utf-8")
