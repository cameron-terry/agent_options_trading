"""Greek and IV validation layer for raw option chain data.

Public API:
    enrich_greeks_iv(contracts) -> list[RawOptionContract]
    get_atm_iv(contracts, spot_price, target_dte=30) -> float | None

enrich_greeks_iv validates Greek and IV fields from the provider, coercing
implausible or non-finite values to None with warnings. Designed to run on the
output of DataProvider.fetch_option_chain() before chains.py applies
filter/compaction.

get_atm_iv extracts a single daily ATM IV scalar from an enriched chain (the
output of enrich_greeks_iv). Used by data/iv_rank.py to build the rolling
iv_history needed for IV-rank and IV-percentile computation (WP-3.4).

"ATM IV" definition (must be applied consistently to every stored observation
and to the live current_iv passed to compute_iv_rank / compute_iv_percentile):
  - Expiration: nearest to target_dte (default 30) calendar days from today,
    among expirations still in the future.
  - Contract: call with delta closest to 0.5. Fallback (no delta): call with
    strike closest to spot_price.

Sign conventions (Alpaca uses long-option perspective for all contracts):
  - delta:  calls ∈ (0, 1], puts ∈ [-1, 0)
  - gamma:  ≥ 0 for both calls and puts
  - theta:  ≤ 0 (time decay is a cost, so negative)
  - vega:   ≥ 0 for both calls and puts

Plausibility rules — violation coerces the affected field to None:
  - |delta| > 1.0         — impossible by definition
  - gamma < 0             — artifact; Alpaca's sign convention never produces this
  - theta > 0             — artifact; Alpaca's sign convention never produces this
  - vega < 0              — artifact; Alpaca's sign convention never produces this
  - IV ≤ 0 or IV > 5.0   — zero/negative impossible; > 500% is almost always a
                            data artifact (real meme/earnings IV rarely exceeds ~300%)
  - Non-finite (NaN, Inf) — on any Greek or IV field

Coercion policy: implausible → None, not merely logged. A retained bad value
corrupts portfolio Greek aggregation and passes downstream plausibility filters.
None feeds the existing missing-Greeks exclusion path in chains.py.

greek_source is set to "alpaca" on all returned contracts. Feed sub-qualifier
(opra vs indicative) is not tracked yet — AlpacaDataClient does not expose which
feed was used. Add once AlpacaDataClient surfaces feed provenance.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import date

from options_agent.data.providers import RawOptionContract

logger = logging.getLogger(__name__)

_IV_MAX = 5.0  # 500% cap — real IV above this is almost always a data artifact


def enrich_greeks_iv(contracts: list[RawOptionContract]) -> list[RawOptionContract]:
    """Validate and plausibility-check Greeks/IV on each contract.

    Returns a new list of RawOptionContract objects with implausible or
    non-finite values coerced to None and greek_source set to "alpaca".
    """
    return [_validate_contract(c) for c in contracts]


def _validate_contract(raw: RawOptionContract) -> RawOptionContract:
    sym = raw.symbol

    delta = _check(raw.delta, sym, "delta", lambda v: abs(v) > 1.0, "|delta| > 1")
    gamma = _check(raw.gamma, sym, "gamma", lambda v: v < 0.0, "gamma < 0")
    theta = _check(raw.theta, sym, "theta", lambda v: v > 0.0, "theta > 0")
    vega = _check(raw.vega, sym, "vega", lambda v: v < 0.0, "vega < 0")
    iv = _check(
        raw.implied_volatility,
        sym,
        "implied_volatility",
        lambda v: v <= 0.0 or v > _IV_MAX,
        f"IV not in (0, {_IV_MAX}]",
    )

    # Alpaca derives IV and core Greeks together from the same pricing model;
    # one present without the other signals a provider inconsistency. Check
    # raw (pre-coercion) values so local plausibility failures don't create
    # spurious inconsistency warnings for a self-consistent provider response.
    raw_has_greeks = any(
        g is not None for g in (raw.delta, raw.gamma, raw.theta, raw.vega)
    )
    raw_has_iv = raw.implied_volatility is not None
    if raw_has_iv != raw_has_greeks:
        logger.warning(
            "greeks_iv: %s — IV %s but core Greeks %s; provider inconsistency.",
            sym,
            "present" if raw_has_iv else "absent",
            "present" if raw_has_greeks else "absent",
        )

    return raw.model_copy(
        update={
            "delta": delta,
            "gamma": gamma,
            "theta": theta,
            "vega": vega,
            "implied_volatility": iv,
            "greek_source": "alpaca",
        }
    )


def _check(
    value: float | None,
    symbol: str,
    field: str,
    is_implausible: Callable[[float], bool],
    reason: str,
) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value):
        logger.warning(
            "greeks_iv: %s.%s=%r is non-finite — coercing to None.",
            symbol,
            field,
            value,
        )
        return None
    if is_implausible(value):
        logger.warning(
            "greeks_iv: %s.%s=%r fails plausibility check (%s) — coercing to None.",
            symbol,
            field,
            value,
            reason,
        )
        return None
    return value


def get_atm_iv(
    contracts: list[RawOptionContract],
    spot_price: float,
    target_dte: int = 30,
) -> float | None:
    """Extract the ATM IV scalar from an enriched option chain.

    Selects the nearest-to-target_dte expiration (future expirations only),
    then picks the call whose delta is closest to 0.5. Falls back to the call
    with strike closest to spot_price when no delta is available.

    Call enrich_greeks_iv() on the raw chain before passing it here so that
    implausible or non-finite IV values are already coerced to None.

    Returns None if no call contracts with valid IV exist on any future
    expiration.
    """
    today = date.today()

    # Calls with valid IV on a future expiration.
    candidates = [
        c
        for c in contracts
        if c.right == "call"
        and c.implied_volatility is not None
        and (c.expiration - today).days > 0
    ]
    if not candidates:
        logger.debug("get_atm_iv: no valid call contracts with future expiry.")
        return None

    # Nearest-to-target_dte expiration.
    target_exp = min(
        {c.expiration for c in candidates},
        key=lambda exp: abs((exp - today).days - target_dte),
    )
    at_exp = [c for c in candidates if c.expiration == target_exp]

    # ATM selection: prefer delta closest to 0.5; fallback to strike proximity.
    with_delta = [c for c in at_exp if c.delta is not None]
    if with_delta:
        atm = min(
            with_delta,
            key=lambda c: abs((c.delta if c.delta is not None else 0.0) - 0.5),
        )
    else:
        atm = min(at_exp, key=lambda c: abs(c.strike - spot_price))

    return atm.implied_volatility
