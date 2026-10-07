"""OpenAI-compatible provider implementations (transcription, vision, text).

Everything goes through the official `openai` SDK against Chat Completions / audio
transcriptions, so pointing OPENAI_BASE_URL (or per-role base_urls) at a vLLM/RunPod endpoint is
a config change. Retries (tenacity) and error translation live here; the pipeline never sees an
SDK exception, only FrameIngestError subclasses with human-readable, key-free messages.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from frame_ingest.capabilities import CapabilityMemo
from frame_ingest.config import AppConfig, known_caps
from frame_ingest.errors import (
    CapabilityChanged,
    FatalProviderError,
    ProviderError,
    ValidationFailure,
    redact,
)
from frame_ingest.guard.paths import read_bytes_nofollow
from frame_ingest.llm_schemas import VisionBatchOut
from frame_ingest.models import StageName
from frame_ingest.providers.base import (
    LLMResult,
    ProviderBundle,
    RawSegment,
    RawTranscription,
    TranscriberCaps,
    UsageDelta,
    VisionBatchRequest,
)

log = logging.getLogger("frame_ingest.openai")
T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")
MAX_VALIDATION_RETRIES = 2


# ── schema ──────────────────────────────────────────────────────────────────
def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema -> OpenAI strict-mode schema: every object closed, every property required,
    no defaults."""
    schema = copy.deepcopy(model.model_json_schema())

    def fix(node: Any) -> None:
        if not isinstance(node, dict):
            return
        node.pop("default", None)
        props = node.get("properties")
        if isinstance(props, dict):
            node["additionalProperties"] = False
            node["required"] = list(props.keys())
            for child in props.values():
                fix(child)
        for key in ("items", "additionalProperties"):
            if isinstance(node.get(key), dict):
                fix(node[key])
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for child in node.get(key, []) or []:
                fix(child)

    fix(schema)
    for definition in (schema.get("$defs") or {}).values():
        fix(definition)
    return schema


# ── errors / retry ──────────────────────────────────────────────────────────
def _is_quota(exc: BaseException) -> bool:
    return getattr(exc, "code", None) == "insufficient_quota" or "insufficient_quota" in str(exc)


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, openai.RateLimitError):
        return not _is_quota(exc)
    if isinstance(exc, openai.APIConnectionError | openai.InternalServerError):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code in (408, 409, 429, 500, 502, 503, 504)
    return False


def translate_error(exc: BaseException, *, model: str, base_url: str | None) -> Exception:
    """Map an SDK exception to a FrameIngestError (or return it unchanged if it is not ours)."""
    where = base_url or "api.openai.com"
    if isinstance(exc, openai.AuthenticationError):
        return FatalProviderError(
            "OpenAI rejected the API key (invalid, revoked or for another endpoint). "
            "Check the OPENAI_API_KEY environment variable.",
            code="invalid_api_key",
            status=401,
        )
    if isinstance(exc, openai.PermissionDeniedError):
        return FatalProviderError(
            f"Your API key is not allowed to use '{model}' (permission denied). "
            "Pick another model in config.yaml / FRAME_INGEST_MODEL_*.",
            code="model_unavailable",
            status=403,
        )
    if isinstance(exc, openai.NotFoundError):
        return FatalProviderError(
            f"Model '{model}' is not available at {where}. "
            "Change it in config.yaml or FRAME_INGEST_MODEL_*.",
            code="model_unavailable",
            status=404,
        )
    if isinstance(exc, openai.RateLimitError) and _is_quota(exc):
        return FatalProviderError(
            "OpenAI reports insufficient quota/credit for this API key. "
            "Add billing credit, then retry the job (finished stages are cached).",
            code="insufficient_quota",
            status=402,
        )
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError(f"Could not reach {where}: {redact(str(exc))}", code="unreachable")
    if isinstance(exc, openai.APIStatusError):
        return ProviderError(
            f"{where} returned HTTP {exc.status_code}: {redact(str(exc))[:300]}",
            code="provider_error",
        )
    return exc if isinstance(exc, Exception) else ProviderError(redact(str(exc)))


async def with_retries(
    fn: Callable[[], Awaitable[R]], *, attempts: int, model: str, base_url: str | None
) -> R:
    """Exponential backoff + jitter on rate limits and transient errors; fatal errors map to
    FatalProviderError immediately."""
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait_random_exponential(multiplier=1, max=30),
            retry=retry_if_exception(is_transient),
            reraise=True,
        ):
            with attempt:
                return await fn()
    except openai.OpenAIError as exc:
        raise translate_error(exc, model=model, base_url=base_url) from exc
    raise ProviderError("retry loop exited unexpectedly")  # pragma: no cover


