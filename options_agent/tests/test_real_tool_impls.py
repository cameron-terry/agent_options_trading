"""WP-8.5 — Component-scoped integration tests for real WP-3 tool implementations.

Verifies that each real tool impl in data/tools.py produces well-formed output
when called against live Alpaca and yfinance APIs. Tests are component-scoped:
they call the tool function directly rather than running a full entry cycle.

Running these tests
-------------------
Requires ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment and an active
Alpaca paper account.  Do NOT include in the per-commit suite.

    uv run pytest -m "integration" options_agent/tests/test_real_tool_impls.py -v

Audit purpose (Q1 answer from WP-8.5 briefing)
------------------------------------------------
Each component below was previously exercised only on mock data. These tests are
the first time each WP is touched by the real data distribution — messy reality
cases like iv_rank=None warm-up, wide spreads, and illiquid strikes. The tests
confirm the seams hold when real data's shape hits components tested on synthetic
inputs.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from options_agent.config import Config
from options_agent.contracts.data import (
    EventInfo,
    FilteredChain,
    PortfolioState,
    UniverseSnapshot,
)
from options_agent.data.tools import build_real_tool_impls
from options_agent.execution.broker import BrokerClient
from options_agent.state.db import metadata

# ── skip guard ────────────────────────────────────────────────────────────────

_has_creds = bool(os.environ.get("ALPACA_API_KEY"))

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _has_creds, reason="ALPACA_API_KEY not set — skipping real tool impl tests"
    ),
]

# ── fixtures ──────────────────────────────────────────────────────────────────

_TEST_SYMBOLS = ["SPY", "AAPL"]


@pytest.fixture(scope="module")
def _universe_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    p = tmp_path_factory.mktemp("universe") / "universe.txt"
    p.write_text("\n".join(_TEST_SYMBOLS) + "\n")
    return p


@pytest.fixture(scope="module")
def _config(_universe_file: Path) -> Config:
    return Config(
        alpaca_paper=True,
        use_real_data_tools=True,
        universe_file=_universe_file,
    )


@pytest.fixture(scope="module")
def _engine():
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(eng)
    yield eng
    metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture(scope="module")
def _broker(_config: Config) -> BrokerClient:
    return BrokerClient(_config)


@pytest.fixture(scope="module")
def _tool_impls(_config: Config, _engine, _broker: BrokerClient) -> dict:
    return build_real_tool_impls(_config, _engine, _broker)


# ── portfolio state ───────────────────────────────────────────────────────────


def test_get_portfolio_state_shape(_tool_impls: dict) -> None:
    """Real account + empty local DB → well-formed PortfolioState."""
    result = _tool_impls["get_portfolio_state"]({})
    assert isinstance(result, PortfolioState)
    assert result.account_equity > 0, "paper account should have positive equity"
    assert result.buying_power >= 0
    assert result.options_buying_power >= 0
    assert result.approval_level >= 0
    assert isinstance(result.positions, list)
    # Net Greeks start at 0.0; assembler fills them in from chain data.
    assert result.net_dollar_delta == 0.0


# ── universe snapshot ─────────────────────────────────────────────────────────


def test_get_universe_snapshot_shape(_tool_impls: dict) -> None:
    """Real Alpaca + yfinance VIX → well-formed UniverseSnapshot."""
    result = _tool_impls["get_universe_snapshot"]({})
    assert isinstance(result, UniverseSnapshot)
    # iv_rank may be None during warm-up — that is correct behaviour, not a bug.
    for sym in _TEST_SYMBOLS:
        if sym in result.symbol_snapshots:
            snap = result.symbol_snapshots[sym]
            assert snap.price > 0, f"{sym} price should be positive"
            # iv_rank=None is valid and expected during the warm-up period.
            if snap.iv_rank is not None:
                assert 0.0 <= snap.iv_rank <= 100.0, f"{sym} iv_rank out of range"


def test_get_universe_snapshot_vix_non_negative(_tool_impls: dict) -> None:
    result = _tool_impls["get_universe_snapshot"]({})
    # vix_level is 0.0 when unavailable (UNKNOWN regime sentinel); otherwise > 0.
    assert result.vix_level >= 0.0


# ── filtered chain ────────────────────────────────────────────────────────────


def test_get_filtered_chain_spy_shape(_tool_impls: dict) -> None:
    """Real Alpaca options chain for SPY → well-formed FilteredChain."""
    result = _tool_impls["get_filtered_chain"]({"symbol": "SPY"})
    assert isinstance(result, FilteredChain)
    assert result.underlying == "SPY"
    assert result.underlying_price > 0
    # The chain may be empty outside market hours (no quotes) but must be valid.
    for contract in result.contracts:
        assert contract.delta is not None, "post-filter contract must have delta"
        assert contract.iv is not None, "post-filter contract must have iv"
        assert contract.spread_width >= 0
        assert contract.dte >= 0


def test_get_filtered_chain_strategy_hint(_tool_impls: dict) -> None:
    """Strategy hint filters to puts only."""
    result = _tool_impls["get_filtered_chain"](
        {"symbol": "SPY", "strategy_hint": "bull_put_spread"}
    )
    for contract in result.contracts:
        assert contract.right == "put", "bull_put_spread hint must return only puts"


# ── events ────────────────────────────────────────────────────────────────────


def test_get_events_shape(_tool_impls: dict) -> None:
    """Real yfinance events for test symbols → dict[str, EventInfo]."""
    result = _tool_impls["get_events"]({"symbols": _TEST_SYMBOLS})
    assert isinstance(result, dict)
    for sym in _TEST_SYMBOLS:
        assert sym in result, f"missing EventInfo for {sym}"
        info = result[sym]
        assert isinstance(info, EventInfo)
        # earnings may be None (no confirmed event in lookahead window)
        if info.earnings is not None:
            assert info.earnings.event_date is not None


# ── journal (empty DB) ────────────────────────────────────────────────────────


def test_get_journal_by_symbol_empty(_tool_impls: dict) -> None:
    """Fresh DB has no journal records — returns empty list, not an error."""
    result = _tool_impls["get_journal_by_symbol"]({"symbol": "SPY"})
    assert isinstance(result, list)
    assert len(result) == 0, "fresh DB should have no journal records"


# ── position history (unknown ID) ─────────────────────────────────────────────


def test_get_position_history_unknown(_tool_impls: dict) -> None:
    """Unknown position_id → None, not an exception."""
    result = _tool_impls["get_position_history"]({"position_id": "nonexistent-id"})
    assert result is None
