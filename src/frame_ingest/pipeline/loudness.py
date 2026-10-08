"""Audio loudness, measured with ffmpeg's `volumedetect` filter through the guarded wrapper.

Used by the audio guarantee: when speech-to-text returns nothing for a track that is clearly not
silent, the voice-activity filter probably dropped speech (common with music under the voice),
so transcription is retried without it.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from pydantic import BaseModel

from frame_ingest.ffmpeg import run_ffmpeg

_MEAN = re.compile(r"mean_volume:\s*(-?inf|-?\d+(?:\.\d+)?) dB")
_MAX = re.compile(r"max_volume:\s*(-?inf|-?\d+(?:\.\d+)?) dB")
FLOOR_DB = -120.0  # stands in for -inf (digital silence) so the value stays JSON-friendly


class Loudness(BaseModel):
    mean_db: float
    max_db: float

    def silent(self, threshold_db: float) -> bool:
        """Near-silent: the average level is at or below `threshold_db` (dBFS)."""
        return self.mean_db <= threshold_db


def _db(match: re.Match[str] | None) -> float | None:
    if match is None:
        return None
    value = float(match.group(1))
    return FLOOR_DB if math.isinf(value) else max(FLOOR_DB, value)


def parse_volumedetect(stderr: str) -> Loudness | None:
    mean, peak = _db(_MEAN.search(stderr)), _db(_MAX.search(stderr))
    if mean is None or peak is None:
        return None
    return Loudness(mean_db=mean, max_db=peak)


async def measure(media: Path) -> Loudness | None:
    """Loudness of the first audio stream of `media` (a file inside the job directory).

    Returns None when ffmpeg could not measure it (no audio stream, undecodable audio)."""
    res = await run_ffmpeg(
        ["-i", str(media), "-vn", "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
        loglevel="info",
        timeout=1800,
    )
    if res.returncode != 0:
        return None
    return parse_volumedetect(res.stderr)
