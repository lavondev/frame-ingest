import os
import re
from datetime import UTC, datetime
from pathlib import Path

from frame_ingest.models import (
    Analysis,
    Chapter,
    Entity,
    FailedBatch,
    FrameAnalysis,
    FrameInfo,
    GlossaryEntry,
    ProcessingNotes,
    ProcessingWarning,
    Quote,
    ResolvedSettings,
    SceneType,
    Segment,
    StageName,
    TokenUsage,
    Transcript,
    VideoInfo,
    VideoSynthesis,
)
from frame_ingest.pipeline.assemble import (
    build_coverage,
    build_entity_index,
    check_timestamps,
    inline,
    render_markdown,
)

GOLDEN = Path(__file__).parent / "golden" / "analysis.md"
NOW = datetime(2026, 10, 4, 12, 30, 5, tzinfo=UTC)


def make_analysis(has_audio: bool = True) -> Analysis:
    segs = (
        [
            Segment(
                id=0,
                start=1,
                end=6,
                raw_text="Welcome to the Widjet Frobnicator.",
                corrected_text="Welcome to the Widget Frobnicator.",
            ),
            Segment(
                id=1,
                start=6,
                end=12,
                raw_text="Open the dashboard first.",
                corrected_text="Open the dashboard first.",
            ),
            Segment(
                id=2,
                start=50,
                end=58,
                raw_text="Now tune the cache size.",
                corrected_text="Now tune the cache size.",
            ),
            Segment(
                id=3,
                start=50.4,
                end=60,
                raw_text="It matters a lot.",
                corrected_text="It matters a lot.",
            ),
        ]
        if has_audio
        else []
    )
    chapters = [
        Chapter(
            index=1,
            id="ch-01",
            title="Setup",
            start=0,
            end=45,
            summary="How to install the product.\n# not a heading",
            key_points=["Open dashboard", "Pick settings"],
            quotes=[Quote(t=6, text="Open the dashboard first.")],
            entities=[Entity(name="Widget Frobnicator", kind="product")],
            decisions_claims=["Settings are stored locally."],
            visual_description="A dashboard is shown.",
            on_screen_text=["Widget Frobnicator", "Step 1"],
            frames=["frame_0001.00.jpg"],
        ),
        Chapter(
            index=2,
            id="ch-02",
            title="Tuning",
            start=45,
            end=90,
            summary="Tune the cache.",
            key_points=["Cache size matters"],
            quotes=[],
            entities=[Entity(name="Cache", kind="concept")],
            decisions_claims=[],
            visual_description="",
            on_screen_text=[],
            frames=["frame_0050.00.jpg"],
        ),
    ]
    scenes = [
        FrameAnalysis(
            frame="frame_0001.00.jpg",
            t=1,
            scene_description="Dashboard home screen.",
            on_screen_text=["Widget Frobnicator", "Step 1"],
            change_from_previous="First frame",
            entities=[Entity(name="Widget Frobnicator", kind="product")],
            scene_type=SceneType.SLIDE,
        ),
        FrameAnalysis(
            frame="frame_0050.00.jpg",
            t=50,
            scene_description="Cache settings panel.",
            on_screen_text=[],
            change_from_previous="Panel changed",
            entities=[],
            scene_type=SceneType.SCREEN_RECORDING,
        ),
    ]
    frames = [
        FrameInfo(
            name=s.frame, t=s.t, reason="first" if s.t == 1 else "scene", width=320, height=240
        )
        for s in scenes
    ]
    notes = ProcessingNotes(
        started_at=NOW,
        finished_at=NOW,
        stages_run=[StageName.PROBE, StageName.TRANSCRIBE, StageName.VISION],
        stages_cached=[StageName.FRAMES],
        stages_skipped=[] if has_audio else [StageName.TRANSCRIBE],
        models={
            "transcribe": "whisper-1",
            "vision": "v-model",
            "correct": "c-model",
            "synthesize": "s-model",
        },
        frame_count=2,
        corrections_changed=1,
        usage=[
            TokenUsage(
                stage=StageName.VISION,
                model="v-model",
                calls=1,
                input_tokens=300,
                output_tokens=120,
            )
        ],
        warnings=[
            ProcessingWarning(
                stage=StageName.VISION,
                code="vision_gap",
                message="Vision analysis failed for 1 frame(s).",
            )
        ],
        failed_batches=[
            FailedBatch(stage=StageName.VISION, index=3, t_start=70, t_end=80, error="timeout")
        ],
    )
    settings = ResolvedSettings(
        frame_cap=100,
        scene_threshold=0.3,
        min_interval_s=10,
        image_detail="auto",
        context="",
        language=None,
        diarize=False,
        transcribe_model="whisper-1",
        vision_model="v-model",
        correct_model="c-model",
        synthesize_model="s-model",
    )
    transcript = Transcript(
        model="whisper-1" if has_audio else None,
        language="en",
        timestamp_precision="segment" if has_audio else "none",
        chunk_count=1,
        segments=segs,
    )
    syn = VideoSynthesis(
        title="Widget Frobnicator Walkthrough",
        tldr="A short setup and tuning tutorial.",
        abstract="Covers setup then cache tuning.",
        glossary=[GlossaryEntry(term="Cache", definition="Fast temporary storage.", first_seen=50)],
        open_questions=["What cache size for huge workloads?"],
        tags=["tutorial", "setup"],
    )
    video = VideoInfo(
        filename="demo video.mp4",
        size_bytes=1000,
        sha256="ab" * 32,
        duration_s=90,
        width=320,
        height=240,
        fps=10,
        video_codec="h264",
        has_audio=has_audio,
        audio_codec="aac",
    )
    return Analysis(
        analyzed_at=NOW,
        video=video,
        settings=settings,
        transcript=transcript,
        frames=frames,
        scenes=scenes,
        chapters=chapters,
        synthesis=syn,
        entity_index=build_entity_index(chapters, scenes, segs),
        notes=notes,
        coverage=build_coverage(has_audio, "asr", bool(segs), 2, 2, chapters, 1, None),
    )


