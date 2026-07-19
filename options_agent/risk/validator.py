import math

from pydantic import ValidationError

from options_agent.contracts.data import (
    EventInfo,
    FilteredChain,
    PortfolioState,
    SymbolSnapshot,
)
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.results import (
    RejectionReason,
    Severity,
    ValidationResult,
    ValidationRuleId,
)
from options_agent.contracts.state import KillSwitchState, PositionStatus
from options_agent.risk.limits import Limits

# Maps strategy name → delta direction sign for conflict detection.
# Position.net_delta is not available in contracts (WP-0 gap); this heuristic
# covers all playbook strategies until Position carries live delta.
#
# MAINTENANCE: This dict must mirror PlaybookConfig.all_allowed_strategies.
# PlaybookConfig (config.py / config.toml [playbook]) is the authoritative source.
# If a strategy is added to the playbook without a corresponding entry here,
# conflict detection silently fails open for that strategy. Update both together.
_STRATEGY_DELTA_SIGN: dict[str, int] = {
    "bull_put_spread": 1,
    "bull_call_spread": 1,
    "bear_call_spread": -1,
    "bear_put_spread": -1,
    "iron_condor": 0,
}

_OPTION_MULTIPLIER = 100  # shares per US equity options contract


def validate_from_dict(raw: dict, limits: Limits) -> ValidationResult:
    """Parse *raw* into a TradeProposal and run structural validation.

    Use this at the orchestrator boundary where the agent's JSON output may
    fail schema validation before a TradeProposal can be constructed.
    """
    try:
        proposal = TradeProposal.model_validate(raw)
    except ValidationError as exc:
        first_error = exc.errors(include_url=False)[0]
        field = ".".join(str(loc) for loc in first_error["loc"]) or "<root>"
        return ValidationResult(
            passed=False,
            reasons=[
                RejectionReason(
                    rule_id=ValidationRuleId.INVALID_SCHEMA,
                    severity=Severity.ERROR,
                    human_message=f"schema error on '{field}': {first_error['msg']}",
                    field_affected=field,
                )
            ],
        )
    return validate_structural(proposal, limits)


def validate_structural(proposal: TradeProposal, limits: Limits) -> ValidationResult:
    """Run the three structural checks on an already-parsed TradeProposal.

    Checks are ordered fastest-to-slowest and stop at the first ERROR finding.
    This function needs no portfolio state or live market data.
    """
    # 1. Playbook check
    if proposal.strategy not in limits.allowed_strategies:
        return ValidationResult(
            passed=False,
            reasons=[
                RejectionReason(
                    rule_id=ValidationRuleId.UNKNOWN_STRATEGY,
                    severity=Severity.ERROR,
                    human_message=(
                        f"strategy '{proposal.strategy}' is not in the allowed"
                        " playbook; update limits.allowed_strategies or the agent"
                        " playbook"
                    ),
                    field_affected="strategy",
                )
            ],
        )

    # 2. Naked-short check (unconditional — no config override)
    naked_reason = _check_naked_short(proposal.legs)
    if naked_reason is not None:
        return ValidationResult(passed=False, reasons=[naked_reason])

    return ValidationResult(passed=True, reasons=[])


