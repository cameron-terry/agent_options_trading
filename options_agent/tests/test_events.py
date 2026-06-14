"""Tests for data/events.py, data/providers/event_provider.py,
data/providers/yfinance_provider.py, and data/macro_calendar.py.

All tests use stub providers or mocked yf.Ticker — no live network calls.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from options_agent.contracts.data import (
    EarningsEvent,
    EventInfo,
    ExDividendEvent,
    MacroEvent,
)
from options_agent.data.events import get_events, get_macro_events
from options_agent.data.providers.event_provider import (
    EventProvider,
    RawDividendResult,
    RawEarningsResult,
)
from options_agent.data.providers.yfinance_provider import YFinanceProvider

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_TODAY = date(2026, 6, 14)
_LOOKAHEAD = 60  # days

# ---------------------------------------------------------------------------
# Stub EventProvider for logic tests
# ---------------------------------------------------------------------------


class _StubProvider:
    """Minimal EventProvider stub — returns preset results without hitting yfinance."""

    def __init__(
        self,
        earnings: dict[str, RawEarningsResult] | None = None,
        dividends: dict[str, RawDividendResult] | None = None,
    ) -> None:
        self._earnings = earnings or {}
        self._dividends = dividends or {}

    def fetch_earnings(
        self,
        symbols: list[str],
        lookahead_days: int,
        as_of: date | None = None,
    ) -> dict[str, RawEarningsResult]:
        return {
            s: self._earnings.get(s, RawEarningsResult(event=None, available=True))
            for s in symbols
        }

    def fetch_dividends(
        self,
        symbols: list[str],
        lookahead_days: int,
        as_of: date | None = None,
    ) -> dict[str, RawDividendResult]:
        return {
            s: self._dividends.get(s, RawDividendResult(event=None, available=True))
            for s in symbols
        }


# _StubProvider must satisfy the EventProvider protocol.
assert isinstance(_StubProvider(), EventProvider)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _earnings_result(days_ahead: int, confirmed: bool = False) -> RawEarningsResult:
    return RawEarningsResult(
        event=EarningsEvent(
            event_date=_TODAY + timedelta(days=days_ahead), confirmed=confirmed
        ),
        available=True,
    )


def _make_earnings_df(earnings_date: date) -> pd.DataFrame:
    """Minimal DataFrame matching yfinance's earnings_dates format."""
    idx = pd.DatetimeIndex([pd.Timestamp(earnings_date)], name="Earnings Date")
    return pd.DataFrame(
        {
            "EPS Estimate": [float("nan")],
            "Reported EPS": [float("nan")],
            "Surprise(%)": [float("nan")],
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# get_events() — DoD: earnings-proximity correct for ≥5 known tickers
# ---------------------------------------------------------------------------

_KNOWN_TICKER_SCENARIOS: list[tuple[str, int | None]] = [
    ("AAPL", 20),  # mid-window earnings
    ("MSFT", 35),  # mid-window earnings
    ("SPY", None),  # ETF, no earnings
    ("NVDA", 10),  # close — the DTE boundary case
    ("AMZN", 50),  # near end of window
    ("GOOG", 5),  # within any common blackout window
]


@pytest.mark.parametrize("symbol,days_ahead", _KNOWN_TICKER_SCENARIOS)
def test_known_tickers_earnings_proximity(symbol: str, days_ahead: int | None) -> None:
    """DoD: days_to_earnings derivable from EventInfo for ≥5 known tickers."""
    earnings: dict[str, RawEarningsResult] = {}
    if days_ahead is not None:
        earnings[symbol] = _earnings_result(days_ahead)
    else:
        earnings[symbol] = RawEarningsResult(event=None, available=True)

    result = get_events(
        [symbol], _LOOKAHEAD, _StubProvider(earnings=earnings), as_of=_TODAY
    )
    info = result[symbol]

    assert info.data_available is True
    if days_ahead is None:
        assert info.earnings is None
    else:
        assert info.earnings is not None
        actual_days = (info.earnings.event_date - _TODAY).days
        assert actual_days == days_ahead


# ---------------------------------------------------------------------------
# DTE-aware boundary case (NVDA, 10 days out)
# ---------------------------------------------------------------------------


def test_dte_aware_boundary_nvda() -> None:
    """Earnings 10 days out supports WP-4's DTE-aware blackout rule.

    A 7-DTE position expires before earnings (7 < 10) — no blackout needed.
    A 45-DTE position spans earnings (45 > 10) — WP-4 should block it.
    This test proves days_to_earnings is the correct integer for that logic.
    """
    result = get_events(
        ["NVDA"],
        _LOOKAHEAD,
        _StubProvider(earnings={"NVDA": _earnings_result(10)}),
        as_of=_TODAY,
    )

    days_to_earnings = (result["NVDA"].earnings.event_date - _TODAY).days  # type: ignore[union-attr]
    assert days_to_earnings == 10

    dte_short = 7
    dte_long = 45
    # WP-4 rule: block if dte >= days_to_earnings (earnings falls within position life)
    assert dte_short < days_to_earnings  # short-dated: safe — expires before earnings
    assert (
        dte_long > days_to_earnings
    )  # long-dated: blocked — earnings within position life


# ---------------------------------------------------------------------------
# data_available semantics
# ---------------------------------------------------------------------------


def test_data_unavailable_when_provider_fails() -> None:
    """Provider failure → data_available=False; None != 'no events'."""
    provider = _StubProvider(
        earnings={"XYZ": RawEarningsResult(event=None, available=False)},
        dividends={"XYZ": RawDividendResult(event=None, available=False)},
    )
    info = get_events(["XYZ"], _LOOKAHEAD, provider, as_of=_TODAY)["XYZ"]

    assert info.data_available is False
    assert info.earnings is None


def test_data_available_true_when_no_events_in_window() -> None:
    """No events + successful fetch → data_available=True, earnings=None.

    None here means 'checked and found nothing', not 'could not check'.
    WP-4 must distinguish: this case is safe; data_available=False is not.
    """
    info = get_events(["SPY"], _LOOKAHEAD, _StubProvider(), as_of=_TODAY)["SPY"]

    assert info.data_available is True
    assert info.earnings is None


def test_partial_provider_failure_per_symbol() -> None:
    """One symbol fails; others succeed — failures are isolated per symbol."""
    provider = _StubProvider(
        earnings={
            "AAPL": _earnings_result(30),
            "FAIL": RawEarningsResult(event=None, available=False),
        },
    )
    result = get_events(["AAPL", "FAIL"], _LOOKAHEAD, provider, as_of=_TODAY)

    assert result["AAPL"].data_available is True
    assert result["AAPL"].earnings is not None
    assert result["FAIL"].data_available is False


def test_earnings_failure_dividends_ok_marks_unavailable() -> None:
    """data_available=False when earnings fail even if dividends succeed."""
    provider = _StubProvider(
        earnings={"KO": RawEarningsResult(event=None, available=False)},
        dividends={
            "KO": RawDividendResult(
                event=ExDividendEvent(event_date=_TODAY + timedelta(15), amount=0.485),
                available=True,
            )
        },
    )
    info = get_events(["KO"], _LOOKAHEAD, provider, as_of=_TODAY)["KO"]

    assert info.data_available is False
    # dividends still flow through even though overall availability is False
    assert info.ex_dividend is not None


# ---------------------------------------------------------------------------
# ex_dividend population
# ---------------------------------------------------------------------------


def test_ex_dividend_populated() -> None:
    """Ex-dividend data flows through when provider returns it."""
    ex_date = _TODAY + timedelta(days=15)
    provider = _StubProvider(
        dividends={
            "KO": RawDividendResult(
                event=ExDividendEvent(event_date=ex_date, amount=0.485),
                available=True,
            )
        }
    )
    info = get_events(["KO"], _LOOKAHEAD, provider, as_of=_TODAY)["KO"]

    assert info.data_available is True
    assert info.ex_dividend is not None
    assert info.ex_dividend.event_date == ex_date
    assert info.ex_dividend.amount == pytest.approx(0.485)


# ---------------------------------------------------------------------------
# confirmed flag passthrough
# ---------------------------------------------------------------------------


def test_earnings_confirmed_flag_preserved() -> None:
    """confirmed field is carried through faithfully — not normalised."""
    provider = _StubProvider(
        earnings={
            "CONFIRMED": RawEarningsResult(
                event=EarningsEvent(event_date=_TODAY + timedelta(10), confirmed=True),
                available=True,
            ),
            "ESTIMATED": RawEarningsResult(
                event=EarningsEvent(event_date=_TODAY + timedelta(10), confirmed=False),
                available=True,
            ),
        }
    )
    result = get_events(["CONFIRMED", "ESTIMATED"], _LOOKAHEAD, provider, as_of=_TODAY)

    assert result["CONFIRMED"].earnings is not None
    assert result["CONFIRMED"].earnings.confirmed is True
    assert result["ESTIMATED"].earnings is not None
    assert result["ESTIMATED"].earnings.confirmed is False


# ---------------------------------------------------------------------------
# All symbols present in result
# ---------------------------------------------------------------------------


def test_all_requested_symbols_present() -> None:
    """get_events returns a key for every requested symbol."""
    symbols = ["AAPL", "MSFT", "SPY", "NVDA", "AMZN"]
    result = get_events(symbols, _LOOKAHEAD, _StubProvider(), as_of=_TODAY)
    assert set(result.keys()) == set(symbols)


# ---------------------------------------------------------------------------
# get_macro_events() tests
# ---------------------------------------------------------------------------


def test_macro_events_are_macroevents() -> None:
    events = get_macro_events(lookahead_days=60, as_of=_TODAY)
    assert all(isinstance(e, MacroEvent) for e in events)


def test_macro_events_within_window() -> None:
    """All returned events fall within [today, today + lookahead]."""
    lookahead = 60
    events = get_macro_events(lookahead_days=lookahead, as_of=_TODAY)
    cutoff = _TODAY + timedelta(days=lookahead)
    for event in events:
        assert _TODAY <= event.event_date <= cutoff


def test_macro_events_outside_window_excluded() -> None:
    """Events beyond the 1-day window are not returned."""
    events = get_macro_events(lookahead_days=1, as_of=_TODAY)
    cutoff = _TODAY + timedelta(days=1)
    for event in events:
        assert event.event_date <= cutoff


def test_macro_event_types_are_valid() -> None:
    """Only recognised event types appear."""
    events = get_macro_events(lookahead_days=365, as_of=_TODAY)
    for event in events:
        assert event.event_type in ("FOMC", "CPI", "NFP", "OTHER")


def test_macro_events_include_fomc_and_nfp_types() -> None:
    """Calendar contains all three event types within a 60-day window from today."""
    events = get_macro_events(lookahead_days=60, as_of=_TODAY)
    types_found = {e.event_type for e in events}
    # Within 60 days of Jun 14 there should be FOMC (Jun 17), NFP (Jul 2), CPI (Jul 14)
    assert "FOMC" in types_found
    assert "NFP" in types_found
    assert "CPI" in types_found


def test_macro_staleness_warning_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Warning is emitted when lookahead extends beyond calendar coverage."""
    with caplog.at_level(logging.WARNING, logger="options_agent.data.events"):
        get_macro_events(lookahead_days=1000, as_of=_TODAY)
    messages = " ".join(caplog.messages)
    assert "macro_calendar" in messages.lower() or "covers through" in messages.lower()


def test_macro_no_warning_within_calendar(caplog: pytest.LogCaptureFixture) -> None:
    """No warning when window is within calendar coverage."""
    with caplog.at_level(logging.WARNING, logger="options_agent.data.events"):
        get_macro_events(lookahead_days=60, as_of=_TODAY)
    assert not caplog.records


# ---------------------------------------------------------------------------
# YFinanceProvider unit tests (yf.Ticker mocked — no network)
# ---------------------------------------------------------------------------


@patch("options_agent.data.providers.yfinance_provider.yf.Ticker")
def test_yfinance_earnings_happy_path(mock_ticker_cls: MagicMock) -> None:
    event_date = date(2026, 7, 22)
    mock = MagicMock()
    mock.earnings_dates = _make_earnings_df(event_date)
    mock_ticker_cls.return_value = mock

    result = YFinanceProvider().fetch_earnings(["AAPL"], 60, as_of=_TODAY)

    assert result["AAPL"].available is True
    assert result["AAPL"].event is not None
    assert result["AAPL"].event.event_date == event_date
    # yfinance future dates are always marked unconfirmed
    assert result["AAPL"].event.confirmed is False


@patch("options_agent.data.providers.yfinance_provider.yf.Ticker")
def test_yfinance_earnings_no_dates(mock_ticker_cls: MagicMock) -> None:
    mock = MagicMock()
    mock.earnings_dates = None
    mock_ticker_cls.return_value = mock

    result = YFinanceProvider().fetch_earnings(["SPY"], 60, as_of=_TODAY)

    assert result["SPY"].available is True
    assert result["SPY"].event is None


@patch("options_agent.data.providers.yfinance_provider.yf.Ticker")
def test_yfinance_earnings_out_of_window_excluded(mock_ticker_cls: MagicMock) -> None:
    """Earnings beyond the lookahead window are not returned."""
    far_date = _TODAY + timedelta(days=120)
    mock = MagicMock()
    mock.earnings_dates = _make_earnings_df(far_date)
    mock_ticker_cls.return_value = mock

    result = YFinanceProvider().fetch_earnings(["XYZ"], 60, as_of=_TODAY)

    assert result["XYZ"].available is True
    assert result["XYZ"].event is None


@patch("options_agent.data.providers.yfinance_provider.yf.Ticker")
def test_yfinance_earnings_failure_marks_unavailable(
    mock_ticker_cls: MagicMock,
) -> None:
    """Network/parse failure → available=False, not a raised exception."""
    mock_ticker_cls.side_effect = RuntimeError("network error")

    result = YFinanceProvider().fetch_earnings(["XYZ"], 60, as_of=_TODAY)

    assert result["XYZ"].available is False
    assert result["XYZ"].event is None


@patch("options_agent.data.providers.yfinance_provider.yf.Ticker")
def test_yfinance_dividend_happy_path(mock_ticker_cls: MagicMock) -> None:
    import calendar as _cal

    ex_date = date(2026, 7, 1)
    ex_ts = int(_cal.timegm(ex_date.timetuple()))
    mock = MagicMock()
    mock.info = {"exDividendDate": ex_ts, "dividendRate": 0.50}
    mock_ticker_cls.return_value = mock

    result = YFinanceProvider().fetch_dividends(["KO"], 60, as_of=_TODAY)

    assert result["KO"].available is True
    assert result["KO"].event is not None
    assert result["KO"].event.event_date == ex_date
    assert result["KO"].event.amount == pytest.approx(0.50)


@patch("options_agent.data.providers.yfinance_provider.yf.Ticker")
def test_yfinance_dividend_no_ex_div(mock_ticker_cls: MagicMock) -> None:
    mock = MagicMock()
    mock.info = {}
    mock_ticker_cls.return_value = mock

    result = YFinanceProvider().fetch_dividends(["NVDA"], 60, as_of=_TODAY)

    assert result["NVDA"].available is True
    assert result["NVDA"].event is None


@patch("options_agent.data.providers.yfinance_provider.yf.Ticker")
def test_yfinance_dividend_failure_marks_unavailable(
    mock_ticker_cls: MagicMock,
) -> None:
    mock_ticker_cls.side_effect = RuntimeError("timeout")

    result = YFinanceProvider().fetch_dividends(["ERR"], 60, as_of=_TODAY)

    assert result["ERR"].available is False
    assert result["ERR"].event is None


@patch("options_agent.data.providers.yfinance_provider.yf.Ticker")
def test_yfinance_multiple_symbols_independent(mock_ticker_cls: MagicMock) -> None:
    """Per-symbol failures do not affect other symbols in the same batch."""
    aapl_date = _TODAY + timedelta(days=20)

    def _ticker_factory(symbol: str) -> MagicMock:
        mock = MagicMock()
        if symbol == "AAPL":
            mock.earnings_dates = _make_earnings_df(aapl_date)
        elif symbol == "FAIL":
            mock_ticker_cls.side_effect = None
            raise RuntimeError("boom")
        else:
            mock.earnings_dates = None
        return mock

    mock_ticker_cls.side_effect = _ticker_factory

    result = YFinanceProvider().fetch_earnings(
        ["AAPL", "SPY", "FAIL"], 60, as_of=_TODAY
    )

    assert result["AAPL"].available is True
    assert result["AAPL"].event is not None
    assert result["SPY"].available is True
    assert result["SPY"].event is None
    assert result["FAIL"].available is False


@patch("options_agent.data.providers.yfinance_provider.yf.Ticker")
def test_yfinance_dividend_out_of_window_excluded(mock_ticker_cls: MagicMock) -> None:
    """Ex-div date beyond the window → event=None."""
    import calendar as _cal

    far_date = _TODAY + timedelta(days=120)
    far_ts = int(_cal.timegm(far_date.timetuple()))
    mock = MagicMock()
    mock.info = {"exDividendDate": far_ts, "dividendRate": 1.0}
    mock_ticker_cls.return_value = mock

    result = YFinanceProvider().fetch_dividends(["XYZ"], 60, as_of=_TODAY)

    assert result["XYZ"].available is True
    assert result["XYZ"].event is None


# ---------------------------------------------------------------------------
# EventInfo round-trip with data_available field (WP-0 amendment regression)
# ---------------------------------------------------------------------------


def test_event_info_round_trip_with_data_available() -> None:
    """EventInfo serialises and deserialises the new data_available field."""
    ei = EventInfo(
        symbol="AAPL",
        earnings=EarningsEvent(event_date=date(2026, 7, 22), confirmed=False),
        ex_dividend=None,
        data_available=True,
    )
    assert EventInfo.model_validate(ei.model_dump()) == ei
    assert EventInfo.model_validate_json(ei.model_dump_json()) == ei


def test_event_info_data_available_defaults_true() -> None:
    """data_available defaults to True so existing callers are not broken."""
    ei = EventInfo(symbol="SPY", earnings=None, ex_dividend=None)
    assert ei.data_available is True


def test_event_info_unavailable_round_trip() -> None:
    ei = EventInfo(symbol="XYZ", earnings=None, ex_dividend=None, data_available=False)
    assert EventInfo.model_validate(ei.model_dump()).data_available is False
