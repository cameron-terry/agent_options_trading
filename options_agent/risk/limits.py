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
    # Per-right cap applied after all filters to control FilteredChain token budget.
    # For two-sided strategies (iron_condor, iron_butterfly) the cap is applied
    # independently to calls and puts so neither wing is starved.
    max_contracts_per_chain: int = Field(default=100, ge=1)

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

    stop_loss_max_loss_fraction is applied as:
      trigger when unrealized_pnl <= -(fraction × est_max_loss)
    Value in (0, 1]: 0.5 means close when 50% of max loss is realised.
    This formula is uniform across credit and debit strategies.
    (WP-0 amendment, WP-5.1: renamed from stop_loss_mult.)
    """

    profit_target_pct: float = Field(default=0.50, gt=0, le=1.0)
    stop_loss_max_loss_fraction: float = Field(default=0.5, gt=0, le=1.0)
    time_stop_dte: int = Field(default=21, ge=0)


class ExitPlanBounds(BaseModel):
    """Policy bounds checked by the validator against a proposal's exit_plan.

    Distinct from ExitPlanDefaults, which answers "what's a sensible default?"
    These answer "what does policy allow?" — a separately-tunable question.
    All bounds are inclusive. Bump limits_version on any change.

    stop_loss_max_loss_fraction bounds apply to the fraction of est_max_loss
    used as the stop-loss threshold. Bounds are in (0, 1].
    (WP-0 amendment, WP-5.1: renamed from stop_loss_mult_min/max.)
    """

    profit_target_pct_min: float = Field(default=0.25, gt=0, le=1.0)
    profit_target_pct_max: float = Field(default=1.0, gt=0, le=1.0)
    stop_loss_max_loss_fraction_min: float = Field(default=0.1, gt=0, le=1.0)
    stop_loss_max_loss_fraction_max: float = Field(default=1.0, gt=0, le=1.0)
    time_stop_dte_min: int = Field(default=7, ge=0)
    time_stop_dte_max: int = Field(default=45, ge=0)

    @model_validator(mode="after")
    def _profit_target_range_valid(self) -> "ExitPlanBounds":
        if self.profit_target_pct_min >= self.profit_target_pct_max:
            raise ValueError(
                f"profit_target_pct_min ({self.profit_target_pct_min}) must be"
                f" < profit_target_pct_max ({self.profit_target_pct_max})"
            )
        return self

    @model_validator(mode="after")
    def _stop_loss_max_loss_fraction_range_valid(self) -> "ExitPlanBounds":
        if self.stop_loss_max_loss_fraction_min >= self.stop_loss_max_loss_fraction_max:
            raise ValueError(
                "stop_loss_max_loss_fraction_min"
                f" ({self.stop_loss_max_loss_fraction_min}) must be <"
                " stop_loss_max_loss_fraction_max"
                f" ({self.stop_loss_max_loss_fraction_max})"
            )
        return self

    @model_validator(mode="after")
    def _time_stop_dte_range_valid(self) -> "ExitPlanBounds":
        if self.time_stop_dte_min >= self.time_stop_dte_max:
            raise ValueError(
                f"time_stop_dte_min ({self.time_stop_dte_min}) must be"
                f" < time_stop_dte_max ({self.time_stop_dte_max})"
            )
        return self


class Limits(BaseModel):
    """All numeric risk thresholds and filter parameters in one place.

    Greek bands are expressed as fractions of current account equity so they
    remain meaningful across different account sizes:

      max_dollar_delta_pct: |net dollar-delta| ≤ pct × equity
        e.g. 0.20 → net directional exposure ≤ 20% of account value

      max_dollar_vega_pct: |net dollar-vega per 1 vol pt| ≤ pct × equity
        e.g. 0.025 → vega exposure ≤ 2.5% of account per 1-point IV move

    Greek unit convention (confirmed WP-4.4):
      dollar_delta = net_delta × underlying_price × 100 × contracts
        Notional-equivalent $ of underlying. "Directional exposure ≤ N% of equity"
        means the portfolio behaves like holding N% of equity in the underlying.
      dollar_vega  = net_vega × 100 × contracts
        $ per 1 vol-point (1%) move in IV. No underlying_price factor.
      dollar_theta = net_theta × 100 × contracts
        $ decay per calendar day.
    PortfolioState.net_dollar_* must use this same convention (WP-3/6 scope).

    min_total_theta is intentionally unconstrained (None) at v0. A positive
    floor would silently ban all long-premium strategies (debit spreads in
    low-IV regimes). Leave it None and let playbook + IV-rank logic govern
    premium direction.

    max_sector_concentration_pct is disabled (None) until WP-3 attaches
    sector data to universe symbols. The field exists so WP-4 can read it
    without a contract change when sector data becomes available.

    event_blackout_days applies to both confirmed and estimated earnings dates
    (SymbolSnapshot.days_to_earnings counts down from whichever date is
    available). The gate condition is:
        days_to_earnings is not None and days_to_earnings <= event_blackout_days
    None (no known earnings) MUST pass — do not invert the null case.
    This is an entry gate only: it does not force-close existing positions.
    If a wider window for estimated dates is needed later, the clean extension
    is a separate estimated_event_blackout_days field — do not build that now.

    min_buying_power_pct gates pre-flight against PortfolioState.options_buying_power
    (not buying_power — the options figure is the honest constraint for spreads).
    Gate fires when: options_buying_power < min_buying_power_pct * account_equity.
    Returns ShortCircuitReason.NO_BUYING_POWER so WP-7 can distinguish capital
    starvation from other gate failures. Percentage form keeps this consistent
    with all other equity-relative limits.

    conviction_floor is the gate threshold for the gate-and-flat sizing model:
    conviction ≤ floor → 0 contracts (CONVICTION_FLOOR). At or above the floor
    the full risk budget is used regardless of the conviction value.  This
    prevents the agent's uncalibrated confidence from directly scaling position
    size.  Once WP-7 shows conviction is predictive, the floor can be tuned or
    a conviction_scaled mode can replace it without changing the field set.

    limits_version must be stamped into every ContextSnapshot and
    JournalRecord so WP-7 analytics can correlate trade outcomes with the
    exact limits active at the time. Bump it whenever any threshold changes.
    """

    limits_version: str = "0.3.0"

    # Risk / sizing
    max_loss_per_trade_pct: float = Field(default=0.01, gt=0, le=1.0)
    max_open_positions: int = Field(default=5, ge=1)
    conviction_floor: float = Field(default=0.35, ge=0, le=1.0)

    # Greek bands (equity-normalised dollar-Greeks)
    max_dollar_delta_pct: float = Field(default=0.20, gt=0, le=1.0)
    max_dollar_vega_pct: float = Field(default=0.025, gt=0, le=1.0)
    min_total_theta: float | None = Field(default=None)

    # Concentration caps
    max_underlying_concentration_pct: float = Field(default=0.20, gt=0, le=1.0)
    max_sector_concentration_pct: float | None = Field(default=None)

    # Allowed strategy names — proposals with strategy not in this set are
    # rejected by validate_structural() with UNKNOWN_STRATEGY. The playbook in
    # agent/prompts.py (WP-6.3) must import these same names so it is
    # impossible for the agent to propose a strategy the validator will reject.
    allowed_strategies: frozenset[str] = Field(
        default=frozenset(
            {
                "bull_put_spread",
                "bear_call_spread",
                "bull_call_spread",
                "bear_put_spread",
                "iron_condor",
                "iron_butterfly",
                "covered_call",
                "cash_secured_put",
            }
        )
    )

    # Event proximity (entry gate only — does not affect open positions)
    event_blackout_days: int = Field(default=5, ge=0)

    # Bias detection (WP-7.4 / PR #70): minimum closed-position count per metric cell
    # before reporting a signal. Cells below this floor return NaN / "insufficient_data"
    # so noise is never surfaced as a confident claim. Tune up as trade count grows.
    # WP-0 amendment (A4-1): kept in Limits (not obs config) — already imported by
    # obs/review.py, consistent with event_blackout_days placement.
    bias_min_sample_size: int = Field(default=10, ge=1)

    # Buying power floor (pre-flight gate; reads options_buying_power)
    min_buying_power_pct: float = Field(default=0.10, gt=0, le=1.0)

    # Duplicate detection: positions on same underlying with same strategy
    # whose nearest expiration is within this many calendar days of the proposal's
    # earliest leg expiration are considered duplicates.
    expiration_overlap_days: int = Field(default=5, ge=0)

    # Conflict detection: when |proposal.net_delta| exceeds this tolerance,
    # check for existing positions with opposing delta direction on same underlying.
    # Uses strategy-family heuristic until Position carries live delta (WP-0 gap).
    opposing_delta_tolerance: float = Field(default=0.05, ge=0)

    # Nested limits
    chain_filter: ChainFilterLimits = Field(default_factory=ChainFilterLimits)
    exit_plan_defaults: ExitPlanDefaults = Field(default_factory=ExitPlanDefaults)
    exit_plan_bounds: ExitPlanBounds = Field(default_factory=ExitPlanBounds)
