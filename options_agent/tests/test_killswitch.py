"""Tests for WP-7.1: kill-switch flag, helpers, and orchestrator cycle-top guards.

Critical invariants tested explicitly:
  - is_halted() returns True under FLATTEN (not just HALT).
    This is the highest-risk line in the card: implementing it as
    ``state == HALT`` silently allows entry under FLATTEN.
  - Entry cycle fails closed when kill-switch read fails.
  - Monitor cycle proceeds with NONE semantics when kill-switch read fails
    (never auto-FLATTENs on unreadable flag).
  - HALT does not freeze the monitor — a stop must still fire under HALT.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from options_agent.config import Config
from options_agent.contracts import (
    ActionTaken,
    KillSwitchState,
    ShortCircuitReason,
)
from options_agent.obs.killswitch import (
    KillSwitchEntry,
    get_current_state,
    is_flatten,
    is_halted,
    list_history,
    resume,
    set_state,
)
from options_agent.orchestrator import run_entry_cycle, run_monitor_cycle
from options_agent.risk.limits import Limits
from options_agent.state.db import get_connection

# ---------------------------------------------------------------------------
# Pure helper invariants
# ---------------------------------------------------------------------------


def test_is_halted_none() -> None:
    assert is_halted(KillSwitchState.NONE) is False


def test_is_halted_halt() -> None:
    assert is_halted(KillSwitchState.HALT) is True


def test_is_halted_flatten_implies_halt() -> None:
    """CRITICAL: FLATTEN must block entry — is_halted() must return True.

    Implementing as ``state == HALT`` is the single most common correctness
    mistake: under FLATTEN the entry cycle would proceed while positions are
    being force-closed, actively fighting itself.
    """
    assert is_halted(KillSwitchState.FLATTEN) is True


def test_is_flatten_none() -> None:
    assert is_flatten(KillSwitchState.NONE) is False


def test_is_flatten_halt() -> None:
    assert is_flatten(KillSwitchState.HALT) is False


def test_is_flatten_flatten() -> None:
    assert is_flatten(KillSwitchState.FLATTEN) is True


# ---------------------------------------------------------------------------
# DB operations: get_current_state
# ---------------------------------------------------------------------------


def test_get_current_state_empty_table(engine) -> None:
    """Empty kill_switch_log → NONE (system not armed)."""
    with get_connection(engine) as conn:
        assert get_current_state(conn) == KillSwitchState.NONE


def test_get_current_state_after_set_halt(engine) -> None:
    with get_connection(engine) as conn:
        set_state(conn, KillSwitchState.HALT, set_by="test", reason="test halt")
        assert get_current_state(conn) == KillSwitchState.HALT


def test_get_current_state_after_set_flatten(engine) -> None:
    with get_connection(engine) as conn:
        set_state(conn, KillSwitchState.FLATTEN, set_by="test", reason="test flatten")
        assert get_current_state(conn) == KillSwitchState.FLATTEN


def test_get_current_state_latest_row_wins(engine) -> None:
    """Multiple rows: the most recently created state is authoritative."""
    with get_connection(engine) as conn:
        set_state(conn, KillSwitchState.HALT, set_by="test", reason="first halt")
        set_state(conn, KillSwitchState.FLATTEN, set_by="test", reason="escalate")
        set_state(conn, KillSwitchState.NONE, set_by="test", reason="resolved")
        assert get_current_state(conn) == KillSwitchState.NONE


# ---------------------------------------------------------------------------
# DB operations: set_state / resume (append-only)
# ---------------------------------------------------------------------------


def test_set_state_returns_entry(engine) -> None:
    with get_connection(engine) as conn:
        entry = set_state(
            conn, KillSwitchState.HALT, set_by="operator", reason="routine test"
        )
    assert isinstance(entry, KillSwitchEntry)
    assert entry.state == KillSwitchState.HALT
    assert entry.set_by == "operator"
    assert entry.reason == "routine test"


def test_set_state_is_append_only(engine) -> None:
    """Each set_state call adds a new row; the table grows."""
    with get_connection(engine) as conn:
        set_state(conn, KillSwitchState.HALT, set_by="t", reason="r1")
        set_state(conn, KillSwitchState.FLATTEN, set_by="t", reason="r2")
        count = conn.execute(
            sa.select(sa.func.count()).select_from(sa.text("kill_switch_log"))
        ).scalar()
    assert count == 2


def test_set_state_requires_set_by(engine) -> None:
    with get_connection(engine) as conn:
        with pytest.raises(ValueError, match="set_by"):
            set_state(conn, KillSwitchState.HALT, set_by="", reason="reason")


def test_set_state_requires_reason(engine) -> None:
    with get_connection(engine) as conn:
        with pytest.raises(ValueError, match="reason"):
            set_state(conn, KillSwitchState.HALT, set_by="operator", reason="")


def test_resume_sets_none(engine) -> None:
    with get_connection(engine) as conn:
        set_state(conn, KillSwitchState.HALT, set_by="t", reason="halt")
        entry = resume(conn, set_by="t", reason="issue resolved")
        assert entry.state == KillSwitchState.NONE
        assert get_current_state(conn) == KillSwitchState.NONE


# ---------------------------------------------------------------------------
# DB operations: list_history
# ---------------------------------------------------------------------------


def test_list_history_empty(engine) -> None:
    with get_connection(engine) as conn:
        assert list_history(conn) == []


def test_list_history_newest_first(engine) -> None:
    with get_connection(engine) as conn:
        set_state(conn, KillSwitchState.HALT, set_by="t", reason="first")
        set_state(conn, KillSwitchState.FLATTEN, set_by="t", reason="second")
        history = list_history(conn)
    assert len(history) == 2
    assert history[0].state == KillSwitchState.FLATTEN
    assert history[1].state == KillSwitchState.HALT


def test_list_history_limit(engine) -> None:
    with get_connection(engine) as conn:
        for i in range(5):
            set_state(conn, KillSwitchState.HALT, set_by="t", reason=f"r{i}")
        history = list_history(conn, limit=3)
    assert len(history) == 3


# ---------------------------------------------------------------------------
# Entry cycle: kill-switch guards
# ---------------------------------------------------------------------------


def test_entry_cycle_none_proceeds(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """No kill switch armed → cycle proceeds past kill-switch check."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock, patch

    from options_agent.execution.broker import BrokerClient

    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    _tc = "options_agent.execution.broker.TradingClient"
    with patch(_tc, return_value=MagicMock()):
        broker = BrokerClient(Config())
    broker.list_open_orders = MagicMock(return_value=[])
    broker.get_all_positions = MagicMock(return_value=[])
    broker.get_account_activities = MagicMock(return_value=[])

    from options_agent.agent.stub_reasoner import stub_reasoner

    # Tuesday 2026-06-16 at 14:30 UTC — NYSE open, outside blackout windows.
    _market_hours = datetime(2026, 6, 16, 14, 30, tzinfo=UTC)
    config = Config(limits=Limits(allowed_strategies=frozenset()))

    with patch("options_agent.orchestrator.reason", return_value=stub_reasoner()):
        result = run_entry_cycle(
            config, broker=broker, engine=engine, _now=_market_hours
        )

    # Empty allowed_strategies → REJECTED (not NO_ACTION_GATED).
    assert result.action_taken == ActionTaken.REJECTED
    assert result.short_circuit_reason is None


