"""Deterministic structure metrics computed from proposal legs + chain quotes.

The agent's TradeProposal carries self-reported est_max_loss, est_max_profit,
and net Greeks. Those numbers feed sizing, the stop-loss/profit-target
thresholds, concentration checks, and the Greek-band gates — trusting LLM
arithmetic there is an unforced error. Every playbook strategy is a
defined-risk, same-expiration structure whose risk metrics are exactly
computable from the legs and current chain quotes, so the orchestrator
recomputes them here and overrides the proposal's values before validation
and sizing.

The same payoff analysis is reused at fill confirmation (WP-1,
recompute_fill_metrics / apply_fill_metrics below) to correct
Position.est_max_loss/profit against the real net_fill_price — the pre-trade
chain mid is only ever an estimate; the fill can differ via slippage.

Conventions (match TradeProposal / Position):
  net_entry_mid   — per-combo-unit price at mid: positive = net debit paid,
                    negative = net credit received (Alpaca mleg convention).
  est_max_loss    — positive dollars per combo unit (contract), i.e. already
                    ×100 for the share multiplier.
  est_max_profit  — positive dollars per combo unit; None when the structure
                    has unbounded upside (should not occur for playbook
                    strategies, but long-ratio structures pass the naked-short
                    check and have open-ended profit).
  net_delta/theta/vega — per combo unit, per share (NOT ×100), matching the
                    convention validate_risk_caps applies
                    (dollar_greek = net_greek × ... × 100 × contracts).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from options_agent.contracts.data import FilteredChain, OptionContract
from options_agent.contracts.proposal import Leg

logger = logging.getLogger(__name__)

_OPTION_MULTIPLIER = 100


class StructureMetrics(BaseModel):
    """Deterministic per-combo-unit metrics for a proposal's leg structure.

    leg_quotes is ordered to match the proposal's legs list — the execution
    step feeds it straight into compute_multi_leg_limit_price().
    est_max_loss / est_max_profit are None when the payoff analysis does not
    apply (mixed expirations) or the bound is not finite; callers fall back
    to the agent's self-reported values in that case.
    """

    net_entry_mid: float
    est_max_loss: float | None
    est_max_profit: float | None
    net_delta: float
    net_theta: float
    net_vega: float
    leg_quotes: list[tuple[float, float]]


def _lookup(chain: FilteredChain, leg: Leg) -> OptionContract | None:
    for contract in chain.contracts:
        if (
            contract.strike == leg.strike
            and contract.expiration == leg.expiration
            and contract.right == leg.right
        ):
            return contract
    return None


def _payoff_per_share(legs: list[Leg], underlying_price: float) -> float:
    """Intrinsic value of the combo at expiration for one combo unit."""
    total = 0.0
    for leg in legs:
        sign = 1.0 if leg.side == "buy" else -1.0
        if leg.right == "call":
            intrinsic = max(underlying_price - leg.strike, 0.0)
        else:
            intrinsic = max(leg.strike - underlying_price, 0.0)
        total += sign * leg.ratio * intrinsic
    return total


def compute_payoff_bounds(
    legs: list[Leg], net_price: float
) -> tuple[float | None, float | None]:
    """Exact per-combo-unit (est_max_loss, est_max_profit) from expiration payoff.

    Assumes every leg shares one expiration — callers must verify this first
    (mixed-expiration structures aren't analyzable this way and should skip
    calling this function). *net_price* is the entry price per combo unit in
    Alpaca mleg convention (positive debit, negative credit); it may be a
    chain-mid quote (pre-trade estimate) or an actual fill price (post-trade
    correction) — the payoff math is identical either way, only the anchor
    price differs. The payoff of a piecewise-linear combo attains its extrema
    at the strike kinks, at S=0, and in the tails; tail slopes decide
    unboundedness. Either return value is None when that side is unbounded or
    not a real risk/reward (e.g. zero).
    """
    # Tail slopes: calls drive the S→∞ slope; puts drive the S→0 slope
    # (payoff slope in S is -Σ sign×ratio over puts below all strikes).
    call_slope = sum(
        (1.0 if leg.side == "buy" else -1.0) * leg.ratio
        for leg in legs
        if leg.right == "call"
    )

    strikes = sorted({leg.strike for leg in legs})
    eval_points = [0.0, *strikes, strikes[-1] * 2 + 1.0]
    pnl_values = [
        (_payoff_per_share(legs, s) - net_price) * _OPTION_MULTIPLIER
        for s in eval_points
    ]

    min_pnl = min(pnl_values)
    max_pnl = max(pnl_values)

    est_max_loss: float | None = None
    est_max_profit: float | None = None

    # call_slope < 0 → unbounded loss to the upside. The naked-short check
    # rejects that structure anyway; leave est_max_loss None so the
    # fallback value never masks it.
    if call_slope >= 0:
        computed_loss = -min_pnl
        if computed_loss > 0:
            est_max_loss = round(computed_loss, 2)
    if call_slope <= 0:
        computed_profit = max_pnl
        if computed_profit > 0:
            est_max_profit = round(computed_profit, 2)
    return est_max_loss, est_max_profit


def compute_structure_metrics(
    legs: list[Leg],
    chain: FilteredChain,
) -> StructureMetrics | None:
    """Compute metrics for *legs* from *chain* quotes; None if any leg is absent.

    A None return means the chain does not cover every leg — the liquidity
    check in validate_market_access fails closed on the same condition, so
    callers never execute a proposal for which this returned None.

    Max loss / max profit use expiration-payoff analysis (compute_payoff_bounds),
    which is exact for same-expiration structures (all playbook strategies).
    Entry price is the combo mid — the actual fill will differ by at most the
    configured offset plus slippage, which is negligible against the
    1%-of-equity risk budget for sizing purposes, but is corrected against the
    real fill at fill-confirmation time (see recompute_fill_metrics).
    """
    contracts: list[OptionContract] = []
    for leg in legs:
        contract = _lookup(chain, leg)
        if contract is None:
            logger.warning(
                "compute_structure_metrics: %s %s %.2f exp %s not in chain for %s",
                leg.side,
                leg.right,
                leg.strike,
                leg.expiration,
                chain.underlying,
            )
            return None
        contracts.append(contract)

    net_entry_mid = 0.0
    net_delta = 0.0
    net_theta = 0.0
    net_vega = 0.0
    leg_quotes: list[tuple[float, float]] = []
    for leg, contract in zip(legs, contracts):
        sign = 1.0 if leg.side == "buy" else -1.0
        net_entry_mid += sign * contract.mid * leg.ratio
        net_delta += sign * contract.delta * leg.ratio
        net_theta += sign * contract.theta * leg.ratio
        net_vega += sign * contract.vega * leg.ratio
        leg_quotes.append((contract.bid, contract.ask))

    est_max_loss: float | None = None
    est_max_profit: float | None = None

    expirations = {leg.expiration for leg in legs}
    if len(expirations) == 1:
        est_max_loss, est_max_profit = compute_payoff_bounds(legs, net_entry_mid)
    else:
        logger.info(
            "compute_structure_metrics: mixed expirations %s — payoff analysis "
            "skipped; agent-supplied max loss/profit retained",
            sorted(e.isoformat() for e in expirations),
        )

    return StructureMetrics(
        net_entry_mid=round(net_entry_mid, 4),
        est_max_loss=est_max_loss,
        est_max_profit=est_max_profit,
        net_delta=round(net_delta, 4),
        net_theta=round(net_theta, 4),
        net_vega=round(net_vega, 4),
        leg_quotes=leg_quotes,
    )


def apply_structure_metrics(
    proposal_updates: dict[str, float],
    metrics: StructureMetrics,
    *,
    agent_est_max_loss: float,
    agent_est_max_profit: float,
    log_context: str,
) -> dict[str, float]:
    """Fill *proposal_updates* with computed metrics, logging large deviations.

    Greeks are always overridden. est_max_loss / est_max_profit are overridden
    only when the payoff analysis produced a finite positive bound; otherwise
    the agent's values are retained (and the caller's validators still check
    them for finiteness).
    """
    proposal_updates["net_delta"] = metrics.net_delta
    proposal_updates["net_theta"] = metrics.net_theta
    proposal_updates["net_vega"] = metrics.net_vega

    if metrics.est_max_loss is not None:
        _log_deviation(
            "est_max_loss", agent_est_max_loss, metrics.est_max_loss, log_context
        )
        proposal_updates["est_max_loss"] = metrics.est_max_loss
    if metrics.est_max_profit is not None:
        _log_deviation(
            "est_max_profit", agent_est_max_profit, metrics.est_max_profit, log_context
        )
        proposal_updates["est_max_profit"] = metrics.est_max_profit
    return proposal_updates


def recompute_fill_metrics(
    legs: list[Leg], net_fill_price: float
) -> tuple[float | None, float | None]:
    """(est_max_loss, est_max_profit) from the actual fill, not the chain mid.

    Same payoff analysis as compute_structure_metrics, driven by the
    confirmed net_fill_price instead of a pre-trade quote. Used at fill
    confirmation (WP-1) to correct Position.est_max_loss/profit for the
    difference between the proposal's estimate and the real execution price.
    Mixed-expiration structures return (None, None) — same fallback contract
    as compute_structure_metrics.
    """
    if len({leg.expiration for leg in legs}) != 1:
        return None, None
    return compute_payoff_bounds(legs, net_fill_price)


def apply_fill_metrics(
    legs: list[Leg],
    net_fill_price: float,
    *,
    prior_est_max_loss: float,
    prior_est_max_profit: float,
    log_context: str,
) -> tuple[float, float]:
    """(est_max_loss, est_max_profit) to store on Position after a fill.

    Recomputes from the actual fill price; falls back to
    *prior_est_max_loss*/*prior_est_max_profit* (the pre-fill, chain-mid-based
    values already on the proposal/Position) when payoff analysis doesn't
    apply. Logs a warning when the recomputed value deviates materially from
    the prior estimate, via the same threshold as the pre-trade enrichment
    step (_log_deviation).
    """
    est_max_loss, est_max_profit = recompute_fill_metrics(legs, net_fill_price)

    if est_max_loss is not None:
        _log_deviation("est_max_loss", prior_est_max_loss, est_max_loss, log_context)
    if est_max_profit is not None:
        _log_deviation(
            "est_max_profit", prior_est_max_profit, est_max_profit, log_context
        )

    return (
        est_max_loss if est_max_loss is not None else prior_est_max_loss,
        est_max_profit if est_max_profit is not None else prior_est_max_profit,
    )


def _log_deviation(
    field: str, agent_value: float, computed_value: float, log_context: str
) -> None:
    """Log when the agent's self-reported value diverges >20% from computed."""
    if computed_value <= 0:
        return
    deviation = abs(agent_value - computed_value) / computed_value
    if deviation > 0.20:
        logger.warning(
            "%s: agent-reported %s %.2f deviates %.0f%% from computed %.2f "
            "(computed value used)",
            log_context,
            field,
            agent_value,
            deviation * 100,
            computed_value,
        )