def validate_market_access(
    proposal: TradeProposal,
    limits: Limits,
    symbol_snapshot: SymbolSnapshot | None,
    portfolio: PortfolioState,
    kill_switch_state: KillSwitchState,
    filtered_chain: FilteredChain | None,
    event_info: EventInfo | None = None,
) -> list[RejectionReason]:
    """Run market-access checks on an already structurally-valid TradeProposal.

    Unlike validate_structural (which stops at the first error), all checks run
    and all failures are collected so the orchestrator can journal every violation.
    Exception: kill-switch short-circuits immediately — no further checks needed.

    Parameters
    ----------
    symbol_snapshot:
        SymbolSnapshot for proposal.underlying. Pass None if unavailable;
        event-gate check will fail closed with EVENT_DATA_MISSING.
    filtered_chain:
        FilteredChain for proposal.underlying. Pass None if unavailable;
        all liquidity checks will fail closed with LIQUIDITY_SPREAD.
    event_info:
        EventInfo for proposal.underlying (from the same fetch that populated
        symbol_snapshot.days_to_earnings). None or data_available=False fails
        closed with EVENT_DATA_MISSING; earnings=None with data_available=True
        passes — the normal, permanent state for ETFs.
    """
    # 1. Kill-switch — short-circuit if halted; no new entries permitted.
    if kill_switch_state in (KillSwitchState.HALT, KillSwitchState.FLATTEN):
        return [
            RejectionReason(
                rule_id=ValidationRuleId.KILL_SWITCH,
                severity=Severity.ERROR,
                human_message=(
                    f"kill switch is {kill_switch_state.value};"
                    " no new entries permitted"
                ),
            )
        ]

    reasons: list[RejectionReason] = []

    # 2. Liquidity — per leg, using chain snapshot.
    reasons.extend(_check_liquidity(proposal.legs, limits, filtered_chain))

    # 3. Exit plan policy bounds.
    reasons.extend(_check_exit_plan_bounds(proposal.exit_plan, limits))

    # 4. Event proximity blackout.
    event_reason = _check_event_gate(
        proposal.underlying, limits, symbol_snapshot, event_info
    )
    if event_reason is not None:
        reasons.append(event_reason)

    # 5. Buying power floor.
    bp_reason = _check_buying_power(portfolio, limits)
    if bp_reason is not None:
        reasons.append(bp_reason)

    # 6. Duplicate / conflicting positions on same underlying.
    reasons.extend(_check_duplicate_and_conflict(proposal, portfolio, limits))

    return reasons


def validate_risk_caps(
    proposal: TradeProposal,
    portfolio_state: PortfolioState,
    limits: Limits,
    *,
    contracts: int,
    underlying_price: float,
) -> ValidationResult:
    """Risk-cap checks that require portfolio state and the sized contract count.

    Called after validate_structural() passes and after size() determines the
    contract count. Intended call order in the entry cycle (WP-8):
      1. validate_structural(proposal, limits)     -- structural, no portfolio needed
      2. size(proposal, portfolio_state, limits)         -- determines contracts
      3. validate_risk_caps(proposal, portfolio_state, limits,
                            contracts=result.contracts, underlying_price=price)

    All checks run independently -- every failure is collected rather than
    fast-failing, so the orchestrator can journal the full rejection picture.
    Exception: MAX_LOSS_NOT_FINITE short-circuits immediately (subsequent checks
    would compare against a corrupted value).

    Checks:
      MAX_LOSS_NOT_FINITE   -- est_max_loss is finite and positive (must run first)
      MAX_LOSS_CAP          -- per-contract est_max_loss <= per-trade risk budget
      PORTFOLIO_DELTA_BAND  -- post-trade |net_dollar_delta| within configured band
      PORTFOLIO_VEGA_BAND   -- post-trade |net_dollar_vega| within configured band
      PORTFOLIO_THETA_FLOOR -- post-trade net_dollar_theta >= floor (opt-in; skipped
                               when limits.min_total_theta is None)
      CONCENTRATION_UNDERLYING -- post-trade underlying risk-weight <= cap
        (sector deferred until Position carries sector data from WP-3)

    Greek unit convention (confirmed with WP-4 owner):
      dollar_delta = proposal.net_delta x underlying_price x 100 x contracts
        Notional-equivalent $ of underlying exposure. Matches the interpretation of
        max_dollar_delta_pct as "net directional exposure <= N% of account value".
      dollar_vega  = proposal.net_vega x 100 x contracts
        $ per 1 vol-point (1%) move in IV; no underlying_price factor needed.
      dollar_theta = proposal.net_theta x 100 x contracts
        $ time decay per calendar day.
    PortfolioState.net_dollar_* must be computed using this same convention by
    context/portfolio.py (WP-3/6 scope). This resolves the "Confirm units" note
    in the PortfolioState docstring.

    NO_ACTION proposals pass unconditionally -- there is no trade to check.
    """
    if proposal.action == "NO_ACTION":
        return ValidationResult(passed=True, reasons=[])

    # Finiteness check before any comparison -- a NaN/inf est_max_loss would silently
    # corrupt subsequent comparisons (nan <= x is always False; the rejection would
    # appear as MAX_LOSS_CAP when the real problem is a malformed number).
    if not math.isfinite(proposal.est_max_loss) or proposal.est_max_loss <= 0:
        return ValidationResult(
            passed=False,
            reasons=[
                RejectionReason(
                    rule_id=ValidationRuleId.MAX_LOSS_NOT_FINITE,
                    severity=Severity.ERROR,
                    human_message=(
                        f"est_max_loss {proposal.est_max_loss!r} must be a finite"
                        " positive number; check the proposal's risk parameters"
                    ),
                    field_affected="est_max_loss",
                    observed=(
                        proposal.est_max_loss
                        if math.isfinite(proposal.est_max_loss)
                        else None
                    ),
                )
            ],
        )

    reasons: list[RejectionReason] = []
    _check_max_loss_cap(proposal, portfolio_state, limits, reasons)
    _check_greek_bands(
        proposal, portfolio_state, limits, contracts, underlying_price, reasons
    )
    _check_concentration(proposal, portfolio_state, limits, contracts, reasons)

    if reasons:
        return ValidationResult(passed=False, reasons=reasons)
    return ValidationResult(passed=True, reasons=[])


