"""Portfolio Greek aggregation (WP-6.2).

Aggregates net portfolio dollar-Greeks from open positions using the current
filtered chain for each underlying.

Cross-WP note (WP-3 dependency): the entry-filtered chain is bounded by
dte_min/dte_max and the configured delta range. Contracts for positions entered
earlier in their lifecycle (e.g. a 45-DTE leg now at 10 DTE) may fall outside
those bounds and will not appear in the chain. When a held leg is absent from
the chain its Greek contribution is treated as 0.0 and a warning is emitted.
This understates portfolio exposure and may make the net-delta band check less
conservative than intended. WP-3 should expose a separate held-leg Greek fetch
(unfiltered by entry criteria) to close this gap.

net_dollar_gamma is not computed here because OptionContract intentionally omits
per-contract gamma (not meaningful as a per-row entry signal; see contracts/data.py
OptionContract docstring). The field is set to 0.0 until a separate gamma source
is available.

Greek unit convention (confirmed WP-4.4; matches Limits docstring):
  dollar_delta = Σ (delta × side_sign × filled_qty × underlying_price × 100)
  dollar_vega  = Σ (vega  × side_sign × filled_qty × 100)   # per 1 vol-point
  dollar_theta = Σ (theta × side_sign × filled_qty × 100)   # per calendar day

filled_qty (from PositionLeg) is the actual number of contracts filled for each
leg and equals ratio × position.quantity for fully-filled positions. Using
filled_qty directly handles the ratio without needing a separate multiplication,
and also correctly reflects partial-fill state if a position were ever seen
before full completion (though WP-1 reconcile should only produce OPEN positions
once fully filled).

side_sign is +1 for "buy" legs and -1 for "sell" legs, reflecting that selling
a put (negative BSM delta) creates a positive portfolio delta exposure.
"""

from options_agent.contracts.data import FilteredChain, PortfolioState


def aggregate_portfolio_greeks(
    portfolio_raw: PortfolioState,
    chains_by_symbol: dict[str, FilteredChain],
) -> tuple[PortfolioState, list[str]]:
    """Recompute net dollar-Greeks on *portfolio_raw* from current chain data.

    Returns a new PortfolioState with net_dollar_delta/vega/theta overwritten by
    values computed from *chains_by_symbol*, plus a list of warning strings for
    any position legs that could not be matched in the supplied chains.

    net_dollar_gamma is always set to 0.0 (see module docstring).
    The account-level fields (equity, buying_power, etc.) and positions list
    are carried through unchanged from *portfolio_raw*.
    """
    warnings: list[str] = []
    net_dollar_delta = 0.0
    net_dollar_vega = 0.0
    net_dollar_theta = 0.0

    for position in portfolio_raw.positions:
        chain = chains_by_symbol.get(position.underlying)
        if chain is None:
            warnings.append(
                f"pos {position.id} ({position.underlying}): no chain supplied;"
                " all Greek contributions set to 0.0"
            )
            continue

        # Build O(1) lookup keyed by (right, strike, iso_expiration).
        # Float strike comparison is safe — strikes are exchange-defined round
        # numbers (e.g. 530.0) that come from the same provider data source.
        greek_lookup: dict[tuple[str, float, str], tuple[float, float, float]] = {}
        for contract in chain.contracts:
            key = (contract.right, contract.strike, contract.expiration.isoformat())
            greek_lookup[key] = (contract.delta, contract.vega, contract.theta)

        underlying_price = chain.underlying_price

        for pos_leg in position.legs:
            leg = pos_leg.leg
            key = (leg.right, leg.strike, leg.expiration.isoformat())
            side_sign = 1.0 if leg.side == "buy" else -1.0
            filled_qty = pos_leg.filled_qty

            greeks = greek_lookup.get(key)
            if greeks is None:
                warnings.append(
                    f"pos {position.id}: {leg.side} {leg.right} {leg.strike}"
                    f" exp {leg.expiration} not in chain (may be outside entry"
                    " filter window); Greek contribution set to 0.0"
                )
                continue

            delta, vega, theta = greeks
            net_dollar_delta += delta * side_sign * filled_qty * underlying_price * 100
            net_dollar_vega += vega * side_sign * filled_qty * 100
            net_dollar_theta += theta * side_sign * filled_qty * 100

    return (
        portfolio_raw.model_copy(
            update={
                "net_dollar_delta": round(net_dollar_delta, 2),
                "net_dollar_vega": round(net_dollar_vega, 2),
                "net_dollar_theta": round(net_dollar_theta, 2),
                "net_dollar_gamma": 0.0,
            }
        ),
        warnings,
    )
