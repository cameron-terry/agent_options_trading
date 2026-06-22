"""Tests for WP-8.10: run_daily_iv_job() and context assembler IV enrichment.

run_daily_iv_job() tests:
  - Happy path: records ATM IV for each symbol on a trading session.
  - Idempotency: running twice on the same day updates, no duplicate rows.
  - Non-trading day: returns without writing any rows.
  - Chain failure: failed symbol writes no row; other symbols still recorded.
  - ATM IV unavailable: no row for that symbol (no null/zero fill).
  - Empty universe: no-op.
  - Symbol failures dispatch a SCHEDULER_SKIP WARN alert.

Context assembler IV enrichment tests (data/tools.py _universe() closure):
  - iv_rank and iv_percentile are populated when DB has sufficient history.
  - iv_rank and iv_percentile remain None when history is insufficient.
  - Chain failure leaves iv_rank/iv_percentile as None (not an exception).
"""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa

from options_agent.agent.tools import TOOL_GET_UNIVERSE_SNAPSHOT
from options_agent.config import Config
from options_agent.contracts.alerts import AlertEventType, AlertSeverity
from options_agent.obs.alerts import AlertDispatcher, NullChannel
from options_agent.orchestrator import run_daily_iv_job
from options_agent.state.db import iv_history_table

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UNIVERSE_TXT = "SPY\nQQQ\n"

# A trading day known to be in the XNYS calendar.
_TRADING_DAY = date(2026, 6, 17)
# US Independence Day — not a session.
_HOLIDAY = date(2026, 7, 4)
# Weekend.
_WEEKEND = date(2026, 6, 21)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path: Path) -> Config:
    universe = tmp_path / "universe.txt"
    universe.write_text(_UNIVERSE_TXT)
    return Config(universe_file=universe)


@pytest.fixture
def null_dispatcher(engine):
    with AlertDispatcher(NullChannel(), engine) as d:
        yield d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_rows(engine, symbol: str, obs_date: date) -> int:
    with engine.connect() as conn:
        result = conn.execute(
            sa.select(sa.func.count())
            .select_from(iv_history_table)
            .where(
                sa.and_(
                    iv_history_table.c.symbol == symbol,
                    iv_history_table.c.observation_date == obs_date,
                )
            )
        )
        return result.scalar_one()


def _get_stored_atm_iv(engine, symbol: str, obs_date: date) -> float | None:
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(iv_history_table.c.atm_iv).where(
                sa.and_(
                    iv_history_table.c.symbol == symbol,
                    iv_history_table.c.observation_date == obs_date,
                )
            )
        ).first()
    return row.atm_iv if row else None


def _seed_iv_history(
    engine, symbol: str, days: int = 60, base_iv: float = 0.20
) -> None:
    """Insert `days` IV observations ending yesterday."""
    today = date.today()
    with engine.begin() as conn:
        for i in range(days):
            obs_date = today - timedelta(days=i + 1)
            conn.execute(
                iv_history_table.insert().values(
                    symbol=symbol,
                    observation_date=obs_date,
                    atm_iv=base_iv + i * 0.001,
                )
            )


def _mock_provider_context(atm_iv: float | None = 0.25):
    """Return context managers that mock AlpacaDataClient and get_atm_iv."""
    return (
        patch("options_agent.orchestrator.AlpacaDataClient"),
        patch("options_agent.orchestrator.get_atm_iv", return_value=atm_iv),
    )


# ---------------------------------------------------------------------------
# run_daily_iv_job — happy path
# ---------------------------------------------------------------------------


