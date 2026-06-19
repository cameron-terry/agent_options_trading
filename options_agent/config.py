import tomllib
from datetime import time
from pathlib import Path

import exchange_calendars as xcals
from pydantic import BaseModel, Field, field_validator, model_validator

from options_agent.risk.limits import Limits


class PlaybookConfig(BaseModel):
    """Strategy playbook — single source of truth for prompt rendering and enforcement.

    Both agent/prompts.py (rendered into the system prompt) and risk/validator.py
    (UNKNOWN_STRATEGY check via Limits.allowed_strategies) derive their strategy sets
    from this object. Never maintain a parallel strategy list elsewhere.

    Bump playbook_version whenever thresholds or strategy sets change so WP-7 analytics
    can correlate trade outcomes with the exact playbook active at the time.

    IV-rank bands (0.0–1.0 percentile rank of the symbol's trailing-year IV):
      high   ≥ iv_rank_high_threshold  → sell premium (credit structures)
      medium  between thresholds       → agent's discretion (both postures allowed)
      low    < iv_rank_low_threshold   → buy premium (debit structures)
      None                             → iv_rank unknown; agent must propose NO_ACTION

    VIX regime tiers are advisory context for the agent's thesis, not enforced rules.
    Named for volatility level, not market direction (low-vol ≠ bullish).
    """

    playbook_version: str = "1.0.0"

    # IV-rank band thresholds (percentile, 0.0–1.0)
    iv_rank_high_threshold: float = Field(default=0.50, ge=0.0, le=1.0)
    iv_rank_low_threshold: float = Field(default=0.25, ge=0.0, le=1.0)

    # VIX regime thresholds (index points)
    vix_high_vol_threshold: float = Field(default=25.0, gt=0.0)
    vix_low_vol_threshold: float = Field(default=15.0, gt=0.0)

    # Strategy sets per IV band — high/low are hard-enforced; medium is permissive.
    # covered_call and cash_secured_put appear in high/medium only (selling premium
    # in low-IV environments has little premium to capture); the prompt additionally
    # gates them on holding/cash conditions.
    high_iv_strategies: frozenset[str] = Field(
        default=frozenset(
            {
                "bear_call_spread",
                "bull_put_spread",
                "cash_secured_put",
                "covered_call",
                "iron_butterfly",
                "iron_condor",
            }
        )
    )
    medium_iv_strategies: frozenset[str] = Field(
        default=frozenset(
            {
                "bear_call_spread",
                "bear_put_spread",
                "bull_call_spread",
                "bull_put_spread",
                "cash_secured_put",
                "covered_call",
                "iron_butterfly",
                "iron_condor",
            }
        )
    )
    low_iv_strategies: frozenset[str] = Field(
        default=frozenset({"bear_put_spread", "bull_call_spread"})
    )

    @model_validator(mode="after")
    def _threshold_ordering_valid(self) -> "PlaybookConfig":
        if self.iv_rank_low_threshold >= self.iv_rank_high_threshold:
            raise ValueError(
                f"iv_rank_low_threshold ({self.iv_rank_low_threshold}) must be"
                f" < iv_rank_high_threshold ({self.iv_rank_high_threshold})"
            )
        if self.vix_low_vol_threshold >= self.vix_high_vol_threshold:
            raise ValueError(
                f"vix_low_vol_threshold ({self.vix_low_vol_threshold}) must be"
                f" < vix_high_vol_threshold ({self.vix_high_vol_threshold})"
            )
        return self

    @property
    def all_allowed_strategies(self) -> frozenset[str]:
        """Union of all IV-band sets — used to populate Limits.allowed_strategies."""
        return (
            self.high_iv_strategies | self.medium_iv_strategies | self.low_iv_strategies
        )

    def allowed_for_iv_band(self, iv_rank: float | None) -> frozenset[str] | None:
        """Strategy set for the given IV rank, or None if iv_rank is unknown.

        None signals that no playbook cell applies — the agent must propose NO_ACTION.
        """
        if iv_rank is None:
            return None
        if iv_rank >= self.iv_rank_high_threshold:
            return self.high_iv_strategies
        if iv_rank < self.iv_rank_low_threshold:
            return self.low_iv_strategies
        return self.medium_iv_strategies

    def iv_band_label(self, iv_rank: float | None) -> str:
        """Human-readable band name for logging and prompt rendering."""
        if iv_rank is None:
            return "unknown"
        if iv_rank >= self.iv_rank_high_threshold:
            return "high"
        if iv_rank < self.iv_rank_low_threshold:
            return "low"
        return "medium"

    def regime_label(self, vix: float | None) -> str:
        """Advisory VIX regime name for prompt context."""
        if vix is None:
            return "unknown"
        if vix > self.vix_high_vol_threshold:
            return "high-vol"
        if vix < self.vix_low_vol_threshold:
            return "low-vol"
        return "normal"


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

    # Agent / reasoner settings
    # model_id is stamped on ContextSnapshot so before/after model comparisons
    # in WP-7 can filter without unpacking nested snapshots.
    # Default: claude-sonnet-4-6 — cost-efficient baseline for the 90-day paper
    # run; upgrade to claude-opus-4-8 only after the journal names a specific
    # reasoning failure that warrants it.
    model_id: str = Field(default="claude-sonnet-4-6")
    # max_schema_retries: additional commit attempts after the first schema-invalid
    # response. Total attempts = max_schema_retries + 1.
    max_schema_retries: int = Field(default=2, ge=0, le=10)
    # max_reasoning_turns: cap on exploration-phase turns before forcing commit.
    max_reasoning_turns: int = Field(default=10, ge=1, le=50)
    # max_tokens: output token cap for both exploration and commit API calls.
    # 4096 covers most proposals; raise if the agent truncates during verbose
    # multi-tool exploration runs or produces long rationale fields.
    max_tokens: int = Field(default=4096, ge=1, le=65536)

    # Risk limits (nested)
    limits: Limits = Field(default_factory=Limits)

    # Strategy playbook — single source of truth for prompts.py and validator.py.
    # Limits.allowed_strategies is derived from this; never set it independently.
    playbook: PlaybookConfig = Field(default_factory=PlaybookConfig)

    @model_validator(mode="after")
    def _sync_allowed_strategies(self) -> "Config":
        # PlaybookConfig is authoritative when allowed_strategies was not explicitly
        # provided. Checking model_fields_set preserves intentional overrides (e.g.
        # tests that force an empty set to exercise UNKNOWN_STRATEGY rejection).
        if "allowed_strategies" not in self.limits.model_fields_set:
            self.limits.allowed_strategies = self.playbook.all_allowed_strategies
        return self

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