# ---------------------------------------------------------------------------
# Private sub-checks for validate_risk_caps
# ---------------------------------------------------------------------------


def _check_max_loss_cap(
    proposal: TradeProposal,
    portfolio_state: PortfolioState,
    limits: Limits,
    reasons: list[RejectionReason],
) -> None:
    max_loss_cap = limits.max_loss_per_trade_pct * portfolio_state.account_equity
    if proposal.est_max_loss > max_loss_cap:
        reasons.append(
            RejectionReason(
                rule_id=ValidationRuleId.MAX_LOSS_CAP,
                severity=Severity.ERROR,
                human_message=(
                    f"est_max_loss {proposal.est_max_loss:.2f} exceeds per-trade cap"
                    f" {max_loss_cap:.2f} ({limits.max_loss_per_trade_pct:.1%} of"
                    f" equity {portfolio_state.account_equity:.2f})"
                ),
                field_affected="est_max_loss",
                observed=proposal.est_max_loss,
                limit=max_loss_cap,
            )
        )


def _check_greek_bands(
    proposal: TradeProposal,
    portfolio_state: PortfolioState,
    limits: Limits,
    contracts: int,
    underlying_price: float,
    reasons: list[RejectionReason],
) -> None:
    # Delta: dollar_delta = net_delta x underlying_price x 100 x contracts
    dollar_delta_added = (
        proposal.net_delta * underlying_price * _OPTION_MULTIPLIER * contracts
    )
    post_dollar_delta = portfolio_state.net_dollar_delta + dollar_delta_added
    delta_cap = limits.max_dollar_delta_pct * portfolio_state.account_equity
    if abs(post_dollar_delta) > delta_cap:
        reasons.append(
            RejectionReason(
                rule_id=ValidationRuleId.PORTFOLIO_DELTA_BAND,
                severity=Severity.ERROR,
                human_message=(
                    f"post-trade net_dollar_delta {post_dollar_delta:.2f} would exceed"
                    f" band +-{delta_cap:.2f}"
                    f" (+-{limits.max_dollar_delta_pct:.1%} of equity)"
                ),
                field_affected="net_delta",
                observed=post_dollar_delta,
                limit=delta_cap,
            )
        )

    # Vega: dollar_vega = net_vega x 100 x contracts (no underlying_price factor)
    dollar_vega_added = proposal.net_vega * _OPTION_MULTIPLIER * contracts
    post_dollar_vega = portfolio_state.net_dollar_vega + dollar_vega_added
    vega_cap = limits.max_dollar_vega_pct * portfolio_state.account_equity
    if abs(post_dollar_vega) > vega_cap:
        reasons.append(
            RejectionReason(
                rule_id=ValidationRuleId.PORTFOLIO_VEGA_BAND,
                severity=Severity.ERROR,
                human_message=(
                    f"post-trade net_dollar_vega {post_dollar_vega:.2f} would exceed"
                    f" band +-{vega_cap:.2f}"
                    f" (+-{limits.max_dollar_vega_pct:.1%} of equity)"
                ),
                field_affected="net_vega",
                observed=post_dollar_vega,
                limit=vega_cap,
            )
        )

    # Theta: opt-in (None = unconstrained; a non-None floor would ban long-premium
    # strategies such as debit spreads in low-IV regimes -- see limits.py docstring)
    if limits.min_total_theta is not None:
        # dollar_theta = net_theta x 100 x contracts
        dollar_theta_added = proposal.net_theta * _OPTION_MULTIPLIER * contracts
        post_dollar_theta = portfolio_state.net_dollar_theta + dollar_theta_added
        if post_dollar_theta < limits.min_total_theta:
            reasons.append(
                RejectionReason(
                    rule_id=ValidationRuleId.PORTFOLIO_THETA_FLOOR,
                    severity=Severity.ERROR,
                    human_message=(
                        f"post-trade net_dollar_theta {post_dollar_theta:.2f}"
                        f" would fall below floor {limits.min_total_theta:.2f}"
                    ),
                    field_affected="net_theta",
                    observed=post_dollar_theta,
                    limit=limits.min_total_theta,
                )
            )


