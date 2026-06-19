"""Chain pre-filter: compacts a raw option chain into a token-budget-aware
FilteredChain.

Public API:
    get_filtered_chain(symbol, provider, limits, strategy_hint=None, *, as_of=None)
        -> FilteredChain

Filter pipeline (in order):
  1. DTE window — keep contracts within [limits.min_dte, limits.max_dte].
  2. Missing bid/ask — exclude contracts where quote data is absent.
  3. Missing Greeks/IV — exclude contracts where delta, theta, vega, or iv is
     None; count each in excluded_for_missing_greeks.
  4. Delta range — keep |delta| in [limits.min_abs_delta, limits.max_abs_delta].
  5. Strategy-hint right filter — restrict to calls, puts, or both depending on
     the hint (see _STRATEGY_RIGHT_MAP).
  6. Spread filter — pass if spread ≤ pct_of_mid * mid  OR  spread ≤ abs_floor.
     Guard: if mid ≤ _MID_EPSILON the percentage test is skipped.
  7. OI filter — applied only when oi_available (provider returned non-None OI).
  8. Relevance sort — primary: proximity to delta window centre; secondary: spread.
  9. Per-right cap — at most max_contracts_per_chain // n_rights contracts per
     right, so neither wing is starved for two-sided strategies.

All exclusions are recorded in FilteredChain metadata so the journal can answer
"why did the agent only see these contracts?" without re-fetching.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from options_agent.contracts.data import (
    ChainFilterParams,
    FilteredChain,
    OptionContract,
)
from options_agent.contracts.state import Position
from options_agent.data.greeks_iv import enrich_greeks_iv
from options_agent.data.providers import (
    DataAuthError,
    DataProvider,
    DataUnavailableError,
)
from options_agent.risk.limits import ChainFilterLimits

logger = logging.getLogger(__name__)

# Minimum mid price below which relative spread % is meaningless (division guard).
_MID_EPSILON = 0.01

# Map from strategy name → set of option rights to include.
# Strategies not present here are treated as None (both rights, default window).
_STRATEGY_RIGHT_MAP: dict[str, frozenset[Literal["call", "put"]]] = {
    "bull_put_spread": frozenset({"put"}),
    "bear_put_spread": frozenset({"put"}),
    "cash_secured_put": frozenset({"put"}),
    "bear_call_spread": frozenset({"call"}),
    "bull_call_spread": frozenset({"call"}),
    "covered_call": frozenset({"call"}),
    "iron_condor": frozenset({"call", "put"}),
    "iron_butterfly": frozenset({"call", "put"}),
}
_DEFAULT_RIGHTS: frozenset[Literal["call", "put"]] = frozenset({"call", "put"})


def get_filtered_chain(
    symbol: str,
    provider: DataProvider,
    limits: ChainFilterLimits,
    strategy_hint: str | None = None,
    *,
    as_of: datetime | None = None,
) -> FilteredChain:
    """Return a compact, liquidity-filtered FilteredChain for one underlying.

    Fetch raw contracts from provider, apply the full filter pipeline, sort by
    relevance, cap by limits.max_contracts_per_chain, and embed all metadata.

    Args:
        symbol: Underlying ticker (e.g. "SPY").
        provider: DataProvider to fetch the raw chain and underlying price from.
        limits: ChainFilterLimits with all threshold values.
        strategy_hint: Optional strategy name from the allowed playbook. Controls
            which rights (calls, puts, or both) are included. Unknown hints are
            treated as None with a logged warning.
        as_of: Timestamp used for DTE computation. Defaults to UTC now.
    """
    if as_of is None:
        as_of = datetime.now(UTC)

    raw_contracts = enrich_greeks_iv(provider.fetch_option_chain(symbol))
    underlying_price = provider.fetch_latest_price(symbol)

    # Determine which rights to include.
    allowed_rights: frozenset[Literal["call", "put"]]
    if strategy_hint is None:
        allowed_rights = _DEFAULT_RIGHTS
    elif strategy_hint in _STRATEGY_RIGHT_MAP:
        allowed_rights = _STRATEGY_RIGHT_MAP[strategy_hint]
    else:
        logger.warning(
            "get_filtered_chain(%r): unknown strategy_hint %r — treating as None"
            " (both rights, default delta window).",
            symbol,
            strategy_hint,
        )
        allowed_rights = _DEFAULT_RIGHTS

    oi_available = any(c.open_interest is not None for c in raw_contracts)
    delta_centre = (limits.min_abs_delta + limits.max_abs_delta) / 2.0

    # Per-right cap: split evenly across included rights.
    n_rights = len(allowed_rights)
    cap_per_right = limits.max_contracts_per_chain // n_rights

    excluded_for_missing_greeks = 0
    by_right: dict[str, list[OptionContract]] = {r: [] for r in allowed_rights}

    for raw in raw_contracts:
        right = raw.right
        if right not in allowed_rights:
            continue

        # DTE filter.
        dte = (raw.expiration - as_of.date()).days
        if not (limits.min_dte <= dte <= limits.max_dte):
            continue

        # Quote completeness — mid and spread require both bid and ask.
        if raw.bid is None or raw.ask is None:
            continue

        # Greeks/IV completeness.
        if (
            raw.delta is None
            or raw.theta is None
            or raw.vega is None
            or raw.implied_volatility is None
        ):
            excluded_for_missing_greeks += 1
            continue

        # Delta range filter (uses absolute value; puts have negative delta).
        abs_delta = abs(raw.delta)
        if not (limits.min_abs_delta <= abs_delta <= limits.max_abs_delta):
            continue

        mid = (raw.bid + raw.ask) / 2.0
        spread_width = raw.ask - raw.bid

        # Spread filter: pass if either the relative OR the absolute rule passes.
        if mid > _MID_EPSILON:
            spread_pct_limit = limits.max_spread_pct_of_mid * mid
            spread_ok = (
                spread_width <= spread_pct_limit
                or spread_width <= limits.max_spread_abs_floor
            )
        else:
            # Near-zero mid: relative test meaningless; fall through to abs floor only.
            spread_ok = spread_width <= limits.max_spread_abs_floor

        if not spread_ok:
            continue

        # OI filter — only when the provider returned OI data.
        # When oi_available=True, a contract with None OI is treated as failing
        # the threshold (excluded), not bypassing it. This upholds the invariant
        # from RawOptionContract: "a contract with None OI must not be silently
        # passed through a min_oi filter."
        if oi_available and (
            raw.open_interest is None or raw.open_interest < limits.min_open_interest
        ):
            continue

        by_right[right].append(
            OptionContract(
                symbol=raw.symbol,
                strike=raw.strike,
                expiration=raw.expiration,
                right=right,
                bid=raw.bid,
                ask=raw.ask,
                mid=mid,
                volume=raw.volume,
                open_interest=raw.open_interest,
                delta=raw.delta,
                theta=raw.theta,
                vega=raw.vega,
                iv=raw.implied_volatility,
                spread_width=spread_width,
                dte=dte,
                greek_source=raw.greek_source,
            )
        )

    if excluded_for_missing_greeks > 0:
        logger.warning(
            "get_filtered_chain(%r): excluded %d contract(s) with missing"
            " Greek/IV data.",
            symbol,
            excluded_for_missing_greeks,
        )

    # Sort each right's contracts by relevance (delta proximity to window centre)
    # then by spread width (tighter spread preferred as tiebreaker).
    for right, contracts in by_right.items():
        contracts.sort(key=lambda c: (abs(abs(c.delta) - delta_centre), c.spread_width))

    total_before_cap = sum(len(v) for v in by_right.values())
    truncated = any(len(v) > cap_per_right for v in by_right.values())

    final_contracts: list[OptionContract] = []
    for contracts in by_right.values():
        final_contracts.extend(contracts[:cap_per_right])

    if not oi_available:
        logger.info(
            "get_filtered_chain(%r): OI unavailable from provider — min_open_interest"
            " threshold was not applied. Chain flagged oi_available=False.",
            symbol,
        )

    filter_params = ChainFilterParams(
        dte_min=limits.min_dte,
        dte_max=limits.max_dte,
        delta_min=limits.min_abs_delta,
        delta_max=limits.max_abs_delta,
        min_open_interest=limits.min_open_interest,
        max_spread_pct_of_mid=limits.max_spread_pct_of_mid,
        max_spread_abs_floor=limits.max_spread_abs_floor,
    )

    return FilteredChain(
        underlying=symbol,
        underlying_price=underlying_price,
        as_of=as_of,
        filter_params=filter_params,
        contracts=final_contracts,
        strategy_hint=strategy_hint,
        oi_available=oi_available,
        excluded_for_missing_greeks=excluded_for_missing_greeks,
        truncated=truncated,
        total_before_cap=total_before_cap,
    )


# (underlying, right, strike, expiration_isoformat)
type LegKey = tuple[str, str, float, str]
# (delta, vega, theta) — same order as the greek_lookup tuple in portfolio.py
type LegGreeks = tuple[float, float, float]


def get_held_leg_greeks(
    positions: list[Position],
    provider: DataProvider,
) -> dict[LegKey, LegGreeks]:
    """Fetch current Greeks for held position legs without entry-criteria filters.

    Unlike get_filtered_chain(), no DTE window or delta range is applied — the
    full provider chain for each underlying is fetched and only Greek plausibility
    checks (enrich_greeks_iv) are run. This closes the gap where a held leg that
    has aged below dte_min would be absent from the entry chain and silently
    contribute 0.0 to portfolio Greek aggregation.

    Returns a dict keyed by (underlying, right, strike, expiration_isoformat)
    mapping to (delta, vega, theta). Contracts absent from the provider snapshot
    (e.g. expired options no longer quoted) are simply omitted; callers fall back
    to 0.0 + warning when a key is not present.

    Only fetches for underlyings that have at least one option leg in positions.
    EQUITY positions (empty legs list) are skipped automatically.
    """
    underlyings = {pos.underlying for pos in positions if pos.legs}

    result: dict[LegKey, LegGreeks] = {}

    for underlying in underlyings:
        try:
            raw_contracts = enrich_greeks_iv(provider.fetch_option_chain(underlying))
        except (DataUnavailableError, DataAuthError) as exc:
            logger.warning(
                "get_held_leg_greeks(%r): chain fetch failed (%s) —"
                " legs for this underlying will not have held-leg Greek fallback.",
                underlying,
                exc,
            )
            continue

        for raw in raw_contracts:
            if raw.delta is None or raw.vega is None or raw.theta is None:
                continue
            key: LegKey = (
                underlying,
                raw.right,
                raw.strike,
                raw.expiration.isoformat(),
            )
            result[key] = (raw.delta, raw.vega, raw.theta)

    return result
