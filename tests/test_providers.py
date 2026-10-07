"""M5: profiles, the egress plan and consent gate, --offline, cost caps, local speech.

Offline, no keys. Cloud providers are replaced with fakes at the one place the CLI builds them,
so what is under test is everything around the network: what is shown, what is refused, what is
stopped. `faster_whisper` is a stand-in module.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import types
from pathlib import Path
from typing import Any

import openai
import pytest

from frame_ingest.budget import Budget, BudgetExceeded, BudgetUnknown, check_estimate
from frame_ingest.cli import main
from frame_ingest.config import AppConfig, Pricing, load_config
from frame_ingest.egress import EgressDenied, EgressPlan, build_plan, enforce
from frame_ingest.engine import Engine
from frame_ingest.errors import FatalProviderError, ProviderError
from frame_ingest.guard.netblock import OfflineViolation, block_network, is_loopback
from frame_ingest.models import Estimate, StageEstimate, StageName
from frame_ingest.profiles import fake_bundle, resolve_profile
from frame_ingest.providers.fake import FakeText, FakeVision
from frame_ingest.providers.faster_whisper import FasterWhisperTranscriber

KEY = "sk-test-PROVIDERS-1234567890"
PRICING = (
    "transcription_per_minute:\n  whisper-1: 0.006\n"
    "text_models:\n  gpt-6-luna:\n    input_per_mtok: 1.0\n    output_per_mtok: 4.0\n"
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fi-home"
    for name in list(os.environ):
        if name.startswith(("FRAME_INGEST_", "OPENAI_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FRAME_INGEST_HOME", str(home))
    home.mkdir(parents=True)
    return home


@pytest.fixture
def cloud_env(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    return home


class Boom:
    """Stands in for the cloud client: reaching it means data was about to be sent."""

    def __call__(self, *_a: Any, **_k: Any) -> Any:
        raise AssertionError("a cloud provider was constructed")


def fake_cloud(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    built: list[int] = []

    def build(*_a: Any, **_k: Any) -> Any:
        built.append(1)
        return fake_bundle()

    monkeypatch.setattr("frame_ingest.providers.openai_client.build_openai_providers", build)
    return built


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], str]:
    code = main([*argv, "--json"])
    out = capsys.readouterr()
    return code, json.loads(out.out) if out.out.strip() else {}, out.err


def estimate_for(config: AppConfig, **stages: StageEstimate) -> Estimate:
    base = {s: StageEstimate() for s in StageName}
    base.update({StageName(k): v for k, v in stages.items()})
    return Estimate(
        duration_s=120,
        has_audio=True,
        scene_cuts=3,
        frames=12,
        stages=base,
        total_api_calls=5,
        total_input_tokens=10_000,
        total_output_tokens=2_000,
    )


# ── loopback detection ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.5.5.5", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("ollama.localhost", True),
        ("0.0.0.0", False),  # noqa: S104
        ("192.168.1.10", False),
        ("10.0.0.5", False),
        ("api.openai.com", False),
        ("localhost.evil.example", False),
        ("127.0.0.1.evil.example", False),
        ("", False),
        (None, False),
    ],
)
def test_is_loopback(host: str | None, expected: bool) -> None:
    assert is_loopback(host) is expected


# ── the egress plan ─────────────────────────────────────────────────────────────
def plan_for(profile: str, config: AppConfig, **stage_kw: StageEstimate) -> EgressPlan:
    from frame_ingest.config import resolve_settings

    est = estimate_for(config, **stage_kw)
    return build_plan(profile, config, est, resolve_settings(config))


STAGES = {
    "transcribe": StageEstimate(api_calls=1, audio_minutes=2.0),
    "vision": StageEstimate(api_calls=2, input_tokens=9000, output_tokens=900),
    "correct": StageEstimate(api_calls=1, input_tokens=2000, output_tokens=2000),
    "synthesize": StageEstimate(api_calls=3, input_tokens=4000, output_tokens=1500),
}


def test_cloud_plan_names_every_destination_amount_and_model(config: AppConfig) -> None:
    plan = plan_for("cloud", config, **STAGES)
    roles = {i.role: i for i in plan.items}
    assert set(roles) == {"transcribe", "vision", "text"}
    assert plan.leaves_machine and all(not i.loopback for i in plan.items)
    assert roles["transcribe"].audio_minutes == 2.0 and roles["transcribe"].approx_mb
    assert "api.openai.com" in roles["transcribe"].destination
    assert roles["vision"].sends.startswith("12 frame(s)")
    assert roles["text"].input_tokens == 6000
    text = plan.render()
    assert "leaves this machine" in text and config.models.vision in text


def test_plan_shows_the_configured_host_never_credentials_or_query(config: AppConfig) -> None:
    cfg = config.model_copy(
        update={
            "base_urls": config.base_urls.model_copy(
                update={"vision": "https://user:hunter2@api.groq.example:8443/openai/v1?key=SECRET"}
            )
        }
    )
    text = plan_for("cloud", cfg, **STAGES).render()
    assert "api.groq.example:8443/openai/v1" in text
    assert "hunter2" not in text and "SECRET" not in text and "user@" not in text


def test_a_loopback_endpoint_is_not_egress(config: AppConfig) -> None:
    cfg = config.model_copy(
        update={
            "base_urls": config.base_urls.model_copy(
                update={
                    "transcribe": "http://127.0.0.1:8000/v1",
                    "vision": "http://localhost:8000/v1",
                    "text": "http://[::1]:8000/v1",
                }
            )
        }
    )
    assert not plan_for("cloud", cfg, **STAGES).leaves_machine


def test_a_lan_endpoint_still_counts_as_leaving_the_machine(config: AppConfig) -> None:
    cfg = config.model_copy(
        update={
            "base_urls": config.base_urls.model_copy(update={"vision": "http://192.168.1.5/v1"})
        }
    )
    assert plan_for("cloud", cfg, **STAGES).leaves_machine


def test_local_plan_stays_on_this_machine(home: Path) -> None:
    from frame_ingest.profiles import profile_config

    cfg = profile_config("local", load_config(home, env={}))
    plan = plan_for("local", cfg, **STAGES)
    assert not plan.leaves_machine and len(plan.items) == 3
    assert "this machine" in plan.items[0].destination
    assert any("Hugging Face" in n for n in plan.notes)
    assert plan.items[1].destination.startswith("http://127.0.0.1:11434")


def test_fake_and_agent_plans_send_nothing(config: AppConfig) -> None:
    assert plan_for("fake", config, **STAGES).items == []
    agent = plan_for("agent", config, **STAGES)
    assert agent.items == [] and "host agent" in agent.render()


# ── the consent gate ────────────────────────────────────────────────────────────
@pytest.fixture
def cloud_plan(config: AppConfig) -> EgressPlan:
    return plan_for("cloud", config, **STAGES)


def gate(plan: EgressPlan, **kw: Any) -> None:
    args: dict[str, Any] = {
        "mode": "ask",
        "allow_flag": False,
        "offline": False,
        "interactive": False,
        "ask": lambda _p: False,
    }
    args.update(kw)
    enforce(plan, **args)


def test_non_interactive_default_denies_and_says_how_to_consent(cloud_plan: EgressPlan) -> None:
    with pytest.raises(EgressDenied) as exc:
        gate(cloud_plan)
    assert "--allow-egress" in exc.value.message and "leaves this machine" in exc.value.message


def test_consent_paths(cloud_plan: EgressPlan) -> None:
    gate(cloud_plan, allow_flag=True)
    gate(cloud_plan, mode="allow")
    prompts: list[str] = []

    def yes(prompt: str) -> bool:
        prompts.append(prompt)
        return True

    gate(cloud_plan, interactive=True, ask=yes)
    assert "Send this data?" in prompts[0] and "api.openai.com" in prompts[0]
    with pytest.raises(EgressDenied, match="Declined"):
        gate(cloud_plan, interactive=True, ask=lambda _p: False)


def test_deny_mode_ignores_a_terminal(cloud_plan: EgressPlan) -> None:
    with pytest.raises(EgressDenied):
        gate(cloud_plan, mode="deny", interactive=True, ask=lambda _p: True)


def test_offline_beats_every_consent(cloud_plan: EgressPlan) -> None:
    with pytest.raises(EgressDenied, match="--offline"):
        gate(cloud_plan, offline=True, allow_flag=True, mode="allow")


def test_a_plan_that_stays_local_needs_no_consent(config: AppConfig) -> None:
    gate(plan_for("fake", config, **STAGES), mode="deny", offline=True)


# ── cli: nothing is sent without consent ───────────────────────────────────────
def test_cloud_run_is_refused_without_consent_and_no_provider_is_built(
    cloud_env: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("frame_ingest.providers.openai_client.build_openai_providers", Boom())
    code, out, err = run_cli(capsys, "run", str(sample_video), "--profile", "cloud")
    assert code == 4 and out["error"]["code"] == "egress_denied"
    assert "leaves this machine" in err  # the plan was shown before refusing
    assert KEY not in out["error"]["message"] + err


def test_cloud_run_with_consent_shows_the_plan_then_runs(
    cloud_env: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = fake_cloud(monkeypatch)
    code, out, err = run_cli(
        capsys, "run", str(sample_video), "--profile", "cloud", "--allow-egress"
    )
    assert code == 0 and built == [1]
    assert err.index("Egress plan") < err.index("[probe] running")  # shown before any work
    items = {i["role"]: i for i in out["egress"]["items"]}
    assert set(items) == {"transcribe", "vision", "text"} and out["egress"]["network"] is True
    assert KEY not in json.dumps(out) + err


def test_config_can_allow_egress_persistently(
    cloud_env: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cloud(monkeypatch)
    monkeypatch.setenv("FRAME_INGEST_EGRESS", "allow")
    assert run_cli(capsys, "run", str(sample_video), "--profile", "cloud")[0] == 0


def test_a_loopback_cloud_endpoint_needs_no_consent(
    cloud_env: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cloud(monkeypatch)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1")
    code, out, _ = run_cli(capsys, "run", str(sample_video), "--profile", "cloud")
    assert code == 0 and out["egress"]["network"] is False


def test_offline_refuses_a_cloud_plan_even_with_the_flag(
    cloud_env: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("frame_ingest.providers.openai_client.build_openai_providers", Boom())
    code, out, _ = run_cli(
        capsys, "run", str(sample_video), "--profile", "cloud", "--allow-egress", "--offline"
    )
    assert code == 4 and "--offline" in out["error"]["message"]


def test_estimate_shows_the_plan_without_contacting_anything(
    cloud_env: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("frame_ingest.providers.openai_client.build_openai_providers", Boom())
    with block_network():  # any non-loopback connection would raise
        code, out, _ = run_cli(capsys, "estimate", str(sample_video), "--profile", "cloud")
    assert code == 0 and out["egress"]["network"] is True
    roles = {i["role"]: i for i in out["egress"]["items"]}
    assert roles["transcribe"]["audio_minutes"] == pytest.approx(0.4, abs=0.01)  # the 24 s clip
    assert roles["vision"]["sends"].startswith(f"{out['estimate']['frames']} frame(s)")
    assert out["estimate"]["cost_usd"] is None  # no pricing.yaml: honest, not a guess


def test_estimate_for_local_is_free_and_local(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run_cli(capsys, "estimate", str(sample_video), "--profile", "local")
    assert code == 0 and out["egress"]["network"] is False
    assert out["estimate"]["cost_usd"] == 0.0


def test_a_job_keeps_the_profile_it_was_created_with(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = run_cli(capsys, "estimate", str(sample_video), "--profile", "fake")[1]["job_id"]
    code, out, _ = run_cli(capsys, "run", "--job", job, "--profile", "local")
    assert code == 2 and "created with profile 'fake'" in out["error"]["message"]


# ── cost caps ───────────────────────────────────────────────────────────────────
def test_max_cost_needs_prices_and_a_big_enough_cap(
    cloud_env: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("frame_ingest.providers.openai_client.build_openai_providers", Boom())
    args = ("run", str(sample_video), "--profile", "cloud", "--allow-egress", "--max-cost")
    code, out, _ = run_cli(capsys, *args, "5")
    assert code == 4 and out["error"]["code"] == "budget_unknown"  # no pricing.yaml

    (cloud_env / "pricing.yaml").write_text(PRICING, encoding="utf-8")
    code, out, _ = run_cli(capsys, *args, "0.0000001")
    assert code == 4 and out["error"]["code"] == "budget_exceeded"
    assert "over --max-cost" in out["error"]["message"]


def test_max_cost_allows_a_cheap_run_and_reports_spend(
    cloud_env: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cloud(monkeypatch)
    (cloud_env / "pricing.yaml").write_text(PRICING, encoding="utf-8")
    code, out, _ = run_cli(
        capsys, "run", str(sample_video), "--profile", "cloud", "--allow-egress", "--max-cost", "5"
    )
    assert code == 0 and out["spent_usd"] is not None and 0 < out["spent_usd"] < 5


def test_budget_charges_audio_and_tokens_and_stops_on_overrun() -> None:
    from frame_ingest.providers.base import UsageDelta

    pricing = Pricing.model_validate(
        {
            "transcription_per_minute": {"w": 0.60},
            "text_models": {"m": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}},
        }
    )
    b = Budget(1.0, pricing)
    b.charge(StageName.TRANSCRIBE, "w", UsageDelta(audio_seconds=60))  # $0.60
    b.charge(StageName.VISION, "m", UsageDelta(input_tokens=100_000, output_tokens=50_000))  # $0.20
    assert b.spent == pytest.approx(0.80)
    with pytest.raises(BudgetExceeded, match="cached"):
        b.charge(StageName.SYNTHESIZE, "m", UsageDelta(input_tokens=1_000_000))
    Budget(0.0, pricing, free=True).charge(StageName.VISION, "m", UsageDelta(input_tokens=10**9))


def test_a_runaway_job_stops_at_the_cap_and_resumes_when_raised(
    config: AppConfig, sample_video: Path
) -> None:
    pricing = Pricing.model_validate({"transcription_per_minute": {config.models.transcribe: 6000}})
    eng = Engine(config, fake_bundle, pricing=pricing)

    async def go() -> None:
        job = await eng.create(sample_video)
        stopped = await eng.run(job.id, budget=Budget(0.01, pricing))
        assert stopped.status.value == "failed" and stopped.error
        assert stopped.error.code == "budget_exceeded"
        done = await eng.run(job.id)  # no cap this time: finished stages are reused
        assert done.status.value == "completed"

    import asyncio

    asyncio.run(go())


def test_check_estimate_free_profiles_skip_the_cap(config: AppConfig) -> None:
    est = estimate_for(config)
    check_estimate(est, 0.01, free=True)
    with pytest.raises(BudgetUnknown):
        check_estimate(est.model_copy(update={"cost_note": "no prices"}), 1.0)
    with pytest.raises(BudgetExceeded):
        check_estimate(est.model_copy(update={"cost_usd": 3.0}), 1.0)


# ── --offline ───────────────────────────────────────────────────────────────────
def test_offline_blocks_outside_connections_and_dns_but_not_loopback() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    original = socket.socket.connect
    with block_network():
        with pytest.raises(OfflineViolation):
            socket.create_connection(("93.184.216.34", 80), timeout=1)
        with pytest.raises(OfflineViolation):
            socket.getaddrinfo("example.com", 80)
        with pytest.raises(OfflineViolation):
            socket.socket().connect_ex(("192.168.1.1", 80))
        socket.create_connection(("127.0.0.1", port), timeout=2).close()  # loopback still works
        socket.getaddrinfo("localhost", port)
    assert socket.socket.connect is original  # restored
    server.close()


def test_offline_local_run_never_leaves_loopback(
    home: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_whisper(monkeypatch)
    patch_local_models(monkeypatch)
    attempts: list[str] = []
    real = socket.socket.connect

    def spy(self: socket.socket, address: Any) -> None:
        attempts.append(str(address))
        real(self, address)

    monkeypatch.setattr(socket.socket, "connect", spy)  # installed *under* the offline guard
    code, out, err = run_cli(capsys, "run", str(sample_video), "--profile", "local", "--offline")
    assert code == 0, (out, err)
    assert all("127.0.0.1" in a or "::1" in a for a in attempts)
    assert out["egress"]["network"] is False
    md = Path(out["outputs"]["md"]).read_text(encoding="utf-8")
    assert "transcribe: small" in md and "qwen3:8b" in md  # the local models, not the cloud ones


# ── local profile ───────────────────────────────────────────────────────────────
async def make_tone(path: Path) -> None:
    """A real one-second Ogg file: local transcription decodes audio with our own ffmpeg."""
    from frame_ingest.ffmpeg import run_ffmpeg

    await run_ffmpeg(
        ["-y", "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "libopus", str(path)], timeout=60
    )


class FakeWhisperModel:
    instances: list[FakeWhisperModel] = []

    def __init__(self, name: str, **kw: Any) -> None:
        self.name, self.kw = name, kw
        FakeWhisperModel.instances.append(self)

    def transcribe(self, path: Any, **kw: Any) -> tuple[Any, Any]:
        self.last = (path, kw)
        self.samples = path
        segs = [
            types.SimpleNamespace(start=0.0, end=3.5, text=" Welcome to the Widjet Frobnicator. "),
            types.SimpleNamespace(start=3.5, end=7.0, text="  "),  # blank: dropped
            types.SimpleNamespace(start=7.0, end=10.0, text="Open the dashboard."),
        ]
        return iter(segs), types.SimpleNamespace(language="en")


def install_fake_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeWhisperModel.instances.clear()
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)


def patch_local_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the OpenAI-compatible vision/text clients for fakes; keep the real wiring."""
    monkeypatch.setattr(
        "frame_ingest.providers.openai_client.OpenAIVision", lambda _c, _cfg, _m: FakeVision()
    )
    monkeypatch.setattr(
        "frame_ingest.providers.openai_client.OpenAIText", lambda _c, _cfg, _m: FakeText()
    )