def _check_concentration(
    proposal: TradeProposal,
    portfolio_state: PortfolioState,
    limits: Limits,
    contracts: int,
    reasons: list[RejectionReason],
) -> None:
    equity = portfolio_state.account_equity
    open_positions = [
        p for p in portfolio_state.positions if p.status == PositionStatus.OPEN
    ]

    # Underlying concentration: risk-weighted by est_max_loss x qty / equity.
    # est_max_loss is per-contract on both Position and TradeProposal
    # (WP-0.3 convention).
    existing_risk = sum(
        p.est_max_loss * p.quantity
        for p in open_positions
        if p.underlying == proposal.underlying
    )
    post_pct = (existing_risk + proposal.est_max_loss * contracts) / equity
    if post_pct > limits.max_underlying_concentration_pct:
        reasons.append(
            RejectionReason(
                rule_id=ValidationRuleId.CONCENTRATION_UNDERLYING,
                severity=Severity.ERROR,
                human_message=(
                    f"post-trade {proposal.underlying} concentration {post_pct:.1%}"
                    f" would exceed cap {limits.max_underlying_concentration_pct:.1%}"
                ),
                field_affected="underlying",
                observed=post_pct,
                limit=limits.max_underlying_concentration_pct,
            )
        )

    # Sector concentration: deferred until Position carries sector data (WP-3).
    # When limits.max_sector_concentration_pct is not None, implement analogously
    # using a sector key on Position (CONCENTRATION_SECTOR rule ID).


# ---------------------------------------------------------------------------
# Private sub-checks for validate_structural
# ---------------------------------------------------------------------------


def _check_naked_short(legs: list[Leg]) -> RejectionReason | None:
    """Per-right net ratio check.

    For each option right (call/put), the total buy ratio across all legs must
    be >= the total sell ratio. A single uncovered sell leg of any ratio is
    rejected -- there is no config override (Core Principle 3).

    Assumption agreed in WP-4.3 briefing: Option A (per-right net ratio).
    """
    for right in ("call", "put"):
        buy_ratio = sum(
            leg.ratio for leg in legs if leg.right == right and leg.side == "buy"
        )
        sell_ratio = sum(
            leg.ratio for leg in legs if leg.right == right and leg.side == "sell"
        )
        if sell_ratio > buy_ratio:
            offending = next(
                leg for leg in legs if leg.right == right and leg.side == "sell"
            )
            return RejectionReason(
                rule_id=ValidationRuleId.NAKED_SHORT,
                severity=Severity.ERROR,
                human_message=(
                    f"naked short {right}: sell ratio {sell_ratio} exceeds"
                    f" buy ratio {buy_ratio} -- add a covering long {right} leg"
                ),
                field_affected=(
                    f"legs[right={right!r}, side='sell', strike={offending.strike}]"
                ),
            )
    return None


