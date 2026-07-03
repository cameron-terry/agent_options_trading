"""Tests for WP-9.2: Overview API + distance-to-trigger meter."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from options_agent.contracts.journal import (
    JournalRecord,
    OutcomeEventType,
    OutcomeRecord,
)
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
from options_agent.monitor.exits import profit_target_threshold, stop_loss_threshold
from options_agent.obs.killswitch import set_state
from options_agent.state.crud import insert_position
from options_agent.state.db import build_engine, get_connection, metadata
from options_agent.state.journal import write_journal_record, write_outcome_record
from options_agent.ui.app import create_app
from options_agent.ui.overview import distance_to_trigger, get_activity, get_tiles

_NOW = datetime(2026, 6, 19, 14, 0, 0, tzinfo=UTC)
_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
)


def _make_position(
    *,
    underlying: str = "SPY",
    strategy: str = "bull_put_spread",
    quantity: int = 1,
    unrealized_pnl: float = 0.0,
    exit_plan: ExitPlan | None = _EXIT_PLAN,
    est_max_loss: float = 2225.0,
    est_max_profit: float = 275.0,
    asset_class: AssetClass = AssetClass.OPTION_STRATEGY,
    status: PositionStatus = PositionStatus.OPEN,
    nearest_expiration: date = date(2026, 8, 15),
    legs: list[PositionLeg] | None = None,
) -> Position:
    if legs is None:
        legs = [
            PositionLeg(
                leg=Leg(
                    right="put", side="sell", strike=450.0, expiration=date(2026, 8, 15)
                ),
                filled_qty=quantity,
                avg_fill_price=0.55,
                status=LegStatus.OPEN,
            ),
            PositionLeg(
                leg=Leg(
                    right="put", side="buy", strike=445.0, expiration=date(2026, 8, 15)
                ),
                filled_qty=quantity,
                avg_fill_price=0.0,
                status=LegStatus.OPEN,
            ),
        ]
    return Position(
        id=str(uuid.uuid4()),
        underlying=underlying,
        strategy=strategy,
        legs=legs,
        quantity=quantity,
        entry_net_amount=-275.0,
        current_mark=-150.0,
        marked_at=_NOW,
        unrealized_pnl=unrealized_pnl,
        realized_pnl=None,
        exit_plan=exit_plan,
        status=status,
        opened_at=_NOW,
        closed_at=None,
        nearest_expiration=nearest_expiration,
        est_max_loss=est_max_loss,
        est_max_profit=est_max_profit,
        opening_order_id="open-ord-001",
        asset_class=asset_class,
    )


def _make_proposal(underlying: str = "SPY") -> TradeProposal:
    return TradeProposal(
        action="OPEN",
        underlying=underlying,
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


def _make_journal_record(
    *,
    cycle_id: str = "cycle-001",
    timestamp: datetime = _NOW,
    action: ActionTaken = ActionTaken.OPENED,
    underlying: str | None = "SPY",
    account_equity: float | None = None,
    position_ids: list[str] | None = None,
    rejection_rule_ids: list | None = None,
) -> JournalRecord:
    assembled: dict = {"iv_rank": 65, "regime": "neutral"}
    if account_equity is not None:
        assembled["portfolio"] = {
            "positions": [],
            "account_equity": account_equity,
            "buying_power": 10000.0,
            "options_buying_power": 10000.0,
            "unrealized_pnl": 0.0,
            "realized_pnl_today": 0.0,
            "approval_level": 2,
            "net_dollar_delta": 0.0,
            "net_dollar_gamma": 0.0,
            "net_dollar_theta": 0.0,
            "net_dollar_vega": 0.0,
        }
    context_snapshot = ContextSnapshot(
        assembled_context=assembled,
        context_hash="sha256:abcdef1234567890",
        model_id="claude-sonnet-4-6",
        prompt_version="v1.0.0",
        assembled_at=timestamp,
    )
    decision = Decision(
        proposal=_make_proposal(underlying or "SPY")
        if action == ActionTaken.OPENED
        else None,
        validation_result=None,
        sizing_result=None,
        action_taken=action,
    )
    return JournalRecord(
        cycle_id=cycle_id,
        timestamp=timestamp,
        action_taken=action,
        decision=decision,
        context_snapshot=context_snapshot,
        position_ids=position_ids or [],
        order_ids=[],
        strategy="bull_put_spread" if underlying else None,
        underlying=underlying,
        limits_version="v1.0.0",
        prompt_version="v1.0.0",
        model_id="claude-sonnet-4-6",
        rejection_rule_ids=rejection_rule_ids or [],
    )


def _make_outcome_record(
    *,
    id: str = "outcome-001",
    position_id: str = "pos-001",
    event_type: OutcomeEventType = OutcomeEventType.FULL_CLOSE,
    recorded_at: datetime = _NOW,
    realized_pnl: float = 218.75,
) -> OutcomeRecord:
    return OutcomeRecord(
        id=id,
        position_id=position_id,
        event_type=event_type,
        recorded_at=recorded_at,
        contracts_closed=2,
        realized_pnl=realized_pnl,
        fill_price=-0.47,
        closing_order_id="ord-close-001",
    )


@pytest.fixture
def engine():
    eng = build_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    yield eng
    eng.dispose()


# ---------------------------------------------------------------------------
# distance_to_trigger — must never drift from monitor.exits trigger math
# ---------------------------------------------------------------------------


def test_distance_to_trigger_matches_profit_target_threshold_at_50pct() -> None:
    pos = _make_position(unrealized_pnl=68.75, est_max_profit=275.0)  # 0.5 * 0.5 * 275
    threshold = profit_target_threshold(pos)

    result = distance_to_trigger(pos, today=_NOW)

    assert result is not None
    assert result.direction == "target"
    assert result.pct == pytest.approx(pos.unrealized_pnl / threshold)


def test_distance_to_trigger_matches_profit_target_threshold_at_trigger() -> None:
    pos = _make_position(
        unrealized_pnl=137.50, est_max_profit=275.0
    )  # exactly at threshold
    threshold = profit_target_threshold(pos)

    result = distance_to_trigger(pos, today=_NOW)

    assert result is not None
    assert result.direction == "target"
    assert result.pct == pytest.approx(1.0)
    assert result.pct == pytest.approx(pos.unrealized_pnl / threshold)


def test_distance_to_trigger_matches_stop_loss_threshold() -> None:
    pos = _make_position(
        unrealized_pnl=-556.25, est_max_loss=2225.0
    )  # 0.5 * 2225 * 0.5
    threshold = stop_loss_threshold(pos)

    result = distance_to_trigger(pos, today=_NOW)

    assert result is not None
    assert result.direction == "stop"
    assert result.pct == pytest.approx(pos.unrealized_pnl / threshold)
    assert result.pct == pytest.approx(0.5)


def test_distance_to_trigger_scales_with_quantity_like_monitor() -> None:
    """quantity must multiply the threshold identically to monitor.exits — a
    regression guard for the "1/quantity of intended threshold" class of bug
    that WP-5's stop-loss/profit-target fix (PR #84) addressed."""
    pos_q1 = _make_position(quantity=1, unrealized_pnl=-556.25, est_max_loss=2225.0)
    pos_q3 = _make_position(quantity=3, unrealized_pnl=-556.25, est_max_loss=2225.0)

    result_q1 = distance_to_trigger(pos_q1, today=_NOW)
    result_q3 = distance_to_trigger(pos_q3, today=_NOW)

    assert result_q1 is not None and result_q3 is not None
    assert result_q3.pct == pytest.approx(result_q1.pct / 3)