def test_run_daily_iv_job_records_for_each_symbol(config, engine) -> None:
    """Happy path: one row per symbol written on a trading day."""
    with (
        patch("options_agent.orchestrator.AlpacaDataClient") as MockClient,
        patch("options_agent.orchestrator.get_atm_iv", return_value=0.25),
    ):
        mock_prov = MockClient.return_value
        mock_prov.fetch_option_chain.return_value = []
        mock_prov.fetch_latest_price.return_value = 500.0

        run_daily_iv_job(config, engine=engine, _today=_TRADING_DAY)

    assert _count_rows(engine, "SPY", _TRADING_DAY) == 1
    assert _count_rows(engine, "QQQ", _TRADING_DAY) == 1
    assert _get_stored_atm_iv(engine, "SPY", _TRADING_DAY) == pytest.approx(0.25)


def test_run_daily_iv_job_idempotent(config, engine) -> None:
    """Running twice on the same day updates the row, no duplicate rows."""

    def _run(iv: float) -> None:
        with (
            patch("options_agent.orchestrator.AlpacaDataClient") as MockClient,
            patch("options_agent.orchestrator.get_atm_iv", return_value=iv),
        ):
            mock_prov = MockClient.return_value
            mock_prov.fetch_option_chain.return_value = []
            mock_prov.fetch_latest_price.return_value = 500.0
            run_daily_iv_job(config, engine=engine, _today=_TRADING_DAY)

    _run(0.20)
    _run(0.22)

    assert _count_rows(engine, "SPY", _TRADING_DAY) == 1
    assert _count_rows(engine, "QQQ", _TRADING_DAY) == 1
    assert _get_stored_atm_iv(engine, "SPY", _TRADING_DAY) == pytest.approx(0.22)


# ---------------------------------------------------------------------------
# run_daily_iv_job — non-trading days
# ---------------------------------------------------------------------------


def test_run_daily_iv_job_skips_holiday(config, engine) -> None:
    """No rows written when the date is a market holiday."""
    with patch("options_agent.orchestrator.AlpacaDataClient") as MockClient:
        run_daily_iv_job(config, engine=engine, _today=_HOLIDAY)
        MockClient.assert_not_called()

    assert _count_rows(engine, "SPY", _HOLIDAY) == 0


def test_run_daily_iv_job_skips_weekend(config, engine) -> None:
    """No rows written on weekends."""
    with patch("options_agent.orchestrator.AlpacaDataClient") as MockClient:
        run_daily_iv_job(config, engine=engine, _today=_WEEKEND)
        MockClient.assert_not_called()

    assert _count_rows(engine, "SPY", _WEEKEND) == 0


# ---------------------------------------------------------------------------
# run_daily_iv_job — missing data: no null/zero fill
# ---------------------------------------------------------------------------


def test_run_daily_iv_job_skips_symbol_on_chain_failure(config, engine) -> None:
    """Symbol whose chain fetch raises writes no row; other symbols succeed."""

    def _chain(symbol: str):
        if symbol == "SPY":
            raise RuntimeError("network error")
        return []

    with (
        patch("options_agent.orchestrator.AlpacaDataClient") as MockClient,
        patch("options_agent.orchestrator.get_atm_iv", return_value=0.20),
    ):
        mock_prov = MockClient.return_value
        mock_prov.fetch_option_chain.side_effect = _chain
        mock_prov.fetch_latest_price.return_value = 100.0
        run_daily_iv_job(config, engine=engine, _today=_TRADING_DAY)

    assert _count_rows(engine, "SPY", _TRADING_DAY) == 0
    assert _count_rows(engine, "QQQ", _TRADING_DAY) == 1


def test_run_daily_iv_job_no_row_when_atm_iv_none(config, engine) -> None:
    """When get_atm_iv() returns None no row is written (not null/zero fill)."""
    with (
        patch("options_agent.orchestrator.AlpacaDataClient") as MockClient,
        patch("options_agent.orchestrator.get_atm_iv", return_value=None),
    ):
        mock_prov = MockClient.return_value
        mock_prov.fetch_option_chain.return_value = []
        mock_prov.fetch_latest_price.return_value = 100.0
        run_daily_iv_job(config, engine=engine, _today=_TRADING_DAY)

    assert _count_rows(engine, "SPY", _TRADING_DAY) == 0
    assert _count_rows(engine, "QQQ", _TRADING_DAY) == 0