def _check_liquidity(
    legs: list[Leg],
    limits: Limits,
    filtered_chain: FilteredChain | None,
) -> list[RejectionReason]:
    """Check bid-ask spread and open interest for each leg against chain limits.

    Fails closed if chain data is absent — a leg not found in the filtered chain
    means either bad agent output or stale data, both of which must not trade.
    """
    reasons: list[RejectionReason] = []
    chain_limits = limits.chain_filter

    if filtered_chain is None:
        for leg in legs:
            reasons.append(
                RejectionReason(
                    rule_id=ValidationRuleId.LIQUIDITY_SPREAD,
                    severity=Severity.ERROR,
                    human_message=(
                        f"no chain data available; cannot verify liquidity for"
                        f" {leg.right} leg at strike {leg.strike}"
                    ),
                    field_affected=(
                        f"legs[right={leg.right!r}, strike={leg.strike},"
                        f" expiration={leg.expiration}]"
                    ),
                )
            )
        return reasons

    chain_lookup = {
        (c.strike, c.expiration, c.right): c for c in filtered_chain.contracts
    }

    for leg in legs:
        key = (leg.strike, leg.expiration, leg.right)
        contract = chain_lookup.get(key)
        field_id = (
            f"legs[right={leg.right!r}, strike={leg.strike},"
            f" expiration={leg.expiration}]"
        )

        if contract is None:
            reasons.append(
                RejectionReason(
                    rule_id=ValidationRuleId.LIQUIDITY_SPREAD,
                    severity=Severity.ERROR,
                    human_message=(
                        f"{leg.right} leg at strike {leg.strike}"
                        f" exp {leg.expiration} not found in filtered chain"
                    ),
                    field_affected=field_id,
                )
            )
            continue

        # Spread fails only when BOTH the percentage rule and the absolute floor
        # are breached — matches the chain filter's pass logic (OR on the pass side).
        spread_pct_limit = chain_limits.max_spread_pct_of_mid * contract.mid
        if (
            contract.spread_width > spread_pct_limit
            and contract.spread_width > chain_limits.max_spread_abs_floor
        ):
            reasons.append(
                RejectionReason(
                    rule_id=ValidationRuleId.LIQUIDITY_SPREAD,
                    severity=Severity.ERROR,
                    human_message=(
                        f"{leg.right} leg at strike {leg.strike}: spread"
                        f" {contract.spread_width:.3f} exceeds both"
                        f" {chain_limits.max_spread_pct_of_mid:.0%} of mid"
                        f" ({spread_pct_limit:.3f}) and abs floor"
                        f" {chain_limits.max_spread_abs_floor:.3f}"
                    ),
                    field_affected=field_id,
                    observed=contract.spread_width,
                    limit=spread_pct_limit,
                )
            )

        if (
            contract.open_interest is not None
            and contract.open_interest < chain_limits.min_open_interest
        ):
            reasons.append(
                RejectionReason(
                    rule_id=ValidationRuleId.LIQUIDITY_OPEN_INTEREST,
                    severity=Severity.ERROR,
                    human_message=(
                        f"{leg.right} leg at strike {leg.strike}: OI"
                        f" {contract.open_interest} < minimum"
                        f" {chain_limits.min_open_interest}"
                    ),
                    field_affected=field_id,
                    observed=float(contract.open_interest),
                    limit=float(chain_limits.min_open_interest),
                )
            )

    return reasons


