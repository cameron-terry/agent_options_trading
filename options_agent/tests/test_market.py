"""Tests for data/market.py — get_universe_snapshot() and MarketRegime.

Coverage:
  - MarketRegime enum values and str-subclass behaviour
  - PlaybookConfig.regime_label() returns correct MarketRegime for each VIX bucket
  - VixFetchResult construction and semantics
  - YFinanceVolatilityProvider protocol conformance (no live network calls)
  - get_universe_snapshot():
      - normal path: VIX available, all prices succeed
      - regime thresholds (low_vol / normal / high_vol)
      - per-symbol regime echoes market_regime
      - VIX unavailable → market_regime=UNKNOWN, per-symbol regime=None
      - symbol price fetch failure → symbol excluded from snapshot
      - macro events forwarded from get_macro_events()
      - as_of override honoured
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import pytest

from options_agent.config import PlaybookConfig
from options_agent.contracts.data import (
    MacroEvent,
    MarketRegime,
    SymbolSnapshot,
    UniverseSnapshot,
)
from options_agent.data.market import get_universe_snapshot
from options_agent.data.providers import DataUnavailableError
from options_agent.data.providers.volatility_provider import (
    VixFetchResult,
    VolatilityIndexProvider,
)
from options_agent.data.providers.yfinance_volatility_provider import (
    YFinanceVolatilityProvider,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 6, 19, 14, 30, tzinfo=UTC)
_PLAYBOOK = PlaybookConfig()  # defaults: low=15, high=25

_YF_PATCH = "options_agent.data.providers.yfinance_volatility_provider.yf"


def _make_vol_provider(level: float | None, available: bool = True) -> MagicMock:
    """Return a mock VolatilityIndexProvider returning the given VIX level."""
    provider = MagicMock(spec=VolatilityIndexProvider)
    provider.fetch_vix.return_value = VixFetchResult(level=level, available=available)
    return provider


def _make_data_provider(prices: dict[str, float]) -> MagicMock:
    """Return a mock DataProvider returning per-symbol prices."""
    provider = MagicMock()
    provider.fetch_latest_price.side_effect = lambda sym: prices[sym]
    return provider


# ---------------------------------------------------------------------------
# MarketRegime enum
# ---------------------------------------------------------------------------


def test_market_regime_values() -> None:
    assert MarketRegime.LOW_VOL == "low_vol"
    assert MarketRegime.NORMAL == "normal"
    assert MarketRegime.HIGH_VOL == "high_vol"
    assert MarketRegime.UNKNOWN == "unknown"


def test_market_regime_is_str_subclass() -> None:
    assert isinstance(MarketRegime.NORMAL, str)


def test_market_regime_round_trips_through_symbol_snapshot() -> None:
    snap = SymbolSnapshot(
        symbol="SPY",
        price=500.0,
        iv_rank=None,
        iv_percentile=None,
        historical_vol=None,
        regime=MarketRegime.NORMAL,
        days_to_earnings=None,
    )
    restored = SymbolSnapshot.model_validate(snap.model_dump())
    assert restored.regime == MarketRegime.NORMAL


def test_market_regime_round_trips_through_universe_snapshot() -> None:
    snap = SymbolSnapshot(
        symbol="SPY",
        price=500.0,
        iv_rank=None,
        iv_percentile=None,
        historical_vol=None,
        regime=MarketRegime.NORMAL,
        days_to_earnings=None,
    )
    u = UniverseSnapshot(
        symbol_snapshots={"SPY": snap},
        vix_level=20.0,
        market_regime=MarketRegime.NORMAL,
        macro_events=[],
        as_of=_AS_OF,
    )
    restored = UniverseSnapshot.model_validate(u.model_dump())
    assert restored.market_regime == MarketRegime.NORMAL
    assert restored.symbol_snapshots["SPY"].regime == MarketRegime.NORMAL


# ---------------------------------------------------------------------------
# PlaybookConfig.regime_label
# ---------------------------------------------------------------------------


def test_regime_label_low_vol_bucket() -> None:
    # VIX strictly below low threshold (15.0 default)
    assert _PLAYBOOK.regime_label(10.0) == MarketRegime.LOW_VOL


def test_regime_label_normal_bucket() -> None:
    assert _PLAYBOOK.regime_label(20.0) == MarketRegime.NORMAL


def test_regime_label_high_vol_bucket() -> None:
    # VIX strictly above high threshold (25.0 default)
    assert _PLAYBOOK.regime_label(30.0) == MarketRegime.HIGH_VOL


def test_regime_label_exactly_at_low_threshold_is_normal() -> None:
    # vix == low_threshold → normal (low_vol is strictly <, not ≤)
    result = _PLAYBOOK.regime_label(_PLAYBOOK.vix_low_vol_threshold)
    assert result == MarketRegime.NORMAL


def test_regime_label_exactly_at_high_threshold_is_normal() -> None:
    # vix == high_threshold → normal (high_vol is strictly >)
    result = _PLAYBOOK.regime_label(_PLAYBOOK.vix_high_vol_threshold)
    assert result == MarketRegime.NORMAL


def test_regime_label_none_vix_is_unknown() -> None:
    assert _PLAYBOOK.regime_label(None) == MarketRegime.UNKNOWN


# ---------------------------------------------------------------------------
# VixFetchResult
# ---------------------------------------------------------------------------


def test_vix_fetch_result_available() -> None:
    r = VixFetchResult(level=18.5, available=True)
    assert r.level == 18.5
    assert r.available is True


def test_vix_fetch_result_unavailable() -> None:
    r = VixFetchResult(level=None, available=False)
    assert r.level is None
    assert r.available is False


# ---------------------------------------------------------------------------
# YFinanceVolatilityProvider — protocol conformance (no live calls)
# ---------------------------------------------------------------------------


def test_yfinance_vol_provider_implements_protocol() -> None:
    provider = YFinanceVolatilityProvider()
    assert isinstance(provider, VolatilityIndexProvider)


def test_yfinance_vol_provider_returns_vix_fetch_result_on_success() -> None:
    provider = YFinanceVolatilityProvider()
    mock_ticker = MagicMock()
    mock_ticker.fast_info.last_price = 16.78

    with patch(_YF_PATCH) as mock_yf:
        mock_yf.Ticker.return_value = mock_ticker
        result = provider.fetch_vix()

    assert result.available is True
    assert result.level == pytest.approx(16.78)


def test_yfinance_vol_provider_falls_back_to_history_when_fast_info_none() -> None:
    import pandas as pd

    provider = YFinanceVolatilityProvider()
    mock_ticker = MagicMock()
    mock_ticker.fast_info.last_price = None
    mock_hist = pd.DataFrame({"Close": [17.5]})
    mock_ticker.history.return_value = mock_hist

    with patch(_YF_PATCH) as mock_yf:
        mock_yf.Ticker.return_value = mock_ticker
        result = provider.fetch_vix()

    assert result.available is True
    assert result.level == pytest.approx(17.5)
    mock_ticker.history.assert_called_once_with(period="1d")


def test_yfinance_vol_provider_returns_unavailable_on_exception() -> None:
    provider = YFinanceVolatilityProvider()

    with patch(_YF_PATCH) as mock_yf:
        mock_yf.Ticker.side_effect = RuntimeError("network error")
        result = provider.fetch_vix()

    assert result.available is False
    assert result.level is None


def test_yfinance_vol_provider_returns_unavailable_when_no_data() -> None:
    import pandas as pd

    provider = YFinanceVolatilityProvider()
    mock_ticker = MagicMock()
    mock_ticker.fast_info.last_price = None
    mock_ticker.history.return_value = pd.DataFrame()  # empty

    with patch(_YF_PATCH) as mock_yf:
        mock_yf.Ticker.return_value = mock_ticker
        result = provider.fetch_vix()

    assert result.available is False
    assert result.level is None


# ---------------------------------------------------------------------------
# get_universe_snapshot — normal path
# ---------------------------------------------------------------------------


def test_get_universe_snapshot_returns_universe_snapshot() -> None:
    dp = _make_data_provider({"SPY": 540.0})
    vp = _make_vol_provider(18.0)

    result = get_universe_snapshot(
        symbols=["SPY"],
        provider=dp,
        vol_provider=vp,
        playbook=_PLAYBOOK,
        as_of=_AS_OF,
    )

    assert isinstance(result, UniverseSnapshot)


def test_get_universe_snapshot_populates_vix_level() -> None:
    dp = _make_data_provider({"SPY": 540.0})
    vp = _make_vol_provider(18.0)

    result = get_universe_snapshot(
        symbols=["SPY"], provider=dp, vol_provider=vp, playbook=_PLAYBOOK, as_of=_AS_OF
    )
    assert result.vix_level == pytest.approx(18.0)


def test_get_universe_snapshot_populates_as_of() -> None:
    dp = _make_data_provider({"SPY": 540.0})
    vp = _make_vol_provider(18.0)

    result = get_universe_snapshot(
        symbols=["SPY"], provider=dp, vol_provider=vp, playbook=_PLAYBOOK, as_of=_AS_OF
    )
    assert result.as_of == _AS_OF


def test_get_universe_snapshot_normal_regime() -> None:
    dp = _make_data_provider({"SPY": 540.0})
    vp = _make_vol_provider(20.0)  # 15 < 20 < 25 → NORMAL

    result = get_universe_snapshot(
        symbols=["SPY"], provider=dp, vol_provider=vp, playbook=_PLAYBOOK, as_of=_AS_OF
    )
    assert result.market_regime == MarketRegime.NORMAL


def test_get_universe_snapshot_low_vol_regime() -> None:
    dp = _make_data_provider({"SPY": 540.0})
    vp = _make_vol_provider(12.0)  # < 15 → LOW_VOL

    result = get_universe_snapshot(
        symbols=["SPY"], provider=dp, vol_provider=vp, playbook=_PLAYBOOK, as_of=_AS_OF
    )
    assert result.market_regime == MarketRegime.LOW_VOL


def test_get_universe_snapshot_high_vol_regime() -> None:
    dp = _make_data_provider({"SPY": 540.0})
    vp = _make_vol_provider(30.0)  # > 25 → HIGH_VOL

    result = get_universe_snapshot(
        symbols=["SPY"], provider=dp, vol_provider=vp, playbook=_PLAYBOOK, as_of=_AS_OF
    )
    assert result.market_regime == MarketRegime.HIGH_VOL


def test_get_universe_snapshot_symbol_price_populated() -> None:
    dp = _make_data_provider({"SPY": 540.5, "AAPL": 212.0})
    vp = _make_vol_provider(18.0)

    result = get_universe_snapshot(
        symbols=["SPY", "AAPL"],
        provider=dp,
        vol_provider=vp,
        playbook=_PLAYBOOK,
        as_of=_AS_OF,
    )
    assert result.symbol_snapshots["SPY"].price == pytest.approx(540.5)
    assert result.symbol_snapshots["AAPL"].price == pytest.approx(212.0)


def test_get_universe_snapshot_per_symbol_regime_echoes_market() -> None:
    dp = _make_data_provider({"SPY": 540.0, "AAPL": 212.0})
    vp = _make_vol_provider(20.0)  # NORMAL

    result = get_universe_snapshot(
        symbols=["SPY", "AAPL"],
        provider=dp,
        vol_provider=vp,
        playbook=_PLAYBOOK,
        as_of=_AS_OF,
    )
    assert result.symbol_snapshots["SPY"].regime == MarketRegime.NORMAL
    assert result.symbol_snapshots["AAPL"].regime == MarketRegime.NORMAL


def test_get_universe_snapshot_iv_fields_are_none() -> None:
    # IV rank/percentile/historical_vol are WP-3.4; always None here.
    dp = _make_data_provider({"SPY": 540.0})
    vp = _make_vol_provider(18.0)

    result = get_universe_snapshot(
        symbols=["SPY"], provider=dp, vol_provider=vp, playbook=_PLAYBOOK, as_of=_AS_OF
    )
    snap = result.symbol_snapshots["SPY"]
    assert snap.iv_rank is None
    assert snap.iv_percentile is None
    assert snap.historical_vol is None
    assert snap.days_to_earnings is None


# ---------------------------------------------------------------------------
# get_universe_snapshot — VIX unavailable
# ---------------------------------------------------------------------------


def test_get_universe_snapshot_vix_unavailable_sets_unknown_regime() -> None:
    dp = _make_data_provider({"SPY": 540.0})
    vp = _make_vol_provider(None, available=False)

    result = get_universe_snapshot(
        symbols=["SPY"], provider=dp, vol_provider=vp, playbook=_PLAYBOOK, as_of=_AS_OF
    )
    assert result.market_regime == MarketRegime.UNKNOWN


def test_get_universe_snapshot_vix_unavailable_per_symbol_regime_is_none() -> None:
    dp = _make_data_provider({"SPY": 540.0})
    vp = _make_vol_provider(None, available=False)

    result = get_universe_snapshot(
        symbols=["SPY"], provider=dp, vol_provider=vp, playbook=_PLAYBOOK, as_of=_AS_OF
    )
    assert result.symbol_snapshots["SPY"].regime is None


# ---------------------------------------------------------------------------
# get_universe_snapshot — price fetch failure
# ---------------------------------------------------------------------------


def test_get_universe_snapshot_excludes_symbol_on_price_failure() -> None:
    dp = MagicMock()
    dp.fetch_latest_price.side_effect = DataUnavailableError(
        "fetch_latest_price", "FAIL", Exception("timeout")
    )
    vp = _make_vol_provider(18.0)

    result = get_universe_snapshot(
        symbols=["FAIL"],
        provider=dp,
        vol_provider=vp,
        playbook=_PLAYBOOK,
        as_of=_AS_OF,
    )
    assert "FAIL" not in result.symbol_snapshots


def test_get_universe_snapshot_excludes_symbol_on_auth_error() -> None:
    from options_agent.data.providers import DataAuthError

    dp = MagicMock()
    dp.fetch_latest_price.side_effect = DataAuthError("credentials rejected")
    vp = _make_vol_provider(18.0)

    result = get_universe_snapshot(
        symbols=["SPY"],
        provider=dp,
        vol_provider=vp,
        playbook=_PLAYBOOK,
        as_of=_AS_OF,
    )
    assert "SPY" not in result.symbol_snapshots


def test_get_universe_snapshot_partial_failure_keeps_healthy_symbols() -> None:
    dp = MagicMock()

    def _price(sym: str) -> float:
        if sym == "BAD":
            raise DataUnavailableError("fetch_latest_price", "BAD", Exception())
        return 500.0

    dp.fetch_latest_price.side_effect = _price
    vp = _make_vol_provider(18.0)

    result = get_universe_snapshot(
        symbols=["SPY", "BAD"],
        provider=dp,
        vol_provider=vp,
        playbook=_PLAYBOOK,
        as_of=_AS_OF,
    )
    assert "SPY" in result.symbol_snapshots
    assert "BAD" not in result.symbol_snapshots


# ---------------------------------------------------------------------------
# get_universe_snapshot — macro events
# ---------------------------------------------------------------------------


def test_get_universe_snapshot_macro_events_forwarded() -> None:
    dp = _make_data_provider({"SPY": 540.0})
    vp = _make_vol_provider(18.0)
    fomc = MacroEvent(name="FOMC", event_date=date(2026, 7, 29), event_type="FOMC")

    with patch(
        "options_agent.data.market.get_macro_events", return_value=[fomc]
    ) as mock_macro:
        result = get_universe_snapshot(
            symbols=["SPY"],
            provider=dp,
            vol_provider=vp,
            playbook=_PLAYBOOK,
            macro_lookahead_days=30,
            as_of=_AS_OF,
        )
        mock_macro.assert_called_once_with(lookahead_days=30, as_of=_AS_OF.date())

    assert len(result.macro_events) == 1
    assert result.macro_events[0].event_type == "FOMC"


def test_get_universe_snapshot_empty_symbols_returns_empty_snapshot() -> None:
    dp = MagicMock()
    vp = _make_vol_provider(18.0)

    with patch("options_agent.data.market.get_macro_events", return_value=[]):
        result = get_universe_snapshot(
            symbols=[],
            provider=dp,
            vol_provider=vp,
            playbook=_PLAYBOOK,
            as_of=_AS_OF,
        )

    assert result.symbol_snapshots == {}
    dp.fetch_latest_price.assert_not_called()