def test_distance_to_trigger_none_for_equity_position() -> None:
    pos = _make_position(asset_class=AssetClass.EQUITY, exit_plan=None)
    assert distance_to_trigger(pos, today=_NOW) is None


def test_distance_to_trigger_none_when_no_exit_plan() -> None:
    pos = _make_position(exit_plan=None)
    assert distance_to_trigger(pos, today=_NOW) is None


# ---------------------------------------------------------------------------
# _strikes_summary — compact strike string for the positions table
# ---------------------------------------------------------------------------


def _leg(right, side, strike):
    return PositionLeg(
        leg=Leg(right=right, side=side, strike=strike, expiration=date(2026, 8, 15)),
        filled_qty=1,
        avg_fill_price=1.0,
        status=LegStatus.OPEN,
    )


def test_strikes_summary_vertical_spread() -> None:
    from options_agent.ui.overview import _strikes_summary

    pos = _make_position(legs=[_leg("put", "sell", 530.0), _leg("put", "buy", 525.0)])
    assert _strikes_summary(pos) == "530/525"


def test_strikes_summary_iron_condor_preserves_short_then_long_per_side() -> None:
    from options_agent.ui.overview import _strikes_summary

    # Matches the design reference's QQQ example: put spread 485/480, call
    # spread 560/565 — each side lists its short leg first, not numeric order
    # (the call side is short-low/long-high, so short-first is *ascending*).
    pos = _make_position(
        legs=[
            _leg("put", "sell", 485.0),
            _leg("put", "buy", 480.0),
            _leg("call", "sell", 560.0),
            _leg("call", "buy", 565.0),
        ]
    )
    assert _strikes_summary(pos) == "485/480 · 560/565"


