"""`doctor` (faircopy's health check) with the OpenAI client faked: no network."""

from __future__ import annotations

from pathlib import Path

import httpx
import openai
import pytest

from frame_ingest import doctor as doctor_mod
from frame_ingest.capabilities import CapabilityMemo
from frame_ingest.config import AppConfig, load_config
from frame_ingest.doctor import display_url, report_dict, run_doctor


class FakeModels:
    def __init__(self, bad: dict[str, Exception]) -> None:
        self.bad = bad

    async def retrieve(self, model: str) -> None:
        if model in self.bad:
            raise self.bad[model]


class FakeClient:
    def __init__(self, bad: dict[str, Exception]) -> None:
        self.models = FakeModels(bad)


async def test_doctor_reports_model_problems_in_plain_language(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    req = httpx.Request("GET", "http://x")
    notfound = openai.NotFoundError("nope", response=httpx.Response(404, request=req), body=None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        doctor_mod, "make_client", lambda _c, _r: FakeClient({config.models.vision: notfound})
    )
    report = await run_doctor(config, CapabilityMemo())
    assert report.ok is False and report.models["vision"].available is False
    assert f"Model '{config.models.vision}' is not available" in (
        report.models["vision"].message or ""
    )
    assert "FRAME_INGEST_MODEL_VISION" in (report.models["vision"].message or "")
    assert report.key_present is True
    assert report.ffmpeg and report.ffmpeg["version"].startswith("ffmpeg version")
    assert any("deprecated" in w for w in report.warnings)  # whisper-1 default is flagged

    monkeypatch.setattr(doctor_mod, "make_client", lambda _c, _r: FakeClient({}))
    assert (await run_doctor(config, CapabilityMemo())).ok is True


async def test_doctor_without_a_key_says_what_to_do(tmp_path: Path) -> None:
    nokey = load_config(tmp_path, env={})
    report = await run_doctor(nokey, CapabilityMemo())
    assert report.ok is False and report.key_present is False
    assert any("OPENAI_API_KEY is missing" in c.message for c in report.checks)
    assert all(m.available is None for m in report.models.values())


async def test_doctor_report_never_contains_the_key(config: AppConfig, monkeypatch) -> None:
    monkeypatch.setattr(doctor_mod, "make_client", lambda _c, _r: FakeClient({}))
    dumped = str(report_dict(await run_doctor(config, CapabilityMemo())))
    assert "sk-test" not in dumped


def test_display_url_strips_credentials_and_query() -> None:
    assert display_url("https://user:pw@host.example:8443/v1?key=sk-abc") == (
        "https://host.example:8443/v1"
    )
    assert display_url(None) is None
