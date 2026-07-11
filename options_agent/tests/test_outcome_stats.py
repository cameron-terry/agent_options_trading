"""Tests for query_outcome_stats_by_symbol (context track-record pre-load)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from options_agent.contracts.journal import OutcomeEventType, OutcomeRecord
from options_agent.contracts.proposal import ExitPlan, Leg
from options_agent.contracts.state import (
    AssetClass,
    ExitReason,
    LegStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.state.crud import insert_position
from options_agent.state.db import get_connection
from options_agent.state.journal import (
    query_outcome_stats_by_symbol,
    write_outcome_record,
)

_NOW = datetime(2026, 7, 10, 15, 0, 0, tzinfo=UTC)


def _make_position(pos_id: str, underlying: str, strategy: str) -> Position:
    leg = Leg(right="put", side="buy", strike=450.0, expiration=date(2026, 8, 15))
    return Position(
        id=pos_id,
        underlying=underlying,
        strategy=strategy,
        legs=[
            PositionLeg(
                leg=leg, filled_qty=1, avg_fill_price=1.0, status=LegStatus.CLOSED
            )
        ],
        quantity=1,
        entry_net_amount=-1.50,
        current_mark=-0.75,
        marked_at=_NOW,
        unrealized_pnl=0.0,
        realized_pnl=75.0,
        exit_plan=ExitPlan(
            profit_target_pct=0.5, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
        ),
        status=PositionStatus.CLOSED,
        opened_at=_NOW - timedelta(days=10),
        closed_at=_NOW,
        nearest_expiration=date(2026, 8, 15),
        est_max_loss=350.0,
        est_max_profit=150.0,
        opening_order_id="ord-1",
        asset_class=AssetClass.OPTION_STRATEGY,
    )


def _outcome(
    position_id: str,
    realized_pnl: float,
    exit_reason: ExitReason | None,
    *,
    recorded_at: datetime = _NOW,
) -> OutcomeRecord:
    return OutcomeRecord(
        id=str(uuid.uuid4()),
        position_id=position_id,
        event_type=OutcomeEventType.FULL_CLOSE,
        recorded_at=recorded_at,
        contracts_closed=1,
        realized_pnl=realized_pnl,
        fill_price=-0.75,
        closing_order_id="close-1",
        exit_reason=exit_reason,
    )


def test_empty_db_returns_empty_stats(engine) -> None:
    with get_connection(engine) as conn:
        assert query_outcome_stats_by_symbol(conn) == {}


def test_aggregates_per_symbol_and_strategy(engine) -> None:
    with get_connection(engine) as conn:
        insert_position(conn, _make_position("p1", "SPY", "bull_put_spread"))
        insert_position(conn, _make_position("p2", "SPY", "iron_condor"))
        insert_position(conn, _make_position("p3", "QQQ", "bear_call_spread"))
        write_outcome_record(
            conn,
            _outcome(
                "p1",
                75.0,
                ExitReason.PROFIT_TARGET,
                recorded_at=_NOW - timedelta(days=2),
            ),
        )
        write_outcome_record(
            conn,
            _outcome(
                "p2", -120.0, ExitReason.STOP_LOSS, recorded_at=_NOW - timedelta(days=1)
            ),
        )
        write_outcome_record(conn, _outcome("p3", 40.0, ExitReason.DTE))

        stats = query_outcome_stats_by_symbol(conn)

    spy = stats["SPY"]
    assert spy.closed_positions == 2
    assert spy.wins == 1
    assert spy.losses == 1
    assert spy.win_rate == 0.5
    assert spy.total_realized_pnl == -45.0
    assert spy.avg_realized_pnl == -22.5
    assert spy.by_strategy["bull_put_spread"].wins == 1
    assert spy.by_strategy["bull_put_spread"].total_realized_pnl == 75.0
    assert spy.by_strategy["iron_condor"].wins == 0
    # Newest first.
    assert spy.recent_exit_reasons[0] == str(ExitReason.STOP_LOSS)

    qqq = stats["QQQ"]
    assert qqq.closed_positions == 1
    assert qqq.win_rate == 1.0


def test_none_exit_reason_omitted_from_recent(engine) -> None:
    with get_connection(engine) as conn:
        insert_position(conn, _make_position("p1", "SPY", "bull_put_spread"))
        write_outcome_record(conn, _outcome("p1", 10.0, None))
        stats = query_outcome_stats_by_symbol(conn)
    assert stats["SPY"].recent_exit_reasons == []