async def test_faster_whisper_transcriber_maps_segments_and_honours_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_whisper(monkeypatch)
    audio = tmp_path / "chunk.ogg"
    await make_tone(audio)
    tr = FasterWhisperTranscriber("small", download_root=tmp_path / "models", offline=True)
    res = await tr.transcribe(
        audio,
        model="small",
        duration_s=10.0,
        prompt="Widget",
        keywords=[],
        language="en",
        diarize=False,
    )
    assert [s.text for s in res.segments] == [
        "Welcome to the Widjet Frobnicator.",
        "Open the dashboard.",
    ]
    assert res.precision == "segment" and res.language == "en" and res.usage.audio_seconds == 10.0
    model = FakeWhisperModel.instances[0]
    samples = model.samples  # decoded by our ffmpeg, not by PyAV in this process
    assert str(samples.dtype) == "float32" and samples.ndim == 1
    assert 15000 < len(samples) < 17500  # ~1 s at 16 kHz
    assert model.kw["local_files_only"] is True and model.kw["download_root"].endswith("models")
    assert model.last[1]["initial_prompt"] == "Widget" and model.last[1]["language"] == "en"
    assert tr.caps_for("small").segment_timestamps and not tr.caps_for("small").diarization
    await tr.transcribe(
        audio, model="small", duration_s=1, prompt=None, keywords=[], language=None, diarize=False
    )
    assert len(FakeWhisperModel.instances) == 1  # loaded once


