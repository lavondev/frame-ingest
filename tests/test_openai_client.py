"""OpenAI provider behaviour against a mocked HTTP transport (no network, no key)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from PIL import Image
from tenacity import wait_none

from frame_ingest.capabilities import CapabilityMemo
from frame_ingest.errors import (
    CapabilityChanged,
    FatalProviderError,
    ProviderError,
    ValidationFailure,
    redact,
)
from frame_ingest.llm_schemas import CorrectionOut, GlobalSynthesisOut, VisionBatchOut
from frame_ingest.models import StageName
from frame_ingest.providers import openai_client as oc
from frame_ingest.providers.base import VisionBatchRequest, VisionFrame

KEY = "sk-test-SECRET-1234567890"
Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=KEY,
        base_url="http://mock.local/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
    )


def chat(content: str, finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "c",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish,
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        },
    )


def err(status: int, message: str, code: str | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": code,
            }
        },
    )


GOOD = json.dumps({"segments": [{"id": 1, "corrected_text": "hi"}]})


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "wait_random_exponential", lambda **_: wait_none())


def text_llm(config, handler: Handler, memo: CapabilityMemo | None = None) -> oc.OpenAIText:
    return oc.OpenAIText(make_client(handler), config, memo or CapabilityMemo())


async def ask(llm: oc.OpenAIText, schema: type = CorrectionOut) -> Any:
    return await llm.complete_json(
        schema, system="s", user="u", stage=StageName.CORRECT, model="test-model"
    )


# ── schema ───────────────────────────────────────────────────────────────────
def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


@pytest.mark.parametrize("model", [CorrectionOut, VisionBatchOut, GlobalSynthesisOut])
def test_strict_schema_closes_every_object_and_requires_all_props(model: type) -> None:
    schema = oc.strict_json_schema(model)
    objects = [n for n in _walk(schema) if "properties" in n]
    assert objects
    for o in objects:
        assert o["additionalProperties"] is False
        assert o["required"] == list(o["properties"])
    assert not any("default" in n for n in _walk(schema) if "properties" in n or "type" in n)


# ── structured output ────────────────────────────────────────────────────────
async def test_request_shape_and_usage(config) -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return chat(GOOD)

    res = await ask(text_llm(config, handler))
    body = seen[0]
    assert body["model"] == "test-model" and body["reasoning_effort"] == "low"
    assert "temperature" not in body
    assert (
        body["response_format"]["type"] == "json_schema"
        and body["response_format"]["json_schema"]["strict"] is True
    )
    assert res.value.segments[0].id == 1
    assert (res.usage.calls, res.usage.input_tokens, res.usage.output_tokens) == (1, 11, 7)


async def test_falls_back_when_endpoint_rejects_json_schema_and_remembers(config) -> None:
    calls: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        if body["response_format"]["type"] == "json_schema":
            return err(
                400,
                "Invalid parameter: response_format of type json_schema is not supported with this model.",
            )
        return chat(GOOD)

    memo = CapabilityMemo()
    llm = text_llm(config, handler, memo)
    assert (await ask(llm)).value.segments
    assert [c["response_format"]["type"] for c in calls] == ["json_schema", "json_object"]
    assert (
        "JSON schema" in calls[1]["messages"][-1]["content"]
    )  # schema is described in the prompt instead
    assert memo.get(None, "test-model", "json_schema") is False
    calls.clear()
    await ask(llm)
    assert [c["response_format"]["type"] for c in calls] == ["json_object"]  # no repeat 400


async def test_drops_reasoning_effort_when_unsupported(config) -> None:
    calls: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        if "reasoning_effort" in body:
            return err(400, "Unrecognized request argument supplied: reasoning_effort")
        return chat(GOOD)

    await ask(text_llm(config, handler))
    assert "reasoning_effort" in calls[0] and "reasoning_effort" not in calls[1]


async def test_invalid_json_is_retried_with_feedback(config) -> None:
    replies = iter(["not json at all", GOOD])
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return chat(next(replies))

    res = await ask(text_llm(config, handler))
    assert res.usage.calls == 2 and res.usage.input_tokens == 22
    assert "rejected" in bodies[1]["messages"][-1]["content"]


async def test_validation_gives_up_after_retries(config) -> None:
    n = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        return chat('{"segments": "wrong"}')

    with pytest.raises(ValidationFailure):
        await ask(text_llm(config, handler))
    assert n == 3


async def test_truncated_output_is_treated_as_invalid(config) -> None:
    replies = iter([chat('{"segments": [', finish="length"), chat(GOOD)])
    res = await ask(text_llm(config, lambda _r: next(replies)))
    assert res.usage.calls == 2


# ── retry / errors ───────────────────────────────────────────────────────────
async def test_rate_limit_is_retried_with_backoff(config) -> None:
    replies = iter([err(429, "Rate limit reached"), err(500, "boom"), chat(GOOD)])
    res = await ask(text_llm(config, lambda _r: next(replies)))
    assert res.value.segments


async def test_connection_errors_exhaust_into_a_readable_error(config) -> None:
    config = config.model_copy(update={"retry_attempts": 2})

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderError, match="Could not reach"):
        await ask(text_llm(config, handler))


@pytest.mark.parametrize(
    ("status", "code", "msg", "expected_code"),
    [
        (401, "invalid_api_key", f"Incorrect API key provided: {KEY}.", "invalid_api_key"),
        (429, "insufficient_quota", "You exceeded your current quota", "insufficient_quota"),
        (404, "model_not_found", "The model `test-model` does not exist", "model_unavailable"),
        (403, None, "Project does not have access to model", "model_unavailable"),
    ],
)
async def test_fatal_errors_are_not_retried_and_never_leak_the_key(
    config, status, code, msg, expected_code
) -> None:
    n = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        return err(status, msg, code)

    with pytest.raises(FatalProviderError) as ei:
        await ask(text_llm(config, handler))
    assert n == 1 and ei.value.code == expected_code
    assert KEY not in ei.value.message and "sk-test" not in ei.value.message


def test_redact_strips_keys_everywhere() -> None:
    assert KEY not in redact(f"bad key {KEY} and sk-proj-abcdef123456 here", KEY)
    assert "sk-proj-abcdef123456" not in redact("x sk-proj-abcdef123456")


# ── transcription ────────────────────────────────────────────────────────────
VERBOSE = {
    "task": "transcribe",
    "language": "english",
    "duration": 4.0,
    "text": "hello world",
    "segments": [
        {
            "id": 0,
            "seek": 0,
            "start": 0.0,
            "end": 2.0,
            "text": " hello",
            "tokens": [1],
            "temperature": 0.0,
            "avg_logprob": -0.1,
            "compression_ratio": 1.0,
            "no_speech_prob": 0.0,
        }
    ],
}


def transcriber(
    config, handler: Handler, memo: CapabilityMemo | None = None
) -> oc.OpenAITranscriber:
    return oc.OpenAITranscriber(make_client(handler), config, memo or CapabilityMemo())


@pytest.fixture
def ogg(tmp_path: Path) -> Path:
    p = tmp_path / "chunk_000.ogg"
    p.write_bytes(b"OggS" + b"\0" * 64)
    return p


async def test_whisper_uses_verbose_json_segments_and_prompt(config, ogg) -> None:
    bodies: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(req.content)
        return httpx.Response(200, json=VERBOSE)

    t = transcriber(config, handler)
    res = await t.transcribe(
        ogg,
        model="whisper-1",
        duration_s=4.0,
        prompt="Frobnicator",
        keywords=["Frobnicator"],
        language="en",
        diarize=False,
    )
    raw = bodies[0]
    assert b"verbose_json" in raw and b"timestamp_granularities" in raw and b"Frobnicator" in raw
    assert b'name="keywords' not in raw  # whisper has no keywords parameter
    assert (
        res.precision == "segment"
        and res.segments[0].text == "hello"
        and res.usage.audio_seconds == 4.0
    )


async def test_gpt_transcribe_sends_keywords(config, ogg) -> None:
    bodies: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(req.content)
        return httpx.Response(200, json=VERBOSE)

    await transcriber(config, handler).transcribe(
        ogg,
        model="gpt-transcribe",
        duration_s=4.0,
        prompt=None,
        keywords=["Kubernetes"],
        language=None,
        diarize=False,
    )
    assert b"Kubernetes" in bodies[0] and b'name="keywords' in bodies[0]


async def test_rejected_verbose_json_switches_to_chunk_mode(config, ogg) -> None:
    memo = CapabilityMemo()

    def handler(req: httpx.Request) -> httpx.Response:
        if b"verbose_json" in req.content:
            return err(
                400, "response_format 'verbose_json' is not compatible with model gpt-transcribe"
            )
        return httpx.Response(200, json={"text": "plain text only"})

    t = transcriber(config, handler, memo)
    assert t.caps_for("gpt-transcribe").segment_timestamps is True
    with pytest.raises(CapabilityChanged):
        await t.transcribe(
            ogg,
            model="gpt-transcribe",
            duration_s=30.0,
            prompt=None,
            keywords=[],
            language=None,
            diarize=False,
        )
    assert t.caps_for("gpt-transcribe").segment_timestamps is False  # remembered
    res = await t.transcribe(
        ogg,
        model="gpt-transcribe",
        duration_s=30.0,
        prompt=None,
        keywords=[],
        language=None,
        diarize=False,
    )
    assert res.precision == "chunk" and (res.segments[0].start, res.segments[0].end) == (0.0, 30.0)


async def test_gpt4o_models_use_plain_json(config, ogg) -> None:
    bodies: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(req.content)
        return httpx.Response(200, json={"text": "just text"})

    t = transcriber(config, handler)
    assert t.caps_for("gpt-4o-transcribe").segment_timestamps is False
    res = await t.transcribe(
        ogg,
        model="gpt-4o-transcribe",
        duration_s=45.0,
        prompt=None,
        keywords=[],
        language=None,
        diarize=False,
    )
    assert b"verbose_json" not in bodies[0] and res.precision == "chunk"


async def test_json_response_languages_objects_become_string(config, ogg) -> None:
    """SDK ≥3.24 parses languages as TranscriptionLanguage(code=...); RawTranscription needs str."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"text": "hello", "languages": [{"code": "en"}, {"code": "es"}]}
        )

    res = await transcriber(config, handler).transcribe(
        ogg,
        model="gpt-4o-transcribe",
        duration_s=10.0,
        prompt=None,
        keywords=[],
        language=None,
        diarize=False,
    )
    assert res.language == "en" and res.precision == "chunk"


