from datetime import date
from typing import Literal

from pydantic import BaseModel, field_validator


class Leg(BaseModel):
    right: Literal["call", "put"]
    side: Literal["buy", "sell"]
    strike: float
    expiration: date
    ratio: int = 1


class ExitPlan(BaseModel):
    profit_target_pct: float
    stop_loss_mult: float
    time_stop_dte: int


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
