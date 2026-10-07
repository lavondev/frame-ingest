"""Egress plan and consent gate (PLAN T8).

Before anything leaves the machine the CLI computes what would go where from the estimate (a
local computation: no provider is contacted) and shows it. Consent comes from, in order: `--offline`
(always denies), `--allow-egress` or `egress: allow`, an interactive yes on a terminal, otherwise
the run is refused. Loopback destinations are not egress.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from frame_ingest.config import AppConfig
from frame_ingest.errors import FrameIngestError
from frame_ingest.guard.netblock import is_loopback
from frame_ingest.models import Estimate, ResolvedSettings, StageName

OPENAI_DEFAULT = "https://api.openai.com/v1 (default)"


class EgressDenied(FrameIngestError):
    code = "egress_denied"
    status = 403


class EgressItem(BaseModel):
    role: str  # transcribe | vision | text
    sends: str  # human text: what is sent
    destination: str  # where; "this machine" for in-process work
    loopback: bool
    models: list[str]
    input_tokens: int = 0
    audio_minutes: float = 0.0
    approx_mb: float | None = None


class EgressPlan(BaseModel):
    profile: str
    items: list[EgressItem] = Field(default_factory=list)
    cost_usd: float | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def leaves_machine(self) -> bool:
        return any(not i.loopback for i in self.items)

    def render(self) -> str:
        if not self.items:
            return "\n".join(
                [f"Egress ({self.profile}): the CLI sends nothing."]
                + [f"  note: {n}" for n in self.notes]
            )
        lines = [f"Egress plan for profile '{self.profile}':"]
        for i in self.items:
            where = i.destination if i.loopback else f"{i.destination}  <-- leaves this machine"
            size = f", ~{i.approx_mb:.1f} MB" if i.approx_mb else ""
            lines.append(f"  - {i.role}: {i.sends}{size} -> {where} [{', '.join(i.models)}]")
        if self.cost_usd is not None:
            lines.append(f"  estimated cost: ~${self.cost_usd:.2f}")
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def destination(base_url: str | None) -> tuple[str, bool]:
    """(display text, is_loopback) for a base URL; credentials and query strings never shown."""
    if not base_url:
        return OPENAI_DEFAULT, False
    parts = urlsplit(base_url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}{parts.path}", is_loopback(parts.hostname)


def build_plan(
    profile: str, config: AppConfig, est: Estimate, settings: ResolvedSettings
) -> EgressPlan:
    if profile == "fake":
        return EgressPlan(profile=profile, cost_usd=0.0, notes=["Fake providers; nothing is sent."])
    if profile == "agent":
        return EgressPlan(
            profile=profile,
            notes=[
                "The CLI sends nothing. In agent mode the frames and transcript you read are "
                "sent to whatever model backs the host agent."
            ],
        )
    plan = EgressPlan(profile=profile, cost_usd=est.cost_usd)
    local = profile == "local"
    st = est.stages

    if est.has_audio and st[StageName.TRANSCRIBE].audio_minutes:
        minutes = st[StageName.TRANSCRIBE].audio_minutes
        model = config.models.diarize if settings.diarize else settings.transcribe_model
        if local:
            plan.items.append(
                EgressItem(
                    role="transcribe",
                    sends=f"{minutes:g} min of audio",
                    destination="this machine (faster-whisper, in process)",
                    loopback=True,
                    models=[model],
                    audio_minutes=minutes,
                )
            )
        else:
            dest, loop = destination(config.base_url_for("transcribe"))
            kbps = config.audio_bitrate_kbps
            plan.items.append(
                EgressItem(
                    role="transcribe",
                    sends=f"{minutes:g} min of audio",
                    destination=dest,
                    loopback=loop,
                    models=[model],
                    audio_minutes=minutes,
                    approx_mb=round(minutes * 60 * kbps / 8 / 1024, 1),
                )
            )
    if est.frames:
        dest, loop = destination(config.base_url_for("vision"))
        vis = st[StageName.VISION]
        plan.items.append(
            EgressItem(
                role="vision",
                sends=f"{est.frames} frame(s) (max {config.max_image_px}px, detail "
                f"{settings.image_detail}) and transcript excerpts",
                destination=dest,
                loopback=loop,
                models=[settings.vision_model],
                input_tokens=vis.input_tokens,
            )
        )
    text_stages = [st[StageName.CORRECT], st[StageName.SYNTHESIZE]]
    if any(s.api_calls for s in text_stages):
        dest, loop = destination(config.base_url_for("text"))
        plan.items.append(
            EgressItem(
                role="text",
                sends="the transcript, frame descriptions and chapter summaries",
                destination=dest,
                loopback=loop,
                models=sorted({settings.correct_model, settings.synthesize_model}),
                input_tokens=sum(s.input_tokens for s in text_stages),
            )
        )
    if local:
        plan.notes.append(
            "Speech model weights download from Hugging Face on first use unless --offline."
        )
    return plan


def enforce(
    plan: EgressPlan,
    *,
    mode: str,
    allow_flag: bool,
    offline: bool,
    interactive: bool,
    ask: Callable[[str], bool],
) -> None:
    """Raise EgressDenied unless this plan may proceed."""
    if not plan.leaves_machine:
        return
    if offline:
        raise EgressDenied(
            "--offline forbids network use, but this plan leaves the machine:\n" + plan.render()
        )
    if allow_flag or mode == "allow":
        return
    if mode == "ask" and interactive and ask(plan.render() + "\nSend this data? [y/N] "):
        return
    how = (
        "Declined."
        if mode == "ask" and interactive
        else "Re-run with --allow-egress after the user agrees, or set egress: allow."
    )
    raise EgressDenied(plan.render() + f"\nNot sent. {how}")