def _check_exit_plan_bounds(
    exit_plan: ExitPlan, limits: Limits
) -> list[RejectionReason]:
    """Check each ExitPlan field against Limits.exit_plan_bounds policy."""
    reasons: list[RejectionReason] = []
    b = limits.exit_plan_bounds

    ptp = exit_plan.profit_target_pct
    if not b.profit_target_pct_min <= ptp <= b.profit_target_pct_max:
        reasons.append(
            RejectionReason(
                rule_id=ValidationRuleId.INVALID_EXIT_PLAN,
                severity=Severity.ERROR,
                human_message=(
                    f"profit_target_pct {exit_plan.profit_target_pct} outside"
                    f" policy [{b.profit_target_pct_min}, {b.profit_target_pct_max}]"
                ),
                field_affected="exit_plan.profit_target_pct",
                observed=exit_plan.profit_target_pct,
            )
        )

    if not (
        b.stop_loss_max_loss_fraction_min
        <= exit_plan.stop_loss_max_loss_fraction
        <= b.stop_loss_max_loss_fraction_max
    ):
        reasons.append(
            RejectionReason(
                rule_id=ValidationRuleId.INVALID_EXIT_PLAN,
                severity=Severity.ERROR,
                human_message=(
                    "stop_loss_max_loss_fraction"
                    f" {exit_plan.stop_loss_max_loss_fraction} outside"
                    f" [{b.stop_loss_max_loss_fraction_min},"
                    f" {b.stop_loss_max_loss_fraction_max}]"
                ),
                field_affected="exit_plan.stop_loss_max_loss_fraction",
                observed=exit_plan.stop_loss_max_loss_fraction,
            )
        )

    if not b.time_stop_dte_min <= exit_plan.time_stop_dte <= b.time_stop_dte_max:
        reasons.append(
            RejectionReason(
                rule_id=ValidationRuleId.INVALID_EXIT_PLAN,
                severity=Severity.ERROR,
                human_message=(
                    f"time_stop_dte {exit_plan.time_stop_dte} outside"
                    f" policy [{b.time_stop_dte_min}, {b.time_stop_dte_max}]"
                ),
                field_affected="exit_plan.time_stop_dte",
                observed=float(exit_plan.time_stop_dte),
            )
        )

    return reasons


def _check_event_gate(
    underlying: str,
    limits: Limits,
    symbol_snapshot: SymbolSnapshot | None,
    event_info: EventInfo | None,
) -> RejectionReason | None:
    """Reject if earnings are within the blackout window; fail closed on missing data.

    "No earnings found" and "earnings unknown" are different states and must
    be treated differently (Limits docstring: the null case must pass):

      event_info None or data_available=False → the provider failed; an
        unknown earnings situation is exactly the IV-crush failure mode this
        gate exists to prevent — fail closed (EVENT_DATA_MISSING).
      earnings=None with data_available=True → the provider succeeded and
        found no earnings in the lookahead window. This is the permanent,
        normal state for ETFs (SPY/QQQ/IWM) — pass.
      earnings present → enforce the blackout window via
        symbol_snapshot.days_to_earnings (derived from the same fetch); a
        missing derivation is an internal inconsistency — fail closed.
    """
    if symbol_snapshot is None:
        return RejectionReason(
            rule_id=ValidationRuleId.EVENT_DATA_MISSING,
            severity=Severity.ERROR,
            human_message=(
                f"no symbol snapshot available for {underlying};"
                " cannot verify earnings proximity — failing closed"
            ),
            field_affected="underlying",
        )

    if event_info is None or not event_info.data_available:
        return RejectionReason(
            rule_id=ValidationRuleId.EVENT_DATA_MISSING,
            severity=Severity.ERROR,
            human_message=(
                f"event data unavailable for {underlying};"
                " cannot verify earnings proximity — failing closed"
            ),
            field_affected="underlying",
        )

    if event_info.earnings is None:
        # Provider succeeded, no earnings within the lookahead window.
        return None

    if symbol_snapshot.days_to_earnings is None:
        # Earnings exist but the snapshot derivation is missing — internal
        # inconsistency between EventInfo and SymbolSnapshot; fail closed.
        return RejectionReason(
            rule_id=ValidationRuleId.EVENT_DATA_MISSING,
            severity=Severity.ERROR,
            human_message=(
                f"earnings exist for {underlying} but days_to_earnings is"
                " unset; snapshot/event derivation inconsistent — failing closed"
            ),
            field_affected="underlying",
        )

    if symbol_snapshot.days_to_earnings <= limits.event_blackout_days:
        return RejectionReason(
            rule_id=ValidationRuleId.EVENT_BLACKOUT,
            severity=Severity.ERROR,
            human_message=(
                f"{underlying} has earnings in {symbol_snapshot.days_to_earnings}"
                f" day(s), within the {limits.event_blackout_days}-day blackout window"
            ),
            field_affected="underlying",
            observed=float(symbol_snapshot.days_to_earnings),
            limit=float(limits.event_blackout_days),
        )

    return None