def test_strikes_summary_single_leg() -> None:
    from options_agent.ui.overview import _strikes_summary

    pos = _make_position(legs=[_leg("put", "sell", 205.0)])
    assert _strikes_summary(pos) == "205"


def test_strikes_summary_integer_strikes_render_without_decimal() -> None:
    from options_agent.ui.overview import _strikes_summary

    pos = _make_position(legs=[_leg("put", "sell", 205.0), _leg("put", "buy", 200.5)])
    assert _strikes_summary(pos) == "205/200.5"


# ---------------------------------------------------------------------------
# get_tiles
# ---------------------------------------------------------------------------


def test_tiles_account_equity_is_none_with_no_journal_history(engine) -> None:
    with get_connection(engine) as conn:
        tiles = get_tiles(conn, now=_NOW)

    assert tiles.account_equity.value is None
    assert tiles.account_equity.as_of is None


def test_tiles_account_equity_reads_latest_journal_snapshot(engine) -> None:
    with get_connection(engine) as conn:
        write_journal_record(
            conn,
            _make_journal_record(cycle_id="c1", timestamp=_NOW, account_equity=50000.0),
        )
        write_journal_record(
            conn,
            _make_journal_record(
                cycle_id="c2",
                timestamp=_NOW.replace(hour=15),
                account_equity=51305.0,
            ),
        )
        tiles = get_tiles(conn, now=_NOW)

    assert tiles.account_equity.value == 51305.0


def test_tiles_realized_pnl_counts_only_terminal_outcomes(engine) -> None:
    with get_connection(engine) as conn:
        pos = _make_position(status=PositionStatus.CLOSED).model_copy(
            update={"id": "pos-001"}
        )
        insert_position(conn, pos)
        write_outcome_record(
            conn,
            _make_outcome_record(
                id="o1", event_type=OutcomeEventType.FULL_CLOSE, realized_pnl=100.0
            ),
        )
        write_outcome_record(
            conn,
            _make_outcome_record(
                id="o2", event_type=OutcomeEventType.PARTIAL_CLOSE, realized_pnl=50.0
            ),
        )
        write_outcome_record(
            conn,
            _make_outcome_record(
                id="o3", event_type=OutcomeEventType.FULL_CLOSE, realized_pnl=-25.0
            ),
        )
        tiles = get_tiles(conn, now=_NOW)

    assert tiles.realized_pnl.total == pytest.approx(
        125.0
    )  # all outcomes count toward total
    assert tiles.realized_pnl.closed_count == 2  # only FULL_CLOSE counted as "closed"
    assert tiles.realized_pnl.hit_count == 1  # only the +100 FULL_CLOSE is a hit


def test_tiles_unrealized_pnl_sums_open_positions(engine) -> None:
    with get_connection(engine) as conn:
        insert_position(conn, _make_position(unrealized_pnl=100.0))
        insert_position(conn, _make_position(unrealized_pnl=-40.0))
        tiles = get_tiles(conn, now=_NOW)

    assert tiles.unrealized_pnl.total == pytest.approx(60.0)
    assert tiles.unrealized_pnl.open_position_count == 2