def test_run_daily_iv_job_empty_universe(engine) -> None:
    """No-op when the universe file has no symbols."""
    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
        f.write("# comment only\n")
        tmp = Path(f.name)

    cfg = Config(universe_file=tmp)
    try:
        with patch("options_agent.orchestrator.AlpacaDataClient") as MockClient:
            run_daily_iv_job(cfg, engine=engine, _today=_TRADING_DAY)
            MockClient.assert_not_called()
    finally:
        tmp.unlink()


# ---------------------------------------------------------------------------
# run_daily_iv_job — alert on symbol failures
# ---------------------------------------------------------------------------


def test_run_daily_iv_job_dispatches_warn_on_failures(config, engine) -> None:
    """Symbol failures dispatch a SCHEDULER_SKIP WARN alert."""
    dispatched: list = []

    class _CapturingChannel:
        def send(self, event) -> None:  # type: ignore[override]
            dispatched.append(event)

    with AlertDispatcher(_CapturingChannel(), engine) as disp:  # type: ignore[arg-type]
        with (
            patch("options_agent.orchestrator.AlpacaDataClient") as MockClient,
            patch(
                "options_agent.orchestrator.get_atm_iv",
                side_effect=RuntimeError("boom"),
            ),
        ):
            mock_prov = MockClient.return_value
            mock_prov.fetch_option_chain.return_value = []
            mock_prov.fetch_latest_price.return_value = 100.0
            run_daily_iv_job(
                config, engine=engine, dispatcher=disp, _today=_TRADING_DAY
            )

    assert any(
        e.event_type == AlertEventType.SCHEDULER_SKIP
        and e.severity == AlertSeverity.WARN
        for e in dispatched
    )


# ---------------------------------------------------------------------------
# Context assembler IV enrichment (via build_real_tool_impls)
# ---------------------------------------------------------------------------

# These tests call the _universe() closure inside build_real_tool_impls() by
# invoking the returned tool_impls dict. The external dependencies (chain
# provider, DB, get_atm_iv, compute_iv_rank/percentile) are all mocked so
# no live data or real DB is needed.


def _make_universe_snapshot(symbols: list[str]):
    """Minimal UniverseSnapshot with iv_rank=None for each symbol."""
    import datetime as dt

    from options_agent.contracts.data import (
        MarketRegime,
        SymbolSnapshot,
        UniverseSnapshot,
    )

    return UniverseSnapshot(
        symbol_snapshots={
            sym: SymbolSnapshot(
                symbol=sym,
                price=100.0,
                iv_rank=None,
                iv_percentile=None,
                historical_vol=None,
                regime=None,
                days_to_earnings=None,
            )
            for sym in symbols
        },
        vix_level=18.0,
        market_regime=MarketRegime.NORMAL,
        macro_events=[],
        as_of=dt.datetime.now(dt.UTC),
    )


def _build_tool_impls_mocked(
    tmp_path: Path, engine, *, iv_rank, iv_pct, atm_iv: float | None = 0.25
):
    """Build real tool impls with all external I/O mocked."""
    universe = tmp_path / "u.txt"
    universe.write_text("SPY\n")
    cfg = Config(universe_file=universe, use_real_data_tools=True)
    broker = MagicMock()
    broker.get_account.return_value = MagicMock(
        equity=10000,
        buying_power=5000,
        options_buying_power=5000,
        options_approved_level=2,
    )

    mock_snapshot = _make_universe_snapshot(["SPY"])
    mock_chain = MagicMock()

    with (
        patch("options_agent.data.tools.AlpacaDataClient") as MockDataClient,
        patch("options_agent.data.tools.YFinanceVolatilityProvider"),
        patch("options_agent.data.tools.YFinanceProvider"),
        patch("options_agent.data.tools._universe_impl", return_value=mock_snapshot),
        patch("options_agent.data.tools.get_atm_iv", return_value=atm_iv),
        patch("options_agent.data.tools.compute_iv_rank", return_value=iv_rank),
        patch("options_agent.data.tools.compute_iv_percentile", return_value=iv_pct),
    ):
        mock_data_prov = MockDataClient.return_value
        mock_data_prov.fetch_option_chain.return_value = [mock_chain]
        mock_data_prov.fetch_latest_price.return_value = 100.0

        from options_agent.data.tools import build_real_tool_impls

        with patch("options_agent.data.tools.list_open_positions", return_value=[]):
            tool_impls = build_real_tool_impls(cfg, engine, broker)
            snapshot = tool_impls[TOOL_GET_UNIVERSE_SNAPSHOT]({})

    return snapshot