# ── clients ─────────────────────────────────────────────────────────────────
def make_client(config: AppConfig, role: str) -> AsyncOpenAI:
    key = config.key_for(role)
    if not key:
        raise FatalProviderError(config.missing_key_message(), code="missing_api_key", status=400)
    return AsyncOpenAI(
        api_key=key,
        base_url=config.base_url_for(role),
        max_retries=0,  # tenacity owns retries so backoff is uniform and observable
        timeout=900.0 if role == "transcribe" else 300.0,
    )


class _ChatJSON:
    """Shared structured-output chat call with capability fallbacks."""

    def __init__(self, client: AsyncOpenAI, config: AppConfig, memo: CapabilityMemo, role: str):
        self.client = client
        self.config = config
        self.memo = memo
        self.base_url = config.base_url_for(role)

    async def call(
        self, schema: type[T], messages: list[dict[str, Any]], model: str
    ) -> LLMResult[T]:
        use_schema = self.memo.get(self.base_url, model, "json_schema", True)
        effort = self.config.reasoning_effort or None
        use_effort = bool(effort) and self.memo.get(self.base_url, model, "reasoning_effort", True)
        msgs = list(messages)
        usage = UsageDelta(calls=0)
        last_error = ""
        for _ in range(MAX_VALIDATION_RETRIES + 1):
            kwargs: dict[str, Any] = {"model": model, "messages": msgs}
            if use_schema:
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema.__name__,
                        "schema": strict_json_schema(schema),
                        "strict": True,
                    },
                }
            else:
                kwargs["response_format"] = {"type": "json_object"}
                kwargs["messages"] = [
                    *msgs,
                    {
                        "role": "user",
                        "content": "Reply with a single JSON object matching this JSON schema, "
                        "no prose:\n" + json.dumps(schema.model_json_schema()),
                    },
                ]
            if use_effort:
                kwargs["reasoning_effort"] = effort

            async def create(kw: dict[str, Any] = kwargs) -> Any:
                return await self.client.chat.completions.create(**kw)

            resp = await with_retries(
                create,
                attempts=self.config.retry_attempts,
                model=model,
                base_url=self.base_url,
            )
            usage.calls += 1
            if resp.usage:
                usage.input_tokens += resp.usage.prompt_tokens or 0
                usage.output_tokens += resp.usage.completion_tokens or 0
            choice = resp.choices[0]
            if getattr(choice.message, "refusal", None):
                raise ProviderError(f"The model refused the request: {choice.message.refusal}")
            content = choice.message.content or ""
            if choice.finish_reason == "length":
                last_error = "output was truncated (finish_reason=length)"
            else:
                try:
                    return LLMResult(
                        value=schema.model_validate_json(_strip_fences(content)), usage=usage
                    )
                except (ValidationError, ValueError) as exc:
                    last_error = f"invalid JSON for {schema.__name__}: {str(exc)[:300]}"
            msgs = [
                *msgs,
                {"role": "assistant", "content": content[:2000]},
                {
                    "role": "user",
                    "content": f"That reply was rejected ({last_error}). Reply again with valid "
                    "JSON matching the schema exactly.",
                },
            ]
        raise ValidationFailure(f"Model output kept failing validation: {last_error}")

    async def call_with_fallbacks(
        self, schema: type[T], messages: list[dict[str, Any]], model: str
    ) -> LLMResult[T]:
        """Handle endpoints that reject strict json_schema or reasoning_effort (one-time cost)."""
        while True:
            try:
                return await self.call(schema, messages, model)
            except ProviderError as exc:
                msg = str(exc).lower()
                if "reasoning_effort" in msg and self.memo.get(
                    self.base_url, model, "reasoning_effort", True
                ):
                    self.memo.set(self.base_url, model, "reasoning_effort", False)
                    continue
                if any(w in msg for w in ("json_schema", "response_format", "structured")) and (
                    self.memo.get(self.base_url, model, "json_schema", True)
                ):
                    self.memo.set(self.base_url, model, "json_schema", False)
                    continue
                raise


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()


class OpenAIText:
    def __init__(self, client: AsyncOpenAI, config: AppConfig, memo: CapabilityMemo) -> None:
        self._chat = _ChatJSON(client, config, memo, "text")

    async def complete_json(
        self, schema: type[T], *, system: str, user: str, stage: StageName, model: str
    ) -> LLMResult[T]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return await self._chat.call_with_fallbacks(schema, messages, model)


def _image_part(path: Path, detail: str) -> dict[str, Any]:
    b64 = base64.b64encode(read_bytes_nofollow(path)).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": detail},
    }