def _check_buying_power(
    portfolio: PortfolioState,
    limits: Limits,
) -> RejectionReason | None:
    """Reject if options_buying_power is below the equity-relative floor."""
    required = limits.min_buying_power_pct * portfolio.account_equity
    if portfolio.options_buying_power < required:
        return RejectionReason(
            rule_id=ValidationRuleId.BUYING_POWER,
            severity=Severity.ERROR,
            human_message=(
                f"options_buying_power {portfolio.options_buying_power:.2f} <"
                f" {limits.min_buying_power_pct:.0%} of equity ({required:.2f})"
            ),
            observed=portfolio.options_buying_power,
            limit=required,
        )
    return None


def _check_duplicate_and_conflict(
    proposal: TradeProposal,
    portfolio: PortfolioState,
    limits: Limits,
) -> list[RejectionReason]:
    """Check for duplicate and directionally-conflicting positions.

    Duplicate: same underlying + same strategy + proposal's earliest leg
    expiration within expiration_overlap_days of position.nearest_expiration.

    Conflict: same underlying + proposal.net_delta opposes existing position's
    inferred delta direction. Direction is inferred from strategy name via
    _STRATEGY_DELTA_SIGN (approximation — see module-level note on WP-0 gap).
    PENDING_OPEN positions are included; this relies on WP-1 reconcile writing
    PENDING_OPEN before the next entry cycle reads portfolio state.
    """
    reasons: list[RejectionReason] = []

    active_statuses = {PositionStatus.OPEN, PositionStatus.PENDING_OPEN}
    same_underlying = [
        p
        for p in portfolio.positions
        if p.underlying == proposal.underlying and p.status in active_statuses
    ]

    if not same_underlying:
        return reasons

    proposal_min_exp = min(leg.expiration for leg in proposal.legs)

    for pos in same_underlying:
        # Duplicate check
        if pos.strategy == proposal.strategy:
            overlap_days = abs((proposal_min_exp - pos.nearest_expiration).days)
            if overlap_days <= limits.expiration_overlap_days:
                reasons.append(
                    RejectionReason(
                        rule_id=ValidationRuleId.DUPLICATE_POSITION,
                        severity=Severity.ERROR,
                        human_message=(
                            f"duplicate {proposal.strategy} on {proposal.underlying}:"
                            f" existing position expires {pos.nearest_expiration},"
                            f" proposal expires {proposal_min_exp}"
                            f" ({overlap_days}d apart, within"
                            f" {limits.expiration_overlap_days}-day window)"
                        ),
                        field_affected="underlying",
                    )
                )

        # Conflict check
        existing_delta_sign = _STRATEGY_DELTA_SIGN.get(pos.strategy, 0)
        if existing_delta_sign == 0:
            continue
        if abs(proposal.net_delta) <= limits.opposing_delta_tolerance:
            continue
        proposal_sign = 1 if proposal.net_delta > 0 else -1
        if proposal_sign != existing_delta_sign:
            sign_label = "positive" if existing_delta_sign > 0 else "negative"
            reasons.append(
                RejectionReason(
                    rule_id=ValidationRuleId.CONFLICTING_POSITION,
                    severity=Severity.ERROR,
                    human_message=(
                        f"conflicting position on {proposal.underlying}:"
                        f" proposal net_delta {proposal.net_delta:+.3f} opposes"
                        f" existing {pos.strategy} ({sign_label}-delta strategy)"
                    ),
                    field_affected="underlying",
                    observed=proposal.net_delta,
                )
            )

    return reasons