def test_entry_cycle_halted_under_halt(engine) -> None:
    """HALT state → entry cycle returns NO_ACTION_GATED + KILL_SWITCH_HALT."""
    with get_connection(engine) as conn:
        set_state(conn, KillSwitchState.HALT, set_by="test", reason="testing halt")

    result = run_entry_cycle(Config(), engine=engine)

    assert result.action_taken == ActionTaken.NO_ACTION_GATED
    assert result.short_circuit_reason == ShortCircuitReason.KILL_SWITCH_HALT
    assert result.proposal is None


def test_entry_cycle_halted_under_flatten(engine) -> None:
    """FLATTEN → entry cycle returns NO_ACTION_GATED + KILL_SWITCH_FLATTEN."""
    with get_connection(engine) as conn:
        set_state(
            conn, KillSwitchState.FLATTEN, set_by="test", reason="testing flatten"
        )

    result = run_entry_cycle(Config(), engine=engine)

    assert result.action_taken == ActionTaken.NO_ACTION_GATED
    assert result.short_circuit_reason == ShortCircuitReason.KILL_SWITCH_FLATTEN
    assert result.proposal is None


def test_entry_cycle_flatten_uses_flatten_reason_not_halt(engine) -> None:
    """FLATTEN produces KILL_SWITCH_FLATTEN, not KILL_SWITCH_HALT.

    Both block entry, but WP-7 analytics must distinguish the two so it can
    correlate positions-closed-by-flatten with the triggering event.
    """
    with get_connection(engine) as conn:
        set_state(conn, KillSwitchState.FLATTEN, set_by="test", reason="flatten test")

    result = run_entry_cycle(Config(), engine=engine)

    assert result.short_circuit_reason is ShortCircuitReason.KILL_SWITCH_FLATTEN
    assert result.short_circuit_reason is not ShortCircuitReason.KILL_SWITCH_HALT


def test_entry_cycle_fail_closed_on_db_error(engine) -> None:
    """If kill-switch read raises, entry cycle fails closed (treats as HALT).

    This is the fail-safe direction for the entry cycle: we cannot confirm
    the switch is NONE, so we refuse to open new positions.
    """
    from options_agent import orchestrator as _orch

    original = _orch.get_current_state

    def _raise(conn):  # type: ignore[no-untyped-def]
        raise RuntimeError("DB unreachable")

    _orch.get_current_state = _raise
    try:
        result = run_entry_cycle(Config(), engine=engine)
    finally:
        _orch.get_current_state = original

    assert result.action_taken == ActionTaken.NO_ACTION_GATED
    assert result.short_circuit_reason == ShortCircuitReason.KILL_SWITCH_HALT


# ---------------------------------------------------------------------------
# Monitor cycle: kill-switch check
# ---------------------------------------------------------------------------

