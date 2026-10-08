"""Pre-run estimate: probes, runs scene detection (free, local) and projects API calls, tokens
and (only when pricing.yaml is filled in) dollars. All token figures are heuristics and labelled
as such; actual usage is recorded per stage from the API responses."""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

from frame_ingest.config import AppConfig, Pricing
from frame_ingest.models import Estimate, ResolvedSettings, StageEstimate, StageName, VideoInfo
from frame_ingest.pipeline.audio import plan_chunks
from frame_ingest.pipeline.frames import detect_scenes, plan_candidates
from frame_ingest.pipeline.synthesize import chapter_target_range
from frame_ingest.providers.base import TranscriberCaps

CALL_OVERHEAD = 700  # system prompt + instructions per call
FRAME_OUT_TOKENS = 230


async def build_estimate(
    config: AppConfig,
    settings: ResolvedSettings,
    video: VideoInfo,
    job_dir: Path,
    video_path: Path,
    caps_for: Callable[[str], TranscriberCaps],
    pricing: Pricing,
) -> Estimate:
    d = video.duration_s
    cuts = await detect_scenes(
        job_dir,
        video_path,
        settings.scene_threshold,
        still_min_s=config.still_min_s,
        still_noise_db=config.still_noise_db,
    )
    cands = plan_candidates(d, cuts, settings.min_interval_s)
    frames = min(len(cands), settings.frame_cap)

    minutes = d / 60
    speech_tokens = int(minutes * config.speech_tokens_per_minute) if video.has_audio else 0
    img_tokens = getattr(config.image_tokens, settings.image_detail)
    stages: dict[StageName, StageEstimate] = {s: StageEstimate() for s in StageName}
    assumptions = [
        f"Frames: upper bound after scene detection ({len(cuts)} cut(s), coverage every "
        f"{settings.min_interval_s:g}s, cap {settings.frame_cap}); near-duplicate removal can "
        "only lower it.",
        f"Speech assumed at {config.speech_tokens_per_minute} tokens/minute.",
        f"Each image assumed at ~{img_tokens} input tokens ({settings.image_detail} detail); "
        "real usage is reported after the run.",
    ]

    # transcription
    caps = caps_for(config.models.diarize if settings.diarize else settings.transcribe_model)
    if video.has_audio:
        chunk_s = config.chunk_minutes * 60 if caps.segment_timestamps else config.short_chunk_s
        overlap = config.chunk_overlap_s if caps.segment_timestamps else 0.0
        n_chunks = len(plan_chunks(d, chunk_s, overlap))
        stages[StageName.TRANSCRIBE] = StageEstimate(
            api_calls=n_chunks, output_tokens=speech_tokens, audio_minutes=round(minutes, 2)
        )
        if not caps.segment_timestamps:
            assumptions.append(
                f"{settings.transcribe_model} returns no segment timestamps, so audio is split "
                "into "
                f"~{config.short_chunk_s:g}s chunks and timestamps are chunk boundaries."
            )

    # vision
    batches = math.ceil(frames / config.vision_batch_size) if frames else 0
    stages[StageName.VISION] = StageEstimate(
        api_calls=batches,
        input_tokens=frames * img_tokens
        + batches * (CALL_OVERHEAD + config.image_tokens.low)
        + int(speech_tokens * 1.15),
        output_tokens=frames * FRAME_OUT_TOKENS,
    )

    # correction
    if video.has_audio and speech_tokens:
        est_segments = max(1, int(d / 6))
        windows = math.ceil(est_segments / config.correction_window)
        stages[StageName.CORRECT] = StageEstimate(
            api_calls=windows,
            input_tokens=int(
                speech_tokens * (1 + 2 * config.correction_context / config.correction_window)
            )
            + windows * (CALL_OVERHEAD + 1500),
            output_tokens=int(speech_tokens * 1.05),
        )

    # synthesis
    lo, hi = chapter_target_range(d, config.max_chapters)
    n_ch = max(1, (lo + hi) // 2)
    visual_tokens = frames * 60
    per_ch_in = (speech_tokens + visual_tokens) // n_ch + CALL_OVERHEAD
    stages[StageName.SYNTHESIZE] = StageEstimate(
        api_calls=n_ch + 2,
        input_tokens=(speech_tokens + visual_tokens + CALL_OVERHEAD)
        + n_ch * per_ch_in
        + n_ch * 250
        + CALL_OVERHEAD,
        output_tokens=n_ch * 40 + 100 + n_ch * 450 + 700,
    )
    assumptions.append(f"Chapters assumed ≈{n_ch} (target range {lo}-{hi}).")

    est = Estimate(
        duration_s=round(d, 2),
        has_audio=video.has_audio,
        scene_cuts=len(cuts),
        frames=frames,
        stages=stages,
        total_api_calls=sum(s.api_calls for s in stages.values()),
        total_input_tokens=sum(s.input_tokens for s in stages.values()),
        total_output_tokens=sum(s.output_tokens for s in stages.values()),
        assumptions=assumptions,
    )
    est.cost_usd, est.cost_note = _cost(est, settings, pricing, config)
    return est


def _cost(
    est: Estimate, settings: ResolvedSettings, pricing: Pricing, config: AppConfig
) -> tuple[float | None, str | None]:
    if not pricing.configured:
        return None, "Add prices to pricing.yaml to see a dollar estimate."
    total = 0.0
    missing: list[str] = []
    tm = config.models.diarize if settings.diarize else settings.transcribe_model
    audio_min = est.stages[StageName.TRANSCRIBE].audio_minutes
    if audio_min:
        price = pricing.transcription_per_minute.get(tm)
        if price is None:
            missing.append(f"transcription_per_minute.{tm}")
        else:
            total += price * audio_min
    per_stage = {
        StageName.VISION: settings.vision_model,
        StageName.CORRECT: settings.correct_model,
        StageName.SYNTHESIZE: settings.synthesize_model,
    }
    for stage, model in per_stage.items():
        e = est.stages[stage]
        if not (e.input_tokens or e.output_tokens):
            continue
        p = pricing.text_models.get(model, {})
        pin, pout = p.get("input_per_mtok"), p.get("output_per_mtok")
        if pin is None or pout is None:
            missing.append(f"text_models.{model}")
            continue
        total += e.input_tokens / 1e6 * pin + e.output_tokens / 1e6 * pout
    if missing:
        return None, "pricing.yaml is missing: " + ", ".join(sorted(set(missing)))
    return round(total, 4), "Estimate from pricing.yaml and the token heuristics above."
