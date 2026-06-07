from pydantic import BaseModel, Field, model_validator


class ChainFilterLimits(BaseModel):
    """Thresholds applied by get_filtered_chain to pre-filter the option chain.

    A contract passes the spread check if:
      spread ≤ max_spread_pct_of_mid * mid  OR  spread ≤ max_spread_abs_floor
    The floor prevents cheap-but-tight contracts from being falsely excluded
    by the percentage rule.
    """

    min_open_interest: int = Field(default=500, ge=0)
    max_spread_pct_of_mid: float = Field(default=0.10, gt=0, le=1.0)
    max_spread_abs_floor: float = Field(default=0.05, ge=0)
    min_dte: int = Field(default=20, ge=0)
    max_dte: int = Field(default=45, ge=0)
    # Absolute value of delta; covers both calls (positive) and puts (negative).
    min_abs_delta: float = Field(default=0.15, ge=0, le=1.0)
    max_abs_delta: float = Field(default=0.45, ge=0, le=1.0)

    @model_validator(mode="after")
    def _dte_range_valid(self) -> "ChainFilterLimits":
        if self.min_dte >= self.max_dte:
            raise ValueError(
                f"min_dte ({self.min_dte}) must be < max_dte ({self.max_dte})"
            )
        return self

    @model_validator(mode="after")
    def _delta_range_valid(self) -> "ChainFilterLimits":
        if self.min_abs_delta >= self.max_abs_delta:
            raise ValueError(
                f"min_abs_delta ({self.min_abs_delta}) must be"
                f" < max_abs_delta ({self.max_abs_delta})"
            )
        return self


class ExitPlanDefaults(BaseModel):
    """Default ExitPlan values applied when a proposal does not supply its own.

    Precedence: proposal.exit_plan fields take priority; these fill any gaps.

    stop_loss_mult is applied as: max_loss = mult × abs(entry_net_amount).
    Using the signed entry_net_amount (negative for credits, positive for
    debits per the WP-0.3 convention) means this formula covers both
    credit and debit strategies without a separate field.
    """

    profit_target_pct: float = Field(default=0.50, gt=0, le=1.0)
    stop_loss_mult: float = Field(default=2.0, gt=0)
    time_stop_dte: int = Field(default=21, ge=0)


class Limits(BaseModel):
    """All numeric risk thresholds and filter parameters in one place.

    Greek bands are expressed as fractions of current account equity so they
    remain meaningful across different account sizes:

      max_dollar_delta_pct: |net dollar-delta| ≤ pct × equity
        e.g. 0.20 → net directional exposure ≤ 20% of account value

      max_dollar_vega_pct: |net dollar-vega per 1 vol pt| ≤ pct × equity
        e.g. 0.025 → vega exposure ≤ 2.5% of account per 1-point IV move

    min_total_theta is intentionally unconstrained (None) at v0. A positive
    floor would silently ban all long-premium strategies (debit spreads in
    low-IV regimes). Leave it None and let playbook + IV-rank logic govern
    premium direction.

    max_sector_concentration_pct is disabled (None) until WP-3 attaches
    sector data to universe symbols. The field exists so WP-4 can read it
    without a contract change when sector data becomes available.

    limits_version must be stamped into every ContextSnapshot and
    JournalRecord so WP-7 analytics can correlate trade outcomes with the
    exact limits active at the time. Bump it whenever any threshold changes.
    """

    limits_version: str = "0.1.0"

    # Risk / sizing
    max_loss_per_trade_pct: float = Field(default=0.01, gt=0, le=1.0)
    max_open_positions: int = Field(default=5, ge=1)

    # Greek bands (equity-normalised dollar-Greeks)
    max_dollar_delta_pct: float = Field(default=0.20, gt=0, le=1.0)
    max_dollar_vega_pct: float = Field(default=0.025, gt=0, le=1.0)
    min_total_theta: float | None = Field(default=None)

    # Concentration caps
    max_underlying_concentration_pct: float = Field(default=0.20, gt=0, le=1.0)
    max_sector_concentration_pct: float | None = Field(default=None)

    # Nested limits
    chain_filter: ChainFilterLimits = Field(default_factory=ChainFilterLimits)
    exit_plan_defaults: ExitPlanDefaults = Field(default_factory=ExitPlanDefaults)