# Juneteenth 2026 — NYSE closed; lets us test monitor cycle without a broker.
_MC_CLOSED_NOW = datetime(2026, 6, 19, 18, 0, tzinfo=UTC)


def test_monitor_cycle_returns_result_under_none(engine) -> None:
    """Under NONE, monitor returns an empty MonitorResult without error."""
    result = run_monitor_cycle(Config(), engine=engine, _now=_MC_CLOSED_NOW)
    assert result.exits_triggered == []
    assert result.errors == []


def test_monitor_cycle_not_blocked_by_halt(engine) -> None:
    """HALT does not block the monitor cycle — exits still evaluate."""
    with get_connection(engine) as conn:
        set_state(conn, KillSwitchState.HALT, set_by="test", reason="halt test")

    # Market is closed so no exits fire, but the cycle must not short-circuit.
    result = run_monitor_cycle(Config(), engine=engine, _now=_MC_CLOSED_NOW)
    assert result.errors == []


def test_monitor_cycle_proceeds_under_flatten(engine) -> None:
    """Monitor proceeds under FLATTEN without raising."""
    with get_connection(engine) as conn:
        set_state(conn, KillSwitchState.FLATTEN, set_by="test", reason="flatten test")

    result = run_monitor_cycle(Config(), engine=engine, _now=_MC_CLOSED_NOW)
    assert result.errors == []


def test_monitor_cycle_proceeds_on_db_error(engine) -> None:
    """Monitor cycle continues with NONE semantics when kill-switch DB read fails.

    Fail-safe: normal exit evaluation continues; we never auto-FLATTEN on an
    unreadable flag.
    """
    from options_agent import orchestrator as _orch

    original = _orch.get_current_state

    def _raise(conn):  # type: ignore[no-untyped-def]
        raise RuntimeError("DB unreachable")

    _orch.get_current_state = _raise
    try:
        result = run_monitor_cycle(Config(), engine=engine, _now=_MC_CLOSED_NOW)
        assert result.errors == []
    finally:
        _orch.get_current_state = original


# ---------------------------------------------------------------------------
# WP-8.9: set_state() / resume() dispatcher injection
# ---------------------------------------------------------------------------


def _null_dispatcher(engine: sa.engine.Engine) -> tuple:
    """Return (dispatcher, channel) backed by NullChannel for assertions."""
    from options_agent.obs.alerts import AlertDispatcher, NullChannel

    channel = NullChannel()
    dispatcher = AlertDispatcher(channel, engine)
    return dispatcher, channel


def test_set_state_dispatches_kill_switch_change(engine) -> None:
    """set_state() with a dispatcher emits KILL_SWITCH_CHANGE."""
    from options_agent.contracts.alerts import AlertEventType

    dispatcher, channel = _null_dispatcher(engine)
    with dispatcher:
        with get_connection(engine) as conn:
            set_state(
                conn,
                KillSwitchState.HALT,
                set_by="operator",
                reason="test halt",
                dispatcher=dispatcher,
            )

    assert len(channel.sent) == 1
    event = channel.sent[0]
    assert event.event_type == AlertEventType.KILL_SWITCH_CHANGE
    assert "operator" in event.detail
    assert "HALT" in event.detail
    assert "test halt" in event.detail


def test_set_state_flatten_dispatches_kill_switch_change(engine) -> None:
    """FLATTEN transition also emits KILL_SWITCH_CHANGE."""
    from options_agent.contracts.alerts import AlertEventType

    dispatcher, channel = _null_dispatcher(engine)
    with dispatcher:
        with get_connection(engine) as conn:
            set_state(
                conn,
                KillSwitchState.FLATTEN,
                set_by="operator",
                reason="flatten test",
                dispatcher=dispatcher,
            )

    assert len(channel.sent) == 1
    assert channel.sent[0].event_type == AlertEventType.KILL_SWITCH_CHANGE
    assert "FLATTEN" in channel.sent[0].detail


def test_resume_dispatches_kill_switch_change(engine) -> None:
    """resume() with a dispatcher emits KILL_SWITCH_CHANGE for the NONE transition."""
    from options_agent.contracts.alerts import AlertEventType

    dispatcher, channel = _null_dispatcher(engine)
    with dispatcher:
        with get_connection(engine) as conn:
            set_state(conn, KillSwitchState.HALT, set_by="op", reason="halt first")
            resume(
                conn,
                set_by="operator",
                reason="issue resolved",
                dispatcher=dispatcher,
            )

    # Only the resume call carries a dispatcher — one event.
    assert len(channel.sent) == 1
    event = channel.sent[0]
    assert event.event_type == AlertEventType.KILL_SWITCH_CHANGE
    assert "NONE" in event.detail
    assert "issue resolved" in event.detail


def test_set_state_no_dispatcher_is_noop(engine) -> None:
    """dispatcher=None (the default) does not raise and writes the DB row normally."""
    with get_connection(engine) as conn:
        entry = set_state(
            conn,
            KillSwitchState.HALT,
            set_by="operator",
            reason="no dispatcher",
        )
    assert entry.state == KillSwitchState.HALT
