"""Local speech-to-text with faster-whisper (optional extra: `uv tool install ".[local]"`).

Runs in this process, so no audio leaves the machine. Weights download from Hugging Face on first
use unless `offline` is set, in which case only already-cached weights are used.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
from pathlib import Path
from typing import Any

from frame_ingest.errors import FatalProviderError, ProviderError
from frame_ingest.guard.paths import PathRejected, require_regular_file
from frame_ingest.providers.base import (
    RawSegment,
    RawTranscription,
    TranscriberCaps,
    UsageDelta,
)

INSTALL_HINT = 'Install the local extra: uv tool install ".[local]" (from the repository).'


def is_available() -> bool:
    try:
        importlib.import_module("faster_whisper")
    except ImportError:
        return False
    return True


SAMPLE_RATE = 16000
_MAX_PCM_BYTES = 512 * 1024 * 1024  # 32 minutes of 16 kHz float samples


async def _decode_pcm(audio: Path) -> Any:
    """Audio file -> mono 16 kHz float32 samples, decoded by our guarded, sandboxed ffmpeg.

    faster-whisper would otherwise decode with PyAV inside this process, outside the ffmpeg
    allowlist and sandbox."""
    import numpy as np

    from frame_ingest.ffmpeg import run_ffmpeg

    res = await run_ffmpeg(
        ["-i", str(audio), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
        timeout=900,
        max_stdout=_MAX_PCM_BYTES,
    )
    if res.returncode != 0 or not res.stdout:
        raise ProviderError("The audio chunk could not be decoded for transcription.")
    return np.frombuffer(res.stdout, dtype=np.float32)


class FasterWhisperTranscriber:
    def __init__(
        self,
        model_name: str,
        *,
        download_root: Path,
        offline: bool,
        device: str = "auto",
        compute_type: str = "auto",
    ) -> None:
        self.model_name = model_name
        self.download_root = download_root
        self.offline = offline
        self.device = device
        self.compute_type = compute_type
        self._model: Any = None
        self._load_lock = threading.Lock()
        self._run_lock = asyncio.Lock()  # one decode at a time: the model is not shared safely

    def caps_for(self, model: str) -> TranscriberCaps:
        return TranscriberCaps(
            segment_timestamps=True, prompt=True, keywords=False, diarization=False
        )

    def _load(self) -> Any:
        with self._load_lock:
            if self._model is None:
                try:
                    module = importlib.import_module("faster_whisper")
                except ImportError as exc:
                    raise FatalProviderError(
                        f"faster-whisper is not installed. {INSTALL_HINT}",
                        code="missing_dependency",
                        status=400,
                    ) from exc
                try:
                    self._model = module.WhisperModel(
                        self.model_name,
                        device=self.device,
                        compute_type=self.compute_type,
                        download_root=str(self.download_root),
                        local_files_only=self.offline,
                    )
                except Exception as exc:
                    hint = (
                        " The weights are not cached; run once without --offline to download."
                        if self.offline
                        else ""
                    )
                    raise FatalProviderError(
                        f"Could not load the speech model '{self.model_name}': "
                        f"{type(exc).__name__}.{hint}",
                        code="model_unavailable",
                        status=400,
                    ) from exc
            return self._model

    def _decode(self, samples: Any, prompt: str | None, language: str | None) -> RawTranscription:
        model = self._load()
        try:
            segments, info = model.transcribe(
                samples,
                language=language,
                initial_prompt=prompt,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            out = [
                RawSegment(start=float(s.start), end=float(s.end), text=s.text.strip())
                for s in segments
                if s.text.strip()
            ]
        except FatalProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Local transcription failed: {type(exc).__name__}.") from exc
        return RawTranscription(
            segments=out,
            language=getattr(info, "language", None),
            precision="segment",
            usage=UsageDelta(),
        )

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
        try:
            require_regular_file(audio, what="audio chunk")
        except PathRejected as exc:
            raise ProviderError(exc.message) from None
        samples = await _decode_pcm(audio)
        async with self._run_lock:
            res = await asyncio.to_thread(self._decode, samples, prompt, language)
        res.usage.audio_seconds = duration_s
        return res
