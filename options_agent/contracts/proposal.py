from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Leg(BaseModel):
    right: Literal["call", "put"]
    side: Literal["buy", "sell"]
    strike: float
    expiration: date
    ratio: int = 1


class ExitPlan(BaseModel):
    # Structural invariants only — policy bounds live in Limits.exit_plan_bounds.
    profit_target_pct: float = Field(gt=0, le=1.0)
    # WP-0 amendment (WP-5.1): renamed from stop_loss_mult and redefined as a
    # fraction of est_max_loss so the formula is uniform across credit and debit
    # strategies. Semantically in (0, 1]; upper bound enforced by ExitPlanBounds.
    stop_loss_max_loss_fraction: float = Field(gt=0)
    time_stop_dte: int = Field(ge=0)


class TradeProposal(BaseModel):
    action: Literal["OPEN", "CLOSE", "ROLL", "NO_ACTION"]
    underlying: str
    strategy: str
    legs: list[Leg]
    thesis: str
    iv_rationale: str
    catalyst_check: str
    conviction: float
    est_max_loss: float
    est_max_profit: float
    breakevens: list[float]
    net_delta: float
    net_theta: float
    net_vega: float
    exit_plan: ExitPlan
    informed_by: list[str]

    @field_validator("conviction")
    @classmethod
    def conviction_in_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"conviction must be between 0.0 and 1.0, got {v}")
        return v
