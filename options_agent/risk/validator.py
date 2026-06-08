from pydantic import ValidationError

from options_agent.contracts.data import FilteredChain, PortfolioState, SymbolSnapshot
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
_STRATEGY_DELTA_SIGN: dict[str, int] = {
    "bull_put_spread": 1,
    "bull_call_spread": 1,
    "cash_secured_put": 1,
    "covered_call": 1,
    "bear_call_spread": -1,
    "bear_put_spread": -1,
    "iron_condor": 0,
    "iron_butterfly": 0,
}


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
    event_reason = _check_event_gate(proposal.underlying, limits, symbol_snapshot)
    if event_reason is not None:
        reasons.append(event_reason)

    # 5. Buying power floor.
    bp_reason = _check_buying_power(portfolio, limits)
    if bp_reason is not None:
        reasons.append(bp_reason)

    # 6. Duplicate / conflicting positions on same underlying.
    reasons.extend(_check_duplicate_and_conflict(proposal, portfolio, limits))

    return reasons


# ---------------------------------------------------------------------------
# Private sub-checks
# ---------------------------------------------------------------------------


def _check_naked_short(legs: list[Leg]) -> RejectionReason | None:
    """Per-right net ratio check.

    For each option right (call/put), the total buy ratio across all legs must
    be >= the total sell ratio. A single uncovered sell leg of any ratio is
    rejected — there is no config override (Core Principle 3).

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
                    f" buy ratio {buy_ratio} — add a covering long {right} leg"
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

        if contract.open_interest < chain_limits.min_open_interest:
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

    if not b.stop_loss_mult_min <= exit_plan.stop_loss_mult <= b.stop_loss_mult_max:
        reasons.append(
            RejectionReason(
                rule_id=ValidationRuleId.INVALID_EXIT_PLAN,
                severity=Severity.ERROR,
                human_message=(
                    f"stop_loss_mult {exit_plan.stop_loss_mult} outside"
                    f" policy [{b.stop_loss_mult_min}, {b.stop_loss_mult_max}]"
                ),
                field_affected="exit_plan.stop_loss_mult",
                observed=exit_plan.stop_loss_mult,
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
) -> RejectionReason | None:
    """Reject if earnings are within the blackout window; fail closed on missing data.

    None days_to_earnings means the earnings date is unknown — that is NOT the
    same as 'no earnings coming'. Missing event data must block trading because
    an unknown earnings situation is exactly the IV-crush failure mode the
    catalyst_check machinery exists to prevent.
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

    if symbol_snapshot.days_to_earnings is None:
        return RejectionReason(
            rule_id=ValidationRuleId.EVENT_DATA_MISSING,
            severity=Severity.ERROR,
            human_message=(
                f"earnings date unknown for {underlying};"
                " cannot verify proximity — failing closed"
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