async def test_faster_whisper_errors_are_actionable_and_key_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "chunk.ogg"
    await make_tone(audio)
    kw: dict[str, Any] = {
        "model": "small",
        "duration_s": 1.0,
        "prompt": None,
        "keywords": [],
        "language": None,
        "diarize": False,
    }
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import fails
    tr = FasterWhisperTranscriber("small", download_root=tmp_path, offline=False)
    with pytest.raises(FatalProviderError) as exc:
        await tr.transcribe(audio, **kw)
    assert exc.value.code == "missing_dependency" and ".[local]" in exc.value.message

    class Broken:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            raise OSError("no such file /secret/path")

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = Broken  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    tr = FasterWhisperTranscriber("small", download_root=tmp_path, offline=True)
    with pytest.raises(FatalProviderError) as exc2:
        await tr.transcribe(audio, **kw)
    assert "without --offline" in exc2.value.message and "/secret/path" not in exc2.value.message

    link = tmp_path / "link.ogg"
    link.symlink_to(audio)
    with pytest.raises(ProviderError, match="symlink"):
        await tr.transcribe(link, **kw)


def test_local_profile_refuses_a_remote_server(
    home: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRAME_INGEST_LOCAL_BASE_URL", "http://203.0.113.9:11434/v1")
    code, out, _ = run_cli(capsys, "run", str(sample_video), "--profile", "local")
    assert code == 4 and out["error"]["code"] == "invalid_config"
    assert "this machine" in out["error"]["message"]


def test_local_profile_wires_loopback_models_and_no_cloud_key(home: Path) -> None:
    cfg = load_config(home, env={"OPENAI_API_KEY": KEY})
    resolved = resolve_profile("local", cfg, offline=True)
    assert resolved.free
    assert resolved.config.base_urls.vision == "http://127.0.0.1:11434/v1"
    assert resolved.config.base_urls.transcribe is None
    assert resolved.config.models.transcribe == "small"
    assert resolved.config.key_for("vision") == "local"  # the user's real key is not used
    assert resolved.config.reasoning_effort is None


# ── doctor ──────────────────────────────────────────────────────────────────────
class _Models:
    def __init__(self, ids: list[str] | Exception) -> None:
        self.ids = ids

    async def list(self) -> Any:
        if isinstance(self.ids, Exception):
            raise self.ids
        return types.SimpleNamespace(data=[types.SimpleNamespace(id=i) for i in self.ids])


def fake_openai(monkeypatch: pytest.MonkeyPatch, ids: list[str] | Exception) -> None:
    monkeypatch.setattr(
        openai,
        "AsyncOpenAI",
        lambda **_k: types.SimpleNamespace(models=_Models(ids)),
    )


def test_doctor_local_is_ready_when_everything_is_there(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_whisper(monkeypatch)
    fake_openai(monkeypatch, ["qwen2.5vl:7b", "qwen3:8b:latest"])
    monkeypatch.setattr("frame_ingest.providers.faster_whisper.is_available", lambda: True)
    code, out, _ = run_cli(capsys, "doctor", "--profile", "local")
    assert code == 0 and out["ok"] is True
    names = {c["name"] for c in out["report"]["checks"]}
    assert {"faster-whisper", "local server", "local vision model", "local text model"} <= names


def test_doctor_local_gives_the_exact_fix_for_each_problem(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_openai(monkeypatch, ["qwen3:8b"])  # vision model missing
    monkeypatch.setattr("frame_ingest.providers.faster_whisper.is_available", lambda: False)
    code, out, _ = run_cli(capsys, "doctor", "--profile", "local")
    msgs = " ".join(c["message"] for c in out["report"]["checks"] if not c["ok"])
    assert code == 1 and "ollama pull qwen2.5vl:7b" in msgs and ".[local]" in msgs

    fake_openai(
        monkeypatch,
        openai.APIConnectionError(request=__import__("httpx").Request("GET", "http://x")),
    )
    code, out, _ = run_cli(capsys, "doctor", "--profile", "local")
    assert code == 1 and "ollama serve" in json.dumps(out)


def test_doctor_default_still_makes_no_network_call(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openai, "AsyncOpenAI", Boom())
    with block_network():
        assert run_cli(capsys, "doctor")[0] == 0


def test_the_plan_never_prints_the_key(
    cloud_env: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", f"https://x:{KEY}@api.example.org/v1")
    code, out, err = run_cli(capsys, "estimate", str(sample_video), "--profile", "cloud")
    assert code == 0 and KEY not in json.dumps(out) + err
    assert "api.example.org" in json.dumps(out)
