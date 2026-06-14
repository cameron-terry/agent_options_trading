"""Tests for AlpacaDataClient (WP-3.1 provider adapter).

All tests use mocked Alpaca SDK responses — no live credentials required.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from alpaca.common.exceptions import APIError

from options_agent.data.providers import (
    DataAuthError,
    DataProvider,
    DataUnavailableError,
    RawOptionContract,
)
from options_agent.data.providers.alpaca_data import (
    AlpacaDataClient,
    _parse_occ_symbol,
    _snapshot_to_raw,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UNDERLYING = "SPY"


def _api_error(status_code: int) -> APIError:
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_http_error = MagicMock()
    mock_http_error.response = mock_response
    return APIError("test error", mock_http_error)


def _make_snapshot(
    symbol: str = "SPY260718P00450000",
    bid: float | None = 1.20,
    ask: float | None = 1.30,
    iv: float | None = 0.24,
    delta: float | None = -0.28,
    gamma: float | None = 0.04,
    theta: float | None = -0.08,
    vega: float | None = 0.22,
    rho: float | None = -0.05,
) -> MagicMock:
    snap = MagicMock()
    snap.symbol = symbol

    if bid is not None or ask is not None:
        snap.latest_quote = MagicMock()
        snap.latest_quote.bid_price = bid
        snap.latest_quote.ask_price = ask
    else:
        snap.latest_quote = None

    snap.implied_volatility = iv

    if delta is not None:
        snap.greeks = MagicMock()
        snap.greeks.delta = delta
        snap.greeks.gamma = gamma
        snap.greeks.theta = theta
        snap.greeks.vega = vega
        snap.greeks.rho = rho
    else:
        snap.greeks = None

    return snap


def _client_with_mocks(
    monkeypatch: pytest.MonkeyPatch,
    option_mock: MagicMock | None = None,
    stock_mock: MagicMock | None = None,
) -> tuple[AlpacaDataClient, MagicMock, MagicMock]:
    """Construct an AlpacaDataClient with patched SDK clients."""
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    opt = option_mock or MagicMock()
    stk = stock_mock or MagicMock()
    with (
        patch(
            "options_agent.data.providers.alpaca_data.OptionHistoricalDataClient",
            return_value=opt,
        ),
        patch(
            "options_agent.data.providers.alpaca_data.StockHistoricalDataClient",
            return_value=stk,
        ),
    ):
        client = AlpacaDataClient()
    return client, opt, stk


# ---------------------------------------------------------------------------
# _parse_occ_symbol unit tests
# ---------------------------------------------------------------------------


def test_parse_occ_put() -> None:
    exp, right, strike = _parse_occ_symbol("SPY260718P00450000", "SPY")
    assert exp == date(2026, 7, 18)
    assert right == "put"
    assert strike == 450.0


def test_parse_occ_call() -> None:
    exp, right, strike = _parse_occ_symbol("AAPL260718C00185000", "AAPL")
    assert exp == date(2026, 7, 18)
    assert right == "call"
    assert strike == 185.0


def test_parse_occ_fractional_strike() -> None:
    # 00185500 → 185.5
    _, _, strike = _parse_occ_symbol("SPY260718C00185500", "SPY")
    assert strike == 185.5


def test_parse_occ_longer_underlying() -> None:
    exp, right, strike = _parse_occ_symbol("GOOGL260718P00150000", "GOOGL")
    assert exp == date(2026, 7, 18)
    assert right == "put"
    assert strike == 150.0


# ---------------------------------------------------------------------------
# _snapshot_to_raw unit tests
# ---------------------------------------------------------------------------


def test_snapshot_to_raw_happy_path() -> None:
    snap = _make_snapshot()
    raw = _snapshot_to_raw(snap, _UNDERLYING)

    assert isinstance(raw, RawOptionContract)
    assert raw.symbol == "SPY260718P00450000"
    assert raw.underlying == "SPY"
    assert raw.strike == 450.0
    assert raw.expiration == date(2026, 7, 18)
    assert raw.right == "put"
    assert raw.bid == 1.20
    assert raw.ask == 1.30
    assert raw.implied_volatility == 0.24
    assert raw.delta == -0.28
    assert raw.gamma == 0.04
    assert raw.theta == -0.08
    assert raw.vega == 0.22
    assert raw.rho == -0.05


def test_snapshot_to_raw_no_quote() -> None:
    snap = _make_snapshot(bid=None, ask=None)
    raw = _snapshot_to_raw(snap, _UNDERLYING)
    assert raw.bid is None
    assert raw.ask is None


def test_snapshot_to_raw_no_greeks() -> None:
    snap = _make_snapshot(delta=None)
    raw = _snapshot_to_raw(snap, _UNDERLYING)
    assert raw.delta is None
    assert raw.gamma is None
    assert raw.theta is None
    assert raw.vega is None
    assert raw.rho is None


def test_snapshot_to_raw_volume_oi_always_none() -> None:
    snap = _make_snapshot()
    raw = _snapshot_to_raw(snap, _UNDERLYING)
    assert raw.volume is None
    assert raw.open_interest is None


def test_snapshot_to_raw_partial_quote_bid_none() -> None:
    # quote object exists but bid_price is None (no bids on illiquid contract)
    snap = _make_snapshot()
    snap.latest_quote = MagicMock()
    snap.latest_quote.bid_price = None
    snap.latest_quote.ask_price = 2.50
    raw = _snapshot_to_raw(snap, _UNDERLYING)
    assert raw.bid is None
    assert raw.ask == 2.50


def test_snapshot_to_raw_partial_quote_ask_none() -> None:
    # quote object exists but ask_price is None (no offers on illiquid contract)
    snap = _make_snapshot()
    snap.latest_quote = MagicMock()
    snap.latest_quote.bid_price = 0.05
    snap.latest_quote.ask_price = None
    raw = _snapshot_to_raw(snap, _UNDERLYING)
    assert raw.bid == 0.05
    assert raw.ask is None


# ---------------------------------------------------------------------------
# AlpacaDataClient construction
# ---------------------------------------------------------------------------


def test_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    with pytest.raises(OSError, match="ALPACA_API_KEY"):
        AlpacaDataClient()


def test_missing_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(OSError, match="ALPACA_SECRET_KEY"):
        AlpacaDataClient()


def test_missing_both_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(OSError):
        AlpacaDataClient()


def test_implements_data_provider_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = _client_with_mocks(monkeypatch)
    assert isinstance(client, DataProvider)


# ---------------------------------------------------------------------------
# fetch_option_chain — happy path
# ---------------------------------------------------------------------------


def test_fetch_option_chain_returns_parsed_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _make_snapshot()
    opt_mock = MagicMock()
    opt_mock.get_option_chain.return_value = {"SPY260718P00450000": snap}

    client, _, _ = _client_with_mocks(monkeypatch, option_mock=opt_mock)
    client.begin_cycle()
    result = client.fetch_option_chain("SPY")

    assert len(result) == 1
    assert result[0].symbol == "SPY260718P00450000"
    assert result[0].right == "put"
    assert result[0].strike == 450.0


def test_fetch_option_chain_multiple_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap_put = _make_snapshot(symbol="SPY260718P00450000")
    snap_call = _make_snapshot(symbol="SPY260718C00460000", bid=0.80, ask=0.90)

    opt_mock = MagicMock()
    opt_mock.get_option_chain.return_value = {
        "SPY260718P00450000": snap_put,
        "SPY260718C00460000": snap_call,
    }

    client, _, _ = _client_with_mocks(monkeypatch, option_mock=opt_mock)
    client.begin_cycle()
    result = client.fetch_option_chain("SPY")

    assert len(result) == 2
    rights = {c.right for c in result}
    assert rights == {"put", "call"}


def test_fetch_option_chain_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Alpaca returns {} when no contracts are listed (e.g. symbol has no options).
    # WP-3.2 must handle an empty list without passing it through a min_oi filter.
    opt_mock = MagicMock()
    opt_mock.get_option_chain.return_value = {}

    client, _, _ = _client_with_mocks(monkeypatch, option_mock=opt_mock)
    client.begin_cycle()
    result = client.fetch_option_chain("SPY")

    assert result == []


# ---------------------------------------------------------------------------
# fetch_option_chain — cache behaviour
# ---------------------------------------------------------------------------


def test_fetch_option_chain_cached_within_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _make_snapshot()
    opt_mock = MagicMock()
    opt_mock.get_option_chain.return_value = {"SPY260718P00450000": snap}

    client, _, _ = _client_with_mocks(monkeypatch, option_mock=opt_mock)
    client.begin_cycle()

    r1 = client.fetch_option_chain("SPY")
    r2 = client.fetch_option_chain("SPY")

    assert r1 is r2
    opt_mock.get_option_chain.assert_called_once()


def test_fetch_option_chain_cache_cleared_by_begin_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _make_snapshot()
    opt_mock = MagicMock()
    opt_mock.get_option_chain.return_value = {"SPY260718P00450000": snap}

    client, _, _ = _client_with_mocks(monkeypatch, option_mock=opt_mock)

    client.begin_cycle()
    client.fetch_option_chain("SPY")  # call 1

    client.begin_cycle()
    client.fetch_option_chain("SPY")  # call 2 — cache was cleared

    assert opt_mock.get_option_chain.call_count == 2


def test_different_symbols_cached_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy_snap = _make_snapshot(symbol="SPY260718P00450000")
    qqq_snap = _make_snapshot(symbol="QQQ260718P00400000", bid=0.60, ask=0.70)

    opt_mock = MagicMock()
    opt_mock.get_option_chain.side_effect = [
        {"SPY260718P00450000": spy_snap},
        {"QQQ260718P00400000": qqq_snap},
    ]

    client, _, _ = _client_with_mocks(monkeypatch, option_mock=opt_mock)
    client.begin_cycle()

    spy = client.fetch_option_chain("SPY")
    qqq = client.fetch_option_chain("QQQ")

    # Each symbol fetched once; second calls use cache
    client.fetch_option_chain("SPY")
    client.fetch_option_chain("QQQ")

    assert opt_mock.get_option_chain.call_count == 2
    assert spy[0].underlying == "SPY"
    assert qqq[0].underlying == "QQQ"


# ---------------------------------------------------------------------------
# fetch_option_chain — rate-limit retry
# ---------------------------------------------------------------------------


def test_rate_limit_retries_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _make_snapshot()
    opt_mock = MagicMock()
    opt_mock.get_option_chain.side_effect = [
        _api_error(429),
        {"SPY260718P00450000": snap},
    ]

    client, _, _ = _client_with_mocks(monkeypatch, option_mock=opt_mock)
    client.begin_cycle()

    with patch("options_agent.data.providers.alpaca_data.time.sleep") as mock_sleep:
        result = client.fetch_option_chain("SPY")

    assert len(result) == 1
    mock_sleep.assert_called_once()
    delay = mock_sleep.call_args[0][0]
    assert 0.5 <= delay <= 1.5  # base 1.0 ± 50%


def test_rate_limit_two_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _make_snapshot()
    opt_mock = MagicMock()
    opt_mock.get_option_chain.side_effect = [
        _api_error(429),
        _api_error(429),
        {"SPY260718P00450000": snap},
    ]

    client, _, _ = _client_with_mocks(monkeypatch, option_mock=opt_mock)
    client.begin_cycle()

    with patch("options_agent.data.providers.alpaca_data.time.sleep") as mock_sleep:
        result = client.fetch_option_chain("SPY")

    assert len(result) == 1
    assert mock_sleep.call_count == 2
    delays = [c[0][0] for c in mock_sleep.call_args_list]
    assert 0.5 <= delays[0] <= 1.5  # base 1.0 ± 50%
    assert 1.0 <= delays[1] <= 3.0  # base 2.0 ± 50%


def test_rate_limit_exhausted_raises_data_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opt_mock = MagicMock()
    opt_mock.get_option_chain.side_effect = [
        _api_error(429),
        _api_error(429),
        _api_error(429),
    ]

    client, _, _ = _client_with_mocks(monkeypatch, option_mock=opt_mock)
    client.begin_cycle()

    with (
        patch("options_agent.data.providers.alpaca_data.time.sleep"),
        pytest.raises(DataUnavailableError) as exc_info,
    ):
        client.fetch_option_chain("SPY")

    err = exc_info.value
    assert err.operation == "fetch_option_chain"
    assert err.symbol == "SPY"
    assert opt_mock.get_option_chain.call_count == 3


# ---------------------------------------------------------------------------
# fetch_option_chain — auth failure
# ---------------------------------------------------------------------------


def test_auth_failure_raises_data_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opt_mock = MagicMock()
    opt_mock.get_option_chain.side_effect = _api_error(401)

    client, _, _ = _client_with_mocks(monkeypatch, option_mock=opt_mock)
    client.begin_cycle()

    with pytest.raises(DataAuthError):
        client.fetch_option_chain("SPY")


def test_auth_failure_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opt_mock = MagicMock()
    opt_mock.get_option_chain.side_effect = _api_error(401)

    client, _, _ = _client_with_mocks(monkeypatch, option_mock=opt_mock)
    client.begin_cycle()

    with pytest.raises(DataAuthError):
        client.fetch_option_chain("SPY")

    opt_mock.get_option_chain.assert_called_once()


# ---------------------------------------------------------------------------
# fetch_option_chain — non-retryable API errors
# ---------------------------------------------------------------------------


def test_non_retryable_error_raises_data_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opt_mock = MagicMock()
    opt_mock.get_option_chain.side_effect = _api_error(500)

    client, _, _ = _client_with_mocks(monkeypatch, option_mock=opt_mock)
    client.begin_cycle()

    with pytest.raises(DataUnavailableError):
        client.fetch_option_chain("SPY")

    opt_mock.get_option_chain.assert_called_once()


# ---------------------------------------------------------------------------
# fetch_latest_price — happy path
# ---------------------------------------------------------------------------


def test_fetch_latest_price_returns_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bar = MagicMock()
    bar.close = 456.32

    stk_mock = MagicMock()
    stk_mock.get_stock_latest_bar.return_value = {"SPY": bar}

    client, _, _ = _client_with_mocks(monkeypatch, stock_mock=stk_mock)
    client.begin_cycle()
    price = client.fetch_latest_price("SPY")

    assert price == 456.32


def test_fetch_latest_price_cached_within_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bar = MagicMock()
    bar.close = 456.32

    stk_mock = MagicMock()
    stk_mock.get_stock_latest_bar.return_value = {"SPY": bar}

    client, _, _ = _client_with_mocks(monkeypatch, stock_mock=stk_mock)
    client.begin_cycle()

    p1 = client.fetch_latest_price("SPY")
    p2 = client.fetch_latest_price("SPY")

    assert p1 == p2
    stk_mock.get_stock_latest_bar.assert_called_once()


def test_fetch_latest_price_cache_cleared_by_begin_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bar = MagicMock()
    bar.close = 456.32

    stk_mock = MagicMock()
    stk_mock.get_stock_latest_bar.return_value = {"SPY": bar}

    client, _, _ = _client_with_mocks(monkeypatch, stock_mock=stk_mock)

    client.begin_cycle()
    client.fetch_latest_price("SPY")

    client.begin_cycle()
    client.fetch_latest_price("SPY")

    assert stk_mock.get_stock_latest_bar.call_count == 2


def test_fetch_latest_price_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stk_mock = MagicMock()
    stk_mock.get_stock_latest_bar.side_effect = _api_error(401)

    client, _, _ = _client_with_mocks(monkeypatch, stock_mock=stk_mock)
    client.begin_cycle()

    with pytest.raises(DataAuthError):
        client.fetch_latest_price("SPY")


def test_fetch_latest_price_rate_limit_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bar = MagicMock()
    bar.close = 456.32

    stk_mock = MagicMock()
    stk_mock.get_stock_latest_bar.side_effect = [
        _api_error(429),
        {"SPY": bar},
    ]

    client, _, _ = _client_with_mocks(monkeypatch, stock_mock=stk_mock)
    client.begin_cycle()

    with patch("options_agent.data.providers.alpaca_data.time.sleep"):
        price = client.fetch_latest_price("SPY")

    assert price == 456.32


# ---------------------------------------------------------------------------
# begin_cycle clears all method caches
# ---------------------------------------------------------------------------


def test_begin_cycle_clears_mixed_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _make_snapshot()
    bar = MagicMock()
    bar.close = 456.32

    opt_mock = MagicMock()
    opt_mock.get_option_chain.return_value = {"SPY260718P00450000": snap}

    stk_mock = MagicMock()
    stk_mock.get_stock_latest_bar.return_value = {"SPY": bar}

    client, _, _ = _client_with_mocks(
        monkeypatch, option_mock=opt_mock, stock_mock=stk_mock
    )

    client.begin_cycle()
    client.fetch_option_chain("SPY")
    client.fetch_latest_price("SPY")

    client.begin_cycle()
    client.fetch_option_chain("SPY")
    client.fetch_latest_price("SPY")

    assert opt_mock.get_option_chain.call_count == 2
    assert stk_mock.get_stock_latest_bar.call_count == 2
