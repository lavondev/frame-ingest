"""Provider profiles: which providers a run uses, and whether that run touches the network.

M1 ships only `fake` (deterministic, offline). `cloud` and `local` arrive with the egress consent
gate in M5, and `agent` with `prepare`/`assemble` in M4. Until then asking for them is a clear
"not available yet" error, never a silent fallback to a cloud call (AGENTS.md rule 7).
"""

from __future__ import annotations

from frame_ingest.engine import ProviderFactory
from frame_ingest.errors import FrameIngestError
from frame_ingest.providers.base import ProviderBundle
from frame_ingest.providers.fake import FakeText, FakeTranscriber, FakeVision

PROFILES = ("fake", "cloud", "local", "agent")
RUNNABLE = ("fake",)
_AVAILABLE_IN = {"cloud": "M5", "local": "M5", "agent": "M4"}


class ProfileUnavailable(FrameIngestError):
    code = "profile_unavailable"
    status = 501


def fake_bundle() -> ProviderBundle:
    return ProviderBundle(transcriber=FakeTranscriber(), vision=FakeVision(), text=FakeText())


def provider_factory(profile: str) -> ProviderFactory:
    if profile == "fake":
        return fake_bundle
    if profile == "agent":
        raise ProfileUnavailable(
            "Profile 'agent' has no `run`: use `prepare`, write your outputs, then `assemble`."
        )
    milestone = _AVAILABLE_IN.get(profile)
    if milestone is None:
        raise ProfileUnavailable(
            f"Unknown profile '{profile}'. Choose from: {', '.join(PROFILES)}."
        )
    raise ProfileUnavailable(
        f"Profile '{profile}' is not available yet (planned for {milestone}). "
        "Use --profile fake, which runs offline with deterministic providers."
    )


def egress_summary(profile: str) -> dict[str, object]:
    """What a run with this profile would send off the machine. Computed, never contacted."""
    if profile == "fake":
        return {"network": False, "destinations": [], "note": "Fake providers; nothing is sent."}
    if profile == "agent":
        return {
            "network": False,
            "destinations": [],
            "note": (
                "The CLI sends nothing. In agent mode the frames and transcript you read are "
                "sent to whatever model backs the host agent."
            ),
        }
    return {
        "network": True,
        "destinations": [],
        "note": f"Profile '{profile}' is not implemented yet; no egress summary available.",
    }
