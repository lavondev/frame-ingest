"""Cost cap (PLAN T10): a hard stop enforced in code, not by a prompt.

Before a run the estimate must fit under the cap (and be priceable); during a run every
provider call is charged as it completes and the job stops with `budget_exceeded` the moment the
total passes the cap. The overshoot is at most one call. Finished stages stay cached, so raising
the cap and re-running resumes where it stopped.
"""

from __future__ import annotations

from frame_ingest.config import Pricing
from frame_ingest.errors import FatalProviderError
from frame_ingest.models import Estimate, StageName
from frame_ingest.providers.base import UsageDelta


class BudgetExceeded(FatalProviderError):
    code = "budget_exceeded"
    status = 402


class BudgetUnknown(FatalProviderError):
    code = "budget_unknown"
    status = 400


def check_estimate(est: Estimate, max_cost: float, *, free: bool = False) -> None:
    """Refuse up front when the projected cost is over the cap or cannot be priced."""
    if free:
        return
    if est.cost_usd is None:
        raise BudgetUnknown(
            f"--max-cost needs a price for every model in use, but: {est.cost_note} "
            "Add the prices to pricing.yaml or drop --max-cost."
        )
    if est.cost_usd > max_cost:
        raise BudgetExceeded(
            f"The estimated cost ${est.cost_usd:.2f} is over --max-cost ${max_cost:.2f}. "
            "Lower --frame-cap, use a cheaper model, or raise the cap."
        )


class Budget:
    def __init__(self, max_usd: float, pricing: Pricing, *, free: bool = False) -> None:
        self.max_usd = max_usd
        self.pricing = pricing
        self.free = free
        self.spent = 0.0

    def charge(self, stage: StageName, model: str, delta: UsageDelta) -> None:
        if self.free:
            return
        if stage == StageName.TRANSCRIBE:
            price = self.pricing.transcription_per_minute.get(model)
            cost = (price or 0.0) * delta.audio_seconds / 60
        else:
            p = self.pricing.text_models.get(model, {})
            cost = (delta.input_tokens / 1e6) * (p.get("input_per_mtok") or 0.0) + (
                delta.output_tokens / 1e6
            ) * (p.get("output_per_mtok") or 0.0)
        self.spent += cost
        if self.spent > self.max_usd:
            raise BudgetExceeded(
                f"Stopped: spent ${self.spent:.2f}, over --max-cost ${self.max_usd:.2f}. "
                "Finished stages are cached; raise the cap and run again to resume."
            )