# ── vision ───────────────────────────────────────────────────────────────────
async def test_vision_request_carries_labelled_images_and_detail(config, tmp_path: Path) -> None:
    imgs = []
    for i in range(2):
        p = tmp_path / f"frame_{i:04d}.00.jpg"
        Image.new("RGB", (8, 8), (i * 50, 0, 0)).save(p, "JPEG")
        imgs.append(
            VisionFrame(name=p.name, t=float(i), path=p, label=f"Frame {i + 1}: file={p.name}")
        )
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return chat(
            json.dumps(
                {
                    "frames": [
                        {
                            "frame": imgs[0].name,
                            "scene_description": "d",
                            "on_screen_text": [],
                            "change_from_previous": "First frame",
                            "entities": [],
                            "scene_type": "slide",
                        }
                    ]
                }
            )
        )

    v = oc.OpenAIVision(make_client(handler), config, CapabilityMemo())
    ref = VisionFrame(name="ref.jpg", t=0, path=imgs[0].path, label="REFERENCE FRAME")
    res = await v.analyze(
        VisionBatchRequest(
            system="sys",
            preamble="pre",
            frames=imgs,
            context_frame=ref,
            postamble="post",
            detail="high",
            model="vm",
        )
    )
    parts = seen[0]["messages"][1]["content"]
    images = [p for p in parts if p["type"] == "image_url"]
    assert [i["image_url"]["detail"] for i in images] == [
        "low",
        "high",
        "high",
    ]  # reference frame is always cheap
    assert images[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert [p["text"] for p in parts if p["type"] == "text"][:2] == ["pre", "REFERENCE FRAME"]
    assert res.value.frames[0].frame == imgs[0].name
