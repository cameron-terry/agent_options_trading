"""Underlying price fetch and VIX/regime classification (WP-3.6).

Public API:
    get_universe_snapshot(symbols, provider, vol_provider, playbook, ...)
        -> UniverseSnapshot

Populates the market-level and per-symbol fields of UniverseSnapshot
that this module owns:
  - UniverseSnapshot.vix_level     — raw VIX level from VolatilityIndexProvider
  - UniverseSnapshot.market_regime — MarketRegime enum from PlaybookConfig
  - UniverseSnapshot.macro_events  — FOMC/CPI/NFP events from data/events.py
  - SymbolSnapshot.price           — latest bar close from DataProvider
  - SymbolSnapshot.regime          — echoes market_regime (v1; see note below)

Fields NOT populated here (owned by other WPs):
  - SymbolSnapshot.iv_rank           — WP-3.4 (IV-rank/percentile storage)
  - SymbolSnapshot.iv_percentile     — WP-3.4
  - SymbolSnapshot.historical_vol    — WP-3.4
  - SymbolSnapshot.days_to_earnings  — WP-6 assembler (derives from EventInfo)

Per-symbol regime (v1 semantic): SymbolSnapshot.regime echoes market_regime
for every symbol rather than computing a per-symbol classification from the
ticker's own HV/IV data. This gives WP-6 a consistently populated field
without WP-3.4 dependency. The field is intentionally not an independent
per-symbol signal in v1 — do not treat it as such in the playbook. Upgrade
to per-symbol classification only if journal evidence shows it adds signal
beyond iv_rank and historical_vol, which already carry per-name vol context.

Symbols that fail price fetch are excluded from symbol_snapshots with a
WARNING. The caller (WP-8 assembler) should log excluded symbols; WP-4
treats absent symbols as not tradeable this cycle.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from options_agent.config import PlaybookConfig
from options_agent.contracts.data import MarketRegime, SymbolSnapshot, UniverseSnapshot
from options_agent.data.events import get_macro_events
from options_agent.data.providers import (
    DataAuthError,
    DataProvider,
    DataUnavailableError,
)
from options_agent.data.providers.volatility_provider import VolatilityIndexProvider

logger = logging.getLogger(__name__)


def get_universe_snapshot(
    symbols: list[str],
    provider: DataProvider,
    vol_provider: VolatilityIndexProvider,
    playbook: PlaybookConfig,
    macro_lookahead_days: int = 60,
    as_of: datetime | None = None,
) -> UniverseSnapshot:
    """Build UniverseSnapshot for the given symbol universe.

    Fetches underlying prices and VIX in sequence (DataProvider's sequential-
    execution invariant — see providers/__init__.py). Symbols that fail price
    fetch are excluded from symbol_snapshots with a WARNING rather than
    aborting the cycle.

    If VIX is unavailable (vol_provider returns available=False), market_regime
    and all per-symbol regime values are set to MarketRegime.UNKNOWN and the
    cycle continues with degraded regime context. vix_level is set to 0.0 as
    a sentinel — consumers should check market_regime for UNKNOWN rather than
    relying on a specific vix_level value.

    Args:
        symbols: Ordered list of universe tickers to include.
        provider: DataProvider for per-symbol price fetches.
        vol_provider: VolatilityIndexProvider for VIX.
        playbook: PlaybookConfig supplying VIX classification thresholds.
        macro_lookahead_days: Forward window (days) for macro event fetch.
        as_of: Timestamp to stamp on the snapshot; defaults to now(UTC).
    """
    now = as_of or datetime.now(UTC)

    # ── 1. VIX level + market regime ─────────────────────────────────────────
    vix_result = vol_provider.fetch_vix()
    if vix_result.available and vix_result.level is not None:
        vix_level = vix_result.level
        market_regime = playbook.regime_label(vix_level)
    else:
        logger.warning(
            "VIX fetch unavailable — setting market_regime=UNKNOWN for this cycle"
        )
        vix_level = 0.0
        market_regime = MarketRegime.UNKNOWN

    # ── 2. Per-symbol price fetch ─────────────────────────────────────────────
    symbol_snapshots: dict[str, SymbolSnapshot] = {}
    for symbol in symbols:
        try:
            price = provider.fetch_latest_price(symbol)
        except (DataUnavailableError, DataAuthError):
            logger.warning(
                "Price fetch failed for %s — excluding from snapshot this cycle",
                symbol,
            )
            continue

        symbol_snapshots[symbol] = SymbolSnapshot(
            symbol=symbol,
            price=price,
            # iv_rank, iv_percentile, historical_vol: populated by WP-3.4 when
            # integrated. None here signals warm-up / unavailable to WP-4.
            iv_rank=None,
            iv_percentile=None,
            historical_vol=None,
            # v1: echo market regime. Not an independent per-symbol signal.
            regime=market_regime if market_regime != MarketRegime.UNKNOWN else None,
            # days_to_earnings: derived by WP-6 assembler from EventInfo.
            days_to_earnings=None,
        )

    # ── 3. Macro events ───────────────────────────────────────────────────────
    macro_events = get_macro_events(
        lookahead_days=macro_lookahead_days,
        as_of=now.date(),
    )

    return UniverseSnapshot(
        symbol_snapshots=symbol_snapshots,
        vix_level=vix_level,
        market_regime=market_regime,
        macro_events=macro_events,
        as_of=now,
    )
