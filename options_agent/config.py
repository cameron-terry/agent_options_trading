import tomllib
from datetime import time
from pathlib import Path

import exchange_calendars as xcals
from pydantic import BaseModel, Field, field_validator, model_validator

from options_agent.risk.limits import Limits


class Config(BaseModel):
    """Operational configuration for one agent run.

    Load from a TOML file with Config.from_toml(path). The [limits] section
    maps directly to the Limits model. entry_times must be "HH:MM" strings
    in TOML (e.g. ["10:30", "13:00", "15:00"]).

    Secrets (ALPACA_API_KEY, ALPACA_SECRET_KEY) must be supplied via
    environment variables — never commit them to config.toml.

    Kill-switch state is stored as a DB row (not in this config) so it can
    be toggled live without a process restart. db_url is the connection
    string used by WP-2/WP-7/WP-8 to read and write that row.
    """

    # Universe
    universe_file: Path = Field(default=Path("universe.txt"))

    # Entry cadence
    entry_times: list[time] = Field(default=[time(10, 30), time(13, 0), time(15, 0)])
    timezone: str = Field(default="America/New_York")
    # Open/close blackout windows guard against wide spreads at the auction;
    # they are independently tunable because the risk profile differs.
    # le=120 is a sanity cap — a blackout longer than 2h is almost certainly
    # a misconfiguration, not intentional policy.
    session_open_blackout_minutes: int = Field(default=30, ge=0, le=120)
    session_close_blackout_minutes: int = Field(default=30, ge=0, le=120)
    # exchange_calendars calendar name; XNYS = NYSE (US equities).
    # exchange_calendars calendar data has a finite forward horizon — refresh
    # the pinned package version periodically so holidays beyond that horizon
    # are reflected correctly.
    exchange_calendar: str = Field(default="XNYS")

    # Persistence / connectivity
    db_url: str = Field(default="sqlite:///options_agent.db")
    alpaca_paper: bool = Field(default=True)

    # Order execution
    # Poll every order_poll_interval_secs for fill status.
    # After order_poll_timeout_secs submit() returns current status
    # (which may be WORKING — see BrokerClient.submit docstring).
    # order_limit_offset_from_mid: non-negative slippage allowance added to
    # mid for buy orders, subtracted for sell orders.  0.0 = mid exactly.
    order_poll_interval_secs: float = Field(default=2.0, gt=0)
    order_poll_timeout_secs: float = Field(default=30.0, gt=0)
    order_limit_offset_from_mid: float = Field(default=0.0, ge=0)

    # WP-0.5 slice entry limit price (net combo price; negative = credit received).
    # The default -1.50 is used in normal paper runs. Override to -0.01 in the
    # smoke test to guarantee fill independent of live market levels — a near-zero
    # credit limit fills trivially on paper. WP-3/WP-8 replaces this field with
    # real mid-price pricing logic.
    # Guard: may only be changed from default on alpaca_paper=True runs.
    slice_limit_price: float = Field(default=-1.50)

    # Risk limits (nested)
    limits: Limits = Field(default_factory=Limits)

    @field_validator("exchange_calendar")
    @classmethod
    def _validate_exchange_calendar(cls, v: str) -> str:
        valid = xcals.get_calendar_names()
        if v not in valid:
            raise ValueError(
                f"Unknown exchange calendar name: {v!r}. "
                f"See exchange_calendars.get_calendar_names() for valid names."
            )
        return v

    @model_validator(mode="after")
    def _slice_limit_price_paper_only(self) -> "Config":
        if not self.alpaca_paper and self.slice_limit_price != -1.50:
            raise ValueError(
                "slice_limit_price may only be overridden on paper runs "
                "(alpaca_paper=True). Do not set a non-default slice_limit_price "
                "on a live account."
            )
        return self

    @classmethod
    def from_toml(cls, path: Path) -> "Config":
        """Load Config from a TOML file at *path*."""
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls.model_validate(data)