def test_markdown_matches_golden_file() -> None:
    md = render_markdown(make_analysis())
    if os.environ.get("UPDATE_GOLDEN") or not GOLDEN.exists():
        GOLDEN.write_text(md)
    assert md == GOLDEN.read_text()


def test_structure_anchors_and_restated_context() -> None:
    md = render_markdown(make_analysis())
    assert md.startswith("---\n") and "chapter_count: 2" in md and "has_audio: true" in md
    for h in (
        "## TL;DR",
        "## Abstract",
        "## Table of Contents",
        "## Glossary",
        "## Entity Index",
        "## Open Questions",
        "## Appendix: Processing Notes",
    ):
        assert h in md
    assert "## Chapter 1: Setup [00:00:00 - 00:00:45] {#ch-01}" in md
    assert "## Chapter 2: Tuning [00:00:45 - 00:01:30] {#ch-02}" in md
    # every chapter subsection restates the chapter title + range so it is retrievable alone
    for section in (
        "Summary",
        "Key Points",
        "Visual Description",
        "On-Screen Text",
        "Notable Quotes",
        "Corrected Transcript",
    ):
        assert f"### {section} — Chapter 1: Setup [00:00:00 - 00:00:45]" in md
    # every anchor link has a target
    ids = set(re.findall(r"\{#([\w-]+)\}", md)) | set(re.findall(r'<a id="([\w-]+)"', md))
    links = set(re.findall(r"\]\(#([\w-]+)\)", md))
    assert links <= ids, links - ids
    # transcript anchors are unique even for segments within the same second
    anchors = re.findall(r'<a id="(t-\d+(?:-\d+)?)"', md)
    assert len(anchors) == len(set(anchors)) == 4
    assert "Welcome to the Widget Frobnicator." in md and "Widjet" not in md


def test_no_timestamp_exceeds_duration() -> None:
    a = make_analysis()
    md = render_markdown(a)
    assert check_timestamps(md, a.video.duration_s) == []
    assert check_timestamps("**[00:05:00]** late\n## Appendix\n99:00:00", 90) == ["00:05:00"]


def test_no_audio_is_noted() -> None:
    md = render_markdown(make_analysis(has_audio=False))
    assert "no audio track" in md.lower() and "has_audio: false" in md
    assert "No audio track — transcript unavailable." in md


def test_model_text_cannot_inject_structure() -> None:
    md = render_markdown(make_analysis())
    assert "\n# not a heading" not in md  # neutralised, so the TOC / headings stay deterministic
    assert inline("# sneaky\nmultiple   lines") == "\\# sneaky multiple lines"


def test_appendix_flags_failed_batches_and_warnings() -> None:
    md = render_markdown(make_analysis())
    assert "Failed batches (1)" in md and "vision batch 3 (00:01:10 - 00:01:20): timeout" in md
    assert "Vision analysis failed for 1 frame(s)." in md
    assert "300 input, 120 output tokens" in md


def test_entity_index_is_computed_with_counts_and_sources() -> None:
    a = make_analysis()
    top = a.entity_index[0]
    assert top.name == "Widget Frobnicator" and top.count >= 3
    assert {m.source for m in top.mentions} == {"chapter", "visual", "transcript"}
    assert all(m.chapter_id in ("ch-01", "ch-02") for m in top.mentions)
