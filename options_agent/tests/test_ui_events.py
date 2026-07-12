"""Tests for WP-9.4: live activity stream (SSE)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

from starlette.routing import Route

from options_agent.contracts.journal import JournalRecord
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.state import (
    ActionTaken,
    AssetClass,
    ContextSnapshot,
    Decision,
    KillSwitchState,
    LegStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.obs.killswitch import set_state
from options_agent.state.crud import insert_position, update_position
from options_agent.state.db import build_engine, metadata
from options_agent.state.journal import write_journal_record
from options_agent.ui import events as events_module
from options_agent.ui.app import create_app
from options_agent.ui.events import (
    _poll_for_changes,
    _read_high_water_marks,
    event_stream,
)

_NOW = datetime(2026, 6, 19, 14, 0, 0, tzinfo=UTC)
_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
)


def _engine():
    engine = build_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def _make_position(*, marked_at: datetime = _NOW) -> Position:
    legs = [
        PositionLeg(
            leg=Leg(
                right="put", side="sell", strike=450.0, expiration=date(2026, 8, 15)
            ),
            filled_qty=1,
            avg_fill_price=0.55,
            status=LegStatus.OPEN,
        ),
        PositionLeg(
            leg=Leg(
                right="put", side="buy", strike=445.0, expiration=date(2026, 8, 15)
            ),
            filled_qty=1,
            avg_fill_price=0.0,
            status=LegStatus.OPEN,
        ),
    ]
    return Position(
        id=str(uuid.uuid4()),
        underlying="SPY",
        strategy="bull_put_spread",
        legs=legs,
        quantity=1,
        entry_net_amount=-275.0,
        current_mark=-150.0,
        marked_at=marked_at,
        unrealized_pnl=0.0,
        realized_pnl=None,
        exit_plan=_EXIT_PLAN,
        status=PositionStatus.OPEN,
        opened_at=_NOW,
        closed_at=None,
        nearest_expiration=date(2026, 8, 15),
        est_max_loss=2225.0,
        est_max_profit=275.0,
        opening_order_id="open-ord-001",
        asset_class=AssetClass.OPTION_STRATEGY,
    )


def _make_journal_record(
    *, cycle_id: str = "cycle-001", timestamp: datetime = _NOW
) -> JournalRecord:
    proposal = TradeProposal(
        action="OPEN",
        underlying="SPY",
        strategy="bull_put_spread",
        legs=[
            Leg(right="put", side="sell", strike=450.0, expiration=date(2026, 8, 15))
        ],
        thesis="Bullish bias near support",
        iv_rationale="IV rank 65th pct",
        catalyst_check="No earnings within 30 days",
        conviction=0.7,
        est_max_loss=2225.0,
        est_max_profit=275.0,
        breakevens=[447.50],
        net_delta=0.12,
        net_theta=8.50,
        net_vega=-0.30,
        exit_plan=_EXIT_PLAN,
        informed_by=[],
    )
    context_snapshot = ContextSnapshot(
        assembled_context={"iv_rank": 65, "regime": "neutral"},
        context_hash="sha256:abcdef1234567890",
        model_id="claude-sonnet-4-6",
        prompt_version="v1.0.0",
        assembled_at=timestamp,
    )
    decision = Decision(
        proposal=proposal,
        validation_result=None,
        sizing_result=None,
        action_taken=ActionTaken.OPENED,
    )
    return JournalRecord(
        cycle_id=cycle_id,
        timestamp=timestamp,
        action_taken=ActionTaken.OPENED,
        decision=decision,
        context_snapshot=context_snapshot,
        position_ids=[],
        order_ids=[],
        strategy="bull_put_spread",
        underlying="SPY",
        limits_version="v1.0.0",
        prompt_version="v1.0.0",
        model_id="claude-sonnet-4-6",
        rejection_rule_ids=[],
    )


# --- _read_high_water_marks / _poll_for_changes -----------------------------


def test_baseline_ignores_rows_that_existed_before_connect():
    engine = _engine()
    with engine.begin() as conn:
        write_journal_record(conn, _make_journal_record())

    marks = _read_high_water_marks(engine)
    changed, _ = _poll_for_changes(engine, marks)

    assert changed == []


def test_new_journal_record_detected():
    engine = _engine()
    with engine.begin() as conn:
        write_journal_record(conn, _make_journal_record(timestamp=_NOW))
    marks = _read_high_water_marks(engine)

    with engine.begin() as conn:
        write_journal_record(
            conn,
            _make_journal_record(
                cycle_id="cycle-002", timestamp=_NOW + timedelta(minutes=1)
            ),
        )
    changed, marks = _poll_for_changes(engine, marks)

    assert changed == ["journal"]
    # A second poll with no new rows reports nothing further.
    changed_again, _ = _poll_for_changes(engine, marks)
    assert changed_again == []


def test_killswitch_change_detected():
    engine = _engine()
    marks = _read_high_water_marks(engine)

    with engine.begin() as conn:
        set_state(conn, KillSwitchState.HALT, set_by="test", reason="unit test")
    changed, _ = _poll_for_changes(engine, marks)

    assert changed == ["killswitch"]


def test_position_mark_update_detected():
    engine = _engine()
    position = _make_position(marked_at=_NOW)
    with engine.begin() as conn:
        insert_position(conn, position)
    marks = _read_high_water_marks(engine)

    position.marked_at = _NOW + timedelta(minutes=1)
    position.current_mark = -140.0
    with engine.begin() as conn:
        update_position(conn, position)
    changed, _ = _poll_for_changes(engine, marks)

    assert changed == ["positions"]


def test_multiple_tables_changed_in_one_poll():
    engine = _engine()
    marks = _read_high_water_marks(engine)

    with engine.begin() as conn:
        write_journal_record(conn, _make_journal_record())
        set_state(conn, KillSwitchState.HALT, set_by="test", reason="unit test")
    changed, _ = _poll_for_changes(engine, marks)

    assert set(changed) == {"journal", "killswitch"}


# --- event_stream() wrapper (the async generator wired into GET /api/events)
#
# Driven directly with asyncio.run rather than through TestClient/httpx: this
# generator never terminates on its own (it loops until the client
# disconnects), and exercising it through the full ASGI/httpx streaming
# transport in this environment deadlocks (TestClient appears to fully drain
# the body before yielding control back to the test, which never happens for
# an infinite generator). Driving it directly still exercises the exact
# function the route in app.py awaits from.


class _FakeRequest:
    """Minimal stand-in for fastapi.Request — only is_disconnected() is used."""

    def __init__(self, *, disconnected: bool = False) -> None:
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


def test_app_registers_events_route():
    app = create_app(engine=_engine())
    paths = {route.path for route in app.routes if isinstance(route, Route)}
    assert "/api/events" in paths


def test_event_stream_stops_immediately_when_already_disconnected():
    engine = _engine()

    async def run() -> list[str]:
        gen = event_stream(engine, _FakeRequest(disconnected=True))
        return [chunk async for chunk in gen]

    assert asyncio.run(run()) == []


def test_event_stream_emits_update_after_new_journal_record(monkeypatch):
    monkeypatch.setattr(events_module, "POLL_INTERVAL_SECONDS", 0.01)
    engine = _engine()

    async def run() -> str:
        gen = event_stream(engine, _FakeRequest())
        try:
            first = await asyncio.wait_for(gen.__anext__(), timeout=2)
            assert first == ": heartbeat\n\n"

            with engine.begin() as conn:
                write_journal_record(conn, _make_journal_record())

            for _ in range(200):  # bounded: several heartbeats before it lands
                chunk = await asyncio.wait_for(gen.__anext__(), timeout=2)
                if chunk.startswith("event: update"):
                    return chunk
            raise AssertionError("no update event observed")
        finally:
            await gen.aclose()

    chunk = asyncio.run(run())
    assert chunk == 'event: update\ndata: {"kind": "journal"}\n\n'