class OpenAIVision:
    def __init__(self, client: AsyncOpenAI, config: AppConfig, memo: CapabilityMemo) -> None:
        self._chat = _ChatJSON(client, config, memo, "vision")

    async def analyze(self, req: VisionBatchRequest) -> LLMResult[VisionBatchOut]:
        content: list[dict[str, Any]] = [{"type": "text", "text": req.preamble}]
        if req.context_frame is not None:
            content.append({"type": "text", "text": req.context_frame.label})
            content.append(_image_part(req.context_frame.path, "low"))
        for f in req.frames:
            content.append({"type": "text", "text": f.label})
            content.append(_image_part(f.path, req.detail))
        if req.postamble:
            content.append({"type": "text", "text": req.postamble})
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": req.system},
            {"role": "user", "content": content},
        ]
        return await self._chat.call_with_fallbacks(VisionBatchOut, messages, req.model)


class OpenAITranscriber:
    def __init__(self, client: AsyncOpenAI, config: AppConfig, memo: CapabilityMemo) -> None:
        self.client = client
        self.config = config
        self.memo = memo
        self.base_url = config.base_url_for("transcribe")

    def caps_for(self, model: str) -> TranscriberCaps:
        caps = known_caps(model)
        if not self.memo.get(self.base_url, model, "segment_timestamps", True):
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
        mime = "audio/mpeg" if audio.suffix == ".mp3" else "audio/ogg"
        data = read_bytes_nofollow(audio)
        kwargs: dict[str, Any] = {"model": model, "file": (audio.name, data, mime)}
        if language:
            kwargs["language"] = language
        if caps.diarization:
            kwargs["response_format"] = "diarized_json"
            kwargs["chunking_strategy"] = "auto"
        elif caps.segment_timestamps:
            kwargs["response_format"] = "verbose_json"
            kwargs["timestamp_granularities"] = ["segment"]
        else:
            kwargs["response_format"] = "json"
        if caps.prompt and prompt:
            kwargs["prompt"] = prompt
        if caps.keywords and keywords:
            kwargs["keywords"] = keywords

        try:
            resp = await with_retries(
                lambda: self.client.audio.transcriptions.create(**kwargs),
                attempts=self.config.retry_attempts,
                model=model,
                base_url=self.base_url,
            )
        except ProviderError as exc:
            msg = str(exc).lower()
            if (
                caps.segment_timestamps
                and not caps.diarization
                and ("verbose_json" in msg or "response_format" in msg)
            ):
                self.memo.set(self.base_url, model, "segment_timestamps", False)
                raise CapabilityChanged("segment_timestamps") from exc
            raise

        usage = UsageDelta(audio_seconds=duration_s)
        u = getattr(resp, "usage", None)
        if u is not None and getattr(u, "type", None) == "tokens":
            usage.input_tokens = int(getattr(u, "input_tokens", 0) or 0)
            usage.output_tokens = int(getattr(u, "output_tokens", 0) or 0)
        return _parse_transcription(resp, duration_s, usage)


def _language_code(value: Any) -> str | None:
    """OpenAI SDK ≥3.24 returns TranscriptionLanguage objects on json responses; verbose_json
    still uses str."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    code = getattr(value, "code", None)
    return code if isinstance(code, str) else None


def _parse_transcription(resp: Any, duration_s: float, usage: UsageDelta) -> RawTranscription:
    text = (getattr(resp, "text", None) or "").strip()
    language = _language_code(getattr(resp, "language", None))
    if language is None:
        langs = getattr(resp, "languages", None) or []
        language = next((c for v in langs if (c := _language_code(v))), None)
    segs = getattr(resp, "segments", None) or []
    out: list[RawSegment] = []
    for s in segs:
        t = (getattr(s, "text", "") or "").strip()
        if t:
            out.append(
                RawSegment(
                    start=float(getattr(s, "start", 0.0) or 0.0),
                    end=float(getattr(s, "end", 0.0) or 0.0),
                    text=t,
                    speaker=getattr(s, "speaker", None),
                )
            )
    if out:
        return RawTranscription(segments=out, language=language, precision="segment", usage=usage)
    if text:  # no segments came back: the chunk boundaries are the timestamps
        return RawTranscription(
            segments=[RawSegment(start=0.0, end=duration_s, text=text)],
            language=language,
            precision="chunk",
            usage=usage,
        )
    return RawTranscription(segments=[], language=language, precision="chunk", usage=usage)


def build_openai_providers(config: AppConfig, memo: CapabilityMemo) -> ProviderBundle:
    return ProviderBundle(
        transcriber=OpenAITranscriber(make_client(config, "transcribe"), config, memo),
        vision=OpenAIVision(make_client(config, "vision"), config, memo),
        text=OpenAIText(make_client(config, "text"), config, memo),
    )


__all__ = [
    "OpenAIText",
    "OpenAITranscriber",
    "OpenAIVision",
    "build_openai_providers",
    "make_client",
    "strict_json_schema",
    "with_retries",
]