def test_tiles_cycles_today_excludes_prior_days(engine) -> None:
    with get_connection(engine) as conn:
        write_journal_record(
            conn, _make_journal_record(cycle_id="today", timestamp=_NOW)
        )
        write_journal_record(
            conn,
            _make_journal_record(
                cycle_id="yesterday",
                timestamp=_NOW.replace(day=_NOW.day - 1),
                action=ActionTaken.NO_ACTION_GATED,
                underlying=None,
            ),
        )
        tiles = get_tiles(conn, now=_NOW)

    assert tiles.cycles_today.total == 1
    assert tiles.cycles_today.by_action == {"OPENED": 1}


# ---------------------------------------------------------------------------
# get_activity
# ---------------------------------------------------------------------------


def test_activity_merges_and_sorts_journal_and_outcomes_newest_first(engine) -> None:
    with get_connection(engine) as conn:
        insert_position(
            conn,
            _make_position(status=PositionStatus.CLOSED).model_copy(
                update={"id": "pos-001"}
            ),
        )
        write_journal_record(conn, _make_journal_record(cycle_id="c1", timestamp=_NOW))
        write_outcome_record(
            conn,
            _make_outcome_record(
                recorded_at=_NOW.replace(hour=15), position_id="pos-001"
            ),
        )
        items = get_activity(conn)

    assert [i.kind for i in items] == ["outcome", "journal"]


def test_activity_respects_limit(engine) -> None:
    with get_connection(engine) as conn:
        for i in range(5):
            write_journal_record(
                conn,
                _make_journal_record(
                    cycle_id=f"c{i}", timestamp=_NOW.replace(hour=10 + i)
                ),
            )
        items = get_activity(conn, limit=2)

    assert len(items) == 2
    assert items[0].timestamp > items[1].timestamp


def test_activity_rejected_headline_includes_rule_ids(engine) -> None:
    from options_agent.contracts.results import ValidationRuleId

    record = _make_journal_record(
        cycle_id="c1",
        timestamp=_NOW,
        action=ActionTaken.REJECTED,
        underlying="AAPL",
        rejection_rule_ids=[ValidationRuleId.EVENT_BLACKOUT],
    )
    with get_connection(engine) as conn:
        write_journal_record(conn, record)
        items = get_activity(conn)

    assert "AAPL" in items[0].headline
    assert "EVENT_BLACKOUT" in items[0].headline


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


def test_get_overview_endpoint_renders_against_populated_db(engine) -> None:
    with get_connection(engine) as conn:
        insert_position(conn, _make_position(unrealized_pnl=100.0))
        write_journal_record(
            conn,
            _make_journal_record(cycle_id="c1", timestamp=_NOW, account_equity=50000.0),
        )
        set_state(conn, KillSwitchState.NONE, set_by="test", reason="init")

    app = create_app(engine=engine)
    client = TestClient(app)

    resp = client.get("/api/overview")

    assert resp.status_code == 200
    body = resp.json()
    assert body["kill_switch"]["state"] == "NONE"
    assert body["tiles"]["unrealized_pnl"]["total"] == pytest.approx(100.0)
    assert body["tiles"]["account_equity"]["value"] == 50000.0
    assert len(body["activity"]) == 1


def test_get_positions_endpoint_includes_distance_to_trigger(engine) -> None:
    with get_connection(engine) as conn:
        insert_position(
            conn, _make_position(unrealized_pnl=137.50, est_max_profit=275.0)
        )

    app = create_app(engine=engine)
    client = TestClient(app)

    resp = client.get("/api/positions")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["distance_to_trigger"]["direction"] == "target"
    assert body[0]["distance_to_trigger"]["pct"] == pytest.approx(1.0)


def test_overview_and_positions_endpoints_make_no_broker_or_market_data_import() -> (
    None
):
    """WP-9 epic invariant: no broker/market-data call anywhere in the console's
    request path. Enforced here as a static import-statement check on the
    overview module — a live-data dependency would show up as an import of
    execution.broker or context.portfolio (which itself requires a live
    FilteredChain; see overview.py's module docstring's prose reference to
    both, which this check must not false-positive on)."""
    import options_agent.ui.overview as overview_module

    source = overview_module.__file__
    assert source is not None
    with open(source) as f:
        import_lines = [
            line for line in f if line.startswith("from ") or line.startswith("import ")
        ]

    banned = ("execution.broker", "context.portfolio", "data.chains")
    for line in import_lines:
        assert not any(name in line for name in banned), line
