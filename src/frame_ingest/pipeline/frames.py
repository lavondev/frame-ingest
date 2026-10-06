"""Stage 4: frame selection.

Scene-change detection (ffmpeg select='gt(scene,X)' + showinfo timestamps) proposes frames at
cuts; a coverage interval adds frames to static stretches; frames are downscaled JPEGs deduped
by perceptual hash and capped (evenly, never dropping the first frame).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import imagehash
from PIL import Image
from pydantic import BaseModel

from frame_ingest.ffmpeg import run_ffmpeg
from frame_ingest.models import FrameInfo, StageName
from frame_ingest.pipeline.context import PipelineContext, StageResult
from frame_ingest.pipeline.timefmt import FRAME_NAME_RE, frame_filename
from frame_ingest.pipeline.util import run_units

NAME = StageName.FRAMES
VERSION = 1
DEPS: list[StageName] = [StageName.PROBE]

_SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9.]+)")
_PTS_RE = re.compile(r"showinfo.*?pts_time:\s*([0-9.]+)")
Reason = Literal["first", "scene", "interval"]


class SceneCut(BaseModel):
    t: float
    score: float | None = None


@dataclass
class Candidate:
    t: float
    reason: Reason
    score: float | None = None


class FramesResult(StageResult):
    frames: list[FrameInfo] = []
    scene_cuts: int = 0
    candidates: int = 0
    duplicates_dropped: int = 0


# ── scene detection ─────────────────────────────────────────────────────────
def parse_scene_output(stderr: str) -> list[SceneCut]:
    """The select filter reports each cut as a showinfo line; metadata=print puts the score on
    the line just before it."""
    cuts: list[SceneCut] = []
    score: float | None = None
    for line in stderr.splitlines():
        sm = _SCORE_RE.search(line)
        if sm:
            score = float(sm.group(1))
            continue
        pm = _PTS_RE.search(line)
        if pm and "n:" in line:
            cuts.append(SceneCut(t=float(pm.group(1)), score=score))
            score = None
    return cuts


async def detect_scenes(job_dir: Path, video_path: Path, threshold: float) -> list[SceneCut]:
    """Free, local. Cached per threshold in frames/scenecuts.json (shared with /estimate)."""
    cache = job_dir / "frames" / "scenecuts.json"
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("threshold") == threshold:
                return [SceneCut.model_validate(c) for c in data["cuts"]]
        except (ValueError, KeyError):
            pass
    vf = (
        f"scale=320:-2,select='gt(scene,{threshold})',metadata=print:key=lavfi.scene_score,showinfo"
    )
    res = await run_ffmpeg(
        ["-i", str(video_path), "-an", "-sn", "-vf", vf, "-f", "null", "-"],
        loglevel="info",
        timeout=3 * 3600,
    )
    cuts = parse_scene_output(res.stderr)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"threshold": threshold, "cuts": [c.model_dump() for c in cuts]}),
        encoding="utf-8",
    )
    return cuts


# ── candidate planning (pure) ───────────────────────────────────────────────
def plan_candidates(
    duration: float, cuts: list[SceneCut], min_interval: float, settle: float = 0.15
) -> list[Candidate]:
    """First frame + one frame just after every scene cut (cuts closer than 1 s collapse) +
    interval frames wherever the gap between neighbours exceeds `min_interval`."""
    if duration <= 0:
        return []
    last_t = max(0.0, duration - 0.1)
    cands = [Candidate(min(0.5, duration / 4), "first")]
    for cut in sorted(cuts, key=lambda c: c.t):
        t = min(cut.t + settle, last_t)
        if t - cands[-1].t >= 1.0:
            cands.append(Candidate(t, "scene", cut.score))
    filled: list[Candidate] = []
    bounds = [*cands[1:], None]
    cur = cands[0]
    filled.append(cur)
    for nxt in bounds:
        end_t = nxt.t if nxt else duration
        t = cur.t + min_interval
        while end_t - t >= min_interval * 0.5 and t < last_t:
            filled.append(Candidate(t, "interval"))
            t += min_interval
        if nxt:
            filled.append(nxt)
            cur = nxt
    return filled


def thin_to_cap(items: list[Candidate], cap: int, duration: float) -> list[Candidate]:
    """Drop frames until `cap` remain, always removing the one whose neighbours are closest
    (scene frames are weighted as more valuable than interval fillers). Keeps the first frame."""
    items = list(items)
    if len(items) > cap * 4:  # keep strongest scene cuts before the O(n^2) pass
        keep_scene = sorted(
            (c for c in items[1:] if c.reason == "scene"), key=lambda c: -(c.score or 0)
        )[: cap * 3]
        keep_ids = {id(c) for c in keep_scene}
        fillers = [c for c in items[1:] if c.reason != "scene"]
        step = max(1, len(fillers) // max(1, cap))
        keep_ids |= {id(c) for c in fillers[::step]}
        items = [items[0], *[c for c in items[1:] if id(c) in keep_ids]]
    while len(items) > max(cap, 1):
        best_i, best_cost = -1, float("inf")
        for i in range(1, len(items)):
            nxt = items[i + 1].t if i + 1 < len(items) else duration
            cost = (nxt - items[i - 1].t) * (1.5 if items[i].reason == "scene" else 1.0)
            if cost < best_cost:
                best_i, best_cost = i, cost
        if best_i < 0:
            break
        del items[best_i]
    return items


def dedupe_hashes(hashes: list[imagehash.ImageHash], distance: int) -> list[bool]:
    """Keep-mask: a frame is dropped when its perceptual hash is within `distance` bits of the
    previously kept frame (static scenes / false cuts)."""
    keep: list[bool] = []
    last: imagehash.ImageHash | None = None
    for h in hashes:
        if last is not None and (h - last) <= distance:
            keep.append(False)
        else:
            keep.append(True)
            last = h
    return keep


# ── extraction ──────────────────────────────────────────────────────────────
async def _extract(video: Path, t: float, out: Path, max_px: int) -> bool:
    vf = (
        f"scale=w='min({max_px},iw)':h='min({max_px},ih)'"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    res = await run_ffmpeg(
        [
            "-y",
            "-ss",
            f"{t:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-vf",
            vf,
            "-q:v",
            "3",
            str(out),
        ],
        timeout=300,
    )
    return res.returncode == 0 and out.is_file() and out.stat().st_size > 0


def key_params(ctx: PipelineContext) -> dict[str, object]:
    s, c = ctx.settings, ctx.config
    return {
        "threshold": s.scene_threshold,
        "interval": s.min_interval_s,
        "cap": s.frame_cap,
        "max_px": c.max_image_px,
        "dedupe": c.dedupe_hash_distance,
    }


def skip_reason(ctx: PipelineContext) -> str | None:
    return None


def validate_cached(ctx: PipelineContext, result: FramesResult) -> bool:
    fdir = ctx.job_dir / "frames"
    return all((fdir / f.name).is_file() for f in result.frames)


async def run(ctx: PipelineContext) -> FramesResult:
    s, cfg = ctx.settings, ctx.config
    fdir = ctx.job_dir / "frames"
    fdir.mkdir(parents=True, exist_ok=True)
    for old in fdir.iterdir():
        if FRAME_NAME_RE.match(old.name):
            old.unlink(missing_ok=True)

    ctx.progress(0, 1, "Detecting scene changes")
    cuts = await detect_scenes(ctx.job_dir, ctx.video_path, s.scene_threshold)
    ctx.log(f"{len(cuts)} scene change(s) detected")

    duration = ctx.video.duration_s
    cands = plan_candidates(duration, cuts, s.min_interval_s)
    n_cands = len(cands)
    cands = thin_to_cap(cands, s.frame_cap * 2, duration)  # headroom for the dedupe pass
    total = len(cands)
    ctx.progress(0, total, f"Extracting {total} candidate frame(s)")

    done = 0

    async def grab(c: Candidate) -> tuple[Candidate, Path | None]:
        nonlocal done
        out = fdir / frame_filename(c.t)
        ok = await _extract(ctx.video_path, c.t, out, cfg.max_image_px)
        done += 1
        ctx.progress(done, total)
        return c, (out if ok else None)

    grabbed = await run_units(cands, grab, 4)
    usable = [(c, p) for c, p in grabbed if p is not None]
    if len(usable) < len(grabbed):
        ctx.warn(
            "frame_extract_failed", f"{len(grabbed) - len(usable)} frame(s) could not be decoded."
        )
    if not usable:
        ctx.warn("no_frames", "No frames could be extracted from this video.")
        return FramesResult(candidates=n_cands, scene_cuts=len(cuts))

    hashes = []
    dims: list[tuple[int, int]] = []
    for _, p in usable:
        with Image.open(p) as im:
            hashes.append(imagehash.phash(im))
            dims.append(im.size)
    keep = dedupe_hashes(hashes, cfg.dedupe_hash_distance)
    keep[0] = True
    kept = [(c, p, d) for (c, p), d, k in zip(usable, dims, keep, strict=True) if k]
    for (_, p), k in zip(usable, keep, strict=True):
        if not k:
            p.unlink(missing_ok=True)
    dropped = len(usable) - len(kept)

    if len(kept) > s.frame_cap:
        survivors = thin_to_cap([c for c, _, _ in kept], s.frame_cap, duration)
        ids = {id(c) for c in survivors}
        for c, p, _ in kept:
            if id(c) not in ids:
                p.unlink(missing_ok=True)
        kept = [k for k in kept if id(k[0]) in ids]

    frames = [
        FrameInfo(
            name=p.name,
            t=c.t,
            reason=c.reason,
            scene_score=c.score,
            width=d[0],
            height=d[1],
        )
        for c, p, d in kept
    ]
    for f in frames:
        ctx.emit("frame", f.model_dump(mode="json"))
    ctx.progress(total, total, f"{len(frames)} frame(s) kept ({dropped} near-duplicates dropped)")
    return FramesResult(
        frames=frames, scene_cuts=len(cuts), candidates=n_cands, duplicates_dropped=dropped
    )