def test_universe_snapshot_iv_rank_populated(tmp_path: Path, engine) -> None:
    """iv_rank and iv_percentile are set when compute functions return values."""
    snapshot = _build_tool_impls_mocked(tmp_path, engine, iv_rank=0.75, iv_pct=0.60)
    ss = snapshot.symbol_snapshots["SPY"]
    assert ss.iv_rank == pytest.approx(0.75)
    assert ss.iv_percentile == pytest.approx(0.60)


def test_universe_snapshot_iv_rank_none_when_insufficient_history(
    tmp_path: Path, engine
) -> None:
    """iv_rank stays None when compute_iv_rank returns None (< min_days)."""
    snapshot = _build_tool_impls_mocked(tmp_path, engine, iv_rank=None, iv_pct=None)
    ss = snapshot.symbol_snapshots["SPY"]
    assert ss.iv_rank is None
    assert ss.iv_percentile is None


def test_universe_snapshot_iv_rank_none_on_chain_failure(
    tmp_path: Path, engine
) -> None:
    """Chain fetch failure leaves iv_rank None; no exception propagates."""
    universe = tmp_path / "u.txt"
    universe.write_text("SPY\n")
    cfg = Config(universe_file=universe, use_real_data_tools=True)
    broker = MagicMock()
    broker.get_account.return_value = MagicMock(
        equity=10000,
        buying_power=5000,
        options_buying_power=5000,
        options_approved_level=2,
    )

    mock_snapshot = _make_universe_snapshot(["SPY"])

    with (
        patch("options_agent.data.tools.AlpacaDataClient") as MockDataClient,
        patch("options_agent.data.tools.YFinanceVolatilityProvider"),
        patch("options_agent.data.tools.YFinanceProvider"),
        patch("options_agent.data.tools._universe_impl", return_value=mock_snapshot),
        patch(
            "options_agent.data.tools.get_atm_iv",
            side_effect=RuntimeError("chain unavailable"),
        ),
        patch("options_agent.data.tools.list_open_positions", return_value=[]),
    ):
        mock_data_prov = MockDataClient.return_value
        mock_data_prov.fetch_option_chain.return_value = []
        mock_data_prov.fetch_latest_price.return_value = 100.0

        from options_agent.data.tools import build_real_tool_impls

        tool_impls = build_real_tool_impls(cfg, engine, broker)
        snapshot = tool_impls[TOOL_GET_UNIVERSE_SNAPSHOT]({})

    ss = snapshot.symbol_snapshots["SPY"]
    assert ss.iv_rank is None
    assert ss.iv_percentile is None


def test_universe_snapshot_iv_rank_none_when_atm_iv_unavailable(
    tmp_path: Path, engine
) -> None:
    """When get_atm_iv() returns None, iv_rank/iv_percentile stay None."""
    snapshot = _build_tool_impls_mocked(
        tmp_path, engine, iv_rank=0.5, iv_pct=0.5, atm_iv=None
    )
    ss = snapshot.symbol_snapshots["SPY"]
    assert ss.iv_rank is None
    assert ss.iv_percentile is None
