"""WP-8.5 — End-to-end entry cycle integration test with real data.

Runs run_entry_cycle() with use_real_data_tools=True against the Alpaca paper
account and verifies that the cycle completes with a valid journaled terminal
state. The success criterion is NOT "an order was placed" — during the IV
warm-up period (iv_rank=None for all symbols) the correct outcome is NO_ACTION,
which must also produce a valid JournalRecord. Asserting a specific trade would
fail on correct behaviour.

Success criteria (Q2-C from WP-8.5 briefing)
---------------------------------------------
  1. run_entry_cycle() completes without uncaught exception.
  2. The returned CycleResult has a valid action_taken.
  3. A JournalRecord is written to the DB (not just for OPENED — all non-gated
     terminal states write a record).

Running
-------
Requires ALPACA_API_KEY / ALPACA_SECRET_KEY. Market must be open for a full
entry attempt; the test skips gracefully on MARKET_CLOSED short-circuit.

    uv run pytest -m "integration" options_agent/tests/test_entry_cycle_real_data.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from options_agent.config import Config
from options_agent.contracts.orchestrator import CycleResult, ShortCircuitReason
from options_agent.contracts.state import ActionTaken
from options_agent.execution.broker import BrokerClient
from options_agent.obs.alerts import AlertDispatcher, NullChannel
from options_agent.orchestrator import run_entry_cycle
from options_agent.state.db import get_connection, metadata
from options_agent.state.journal import query_journal

_has_creds = bool(os.environ.get("ALPACA_API_KEY"))

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _has_creds,
        reason="ALPACA_API_KEY not set — skipping real-data e2e test",
    ),
]

_TEST_SYMBOLS = ["SPY", "AAPL"]

# ActionTaken values that are valid terminal states for a completed cycle.
_VALID_TERMINAL_ACTIONS = {
    ActionTaken.OPENED,
    ActionTaken.NO_ACTION_GATED,
    ActionTaken.NO_ACTION_AGENT,
    ActionTaken.REJECTED,
    ActionTaken.SIZED_TO_ZERO,
    ActionTaken.EXECUTION_FAILED,
}


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
        # Faster poll timeout for integration tests.
        order_poll_timeout_secs=10.0,
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
def _dispatcher(_engine) -> AlertDispatcher:
    return AlertDispatcher(NullChannel(), _engine)


def test_entry_cycle_real_data_completes(
    _config: Config, _engine, _broker: BrokerClient, _dispatcher: AlertDispatcher
) -> None:
    """Full cycle with real data produces a valid terminal state and no crash.

    Success criterion: cycle completes, action_taken is a known terminal state,
    and (when the cycle progressed past all gates) a JournalRecord is persisted.
    A NO_ACTION result due to market closed / blackout / empty action space is
    explicitly acceptable — do NOT re-run hoping to get an OPENED; this is not
    a reliability test for the market or the agent's strategy selection.
    """
    with _dispatcher:
        result = run_entry_cycle(
            _config,
            broker=_broker,
            engine=_engine,
            dispatcher=_dispatcher,
        )

    assert isinstance(result, CycleResult)
    assert result.action_taken in _valid_actions(), (
        f"unexpected action_taken={result.action_taken}"
    )

    # For early short-circuits (market closed, kill switch, etc.) there is no
    # journal record — that is correct and expected. Only assert the journal when
    # the cycle progressed past the temporal gate into full cycle execution.
    if result.short_circuit_reason in {
        ShortCircuitReason.MARKET_CLOSED,
        ShortCircuitReason.BLACKOUT_WINDOW,
        ShortCircuitReason.KILL_SWITCH_HALT,
        ShortCircuitReason.KILL_SWITCH_FLATTEN,
    }:
        pytest.skip(
            f"Cycle short-circuited at {result.short_circuit_reason} "
            "before full data assembly — this is correct behaviour, not a failure."
        )

    # Past temporal gates: a journal record must exist.
    with get_connection(_engine) as conn:
        # Query all records; we only ran one cycle so there should be at most one.
        records = query_journal(conn)
    assert len(records) >= 1, (
        "cycle progressed past temporal gates but wrote no JournalRecord"
    )
    record = records[-1]
    assert record.action_taken == result.action_taken


def _valid_actions() -> set[ActionTaken]:
    return _VALID_TERMINAL_ACTIONS
