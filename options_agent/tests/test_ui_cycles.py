"""Tests for WP-9.3: Decision explorer API (cycle list + trace renderer)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from options_agent.contracts.journal import (
    JournalRecord,
    OutcomeEventType,
    OutcomeRecord,
)
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.results import (
    RejectionReason,
    Severity,
    SizingConstraint,
    SizingResult,
    ValidationResult,
    ValidationRuleId,
)
from options_agent.contracts.state import (
    ActionTaken,
    ContextSnapshot,
    Decision,
    LegStatus,
    Order,
    OrderRole,
    OrderStatus,
    Position,
    PositionLeg,
    PositionStatus,
    ToolCallRecord,
)
from options_agent.state.crud import insert_order, insert_position
from options_agent.state.db import build_engine, get_connection, metadata
from options_agent.state.journal import write_journal_record, write_outcome_record
from options_agent.ui.app import create_app
from options_agent.ui.cycles import get_cycle_detail, get_cycles

_NOW = datetime(2026, 7, 11, 14, 0, 0, tzinfo=UTC)
_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
)


def _make_proposal(underlying: str = "SPY") -> TradeProposal:
    return TradeProposal(
        action="OPEN",
        underlying=underlying,
        strategy="bull_put_spread",
        legs=[Leg(right="put", side="sell", strike=530.0, expiration=date(2026, 8, 7))],
        thesis="Consolidation above the 50-day with rich IV.",
        iv_rationale="IV rank 62 vs realized 28.",
        catalyst_check="No earnings within 37 DTE.",
        conviction=0.72,
        est_max_loss=730.0,
        est_max_profit=270.0,
        breakevens=[527.65],
        net_delta=0.24,
        net_theta=8.5,
        net_vega=-0.3,
        exit_plan=_EXIT_PLAN,
        informed_by=[],
    )


def _make_journal_record(
    *,
    cycle_id: str = "cycle-001",
    timestamp: datetime = _NOW,
    action: ActionTaken = ActionTaken.OPENED,
    underlying: str | None = "SPY",
    position_ids: list[str] | None = None,
    order_ids: list[str] | None = None,
    rejection_rule_ids: list[ValidationRuleId] | None = None,
    validation_result: ValidationResult | None = None,
    sizing_result: SizingResult | None = None,
    tool_calls_transcript: list[ToolCallRecord] | None = None,
    data_quality_flags: list[str] | None = None,
) -> JournalRecord:
    context_snapshot = ContextSnapshot(
        assembled_context={"iv_rank": 62},
        context_hash="sha256:9f3ac21e",
        model_id="claude-sonnet-5",
        prompt_version="v2.1.0",
        assembled_at=timestamp,
        tool_calls_transcript=tool_calls_transcript or [],
    )
    no_proposal_actions = (ActionTaken.NO_ACTION_AGENT, ActionTaken.NO_ACTION_GATED)
    proposal = (
        None if action in no_proposal_actions else _make_proposal(underlying or "SPY")
    )
    decision = Decision(
        proposal=proposal,
        validation_result=validation_result,
        sizing_result=sizing_result,
        action_taken=action,
    )
    return JournalRecord(
        cycle_id=cycle_id,
        timestamp=timestamp,
        action_taken=action,
        decision=decision,
        context_snapshot=context_snapshot,
        position_ids=position_ids or [],
        order_ids=order_ids or [],
        strategy="bull_put_spread" if proposal else None,
        underlying=underlying,
        conviction=proposal.conviction if proposal else None,
        limits_version="v1.0.0",
        prompt_version="v2.1.0",
        model_id="claude-sonnet-5",
        rejection_rule_ids=rejection_rule_ids or [],
        data_quality_flags=data_quality_flags or [],
    )


def _make_position(
    *, position_id: str = "pos-001", underlying: str = "SPY"
) -> Position:
    return Position(
        id=position_id,
        underlying=underlying,
        strategy="bull_put_spread",
        legs=[
            PositionLeg(
                leg=Leg(
                    right="put", side="sell", strike=530.0, expiration=date(2026, 8, 7)
                ),
                filled_qty=2,
                avg_fill_price=2.47,
                status=LegStatus.OPEN,
            )
        ],
        quantity=2,
        entry_net_amount=-270.0,
        current_mark=-132.0,
        marked_at=_NOW,
        unrealized_pnl=276.0,
        realized_pnl=None,
        exit_plan=_EXIT_PLAN,
        status=PositionStatus.OPEN,
        opened_at=_NOW,
        closed_at=None,
        nearest_expiration=date(2026, 8, 7),
        est_max_loss=730.0,
        est_max_profit=270.0,
        opening_order_id="ord-001",
    )


def _make_order(*, order_id: str = "ord-001", position_id: str = "pos-001") -> Order:
    return Order(
        id=order_id,
        broker_order_id=f"broker-{order_id}",
        position_id=position_id,
        role=OrderRole.OPEN,
        status=OrderStatus.FILLED,
        broker_status_raw="filled",
        submitted_at=_NOW,
        filled_at=_NOW,
        limit_price=-1.35,
        legs_filled=[],
        net_fill_price=-1.35,
        filled_qty=2,
    )


@pytest.fixture
def engine():
    eng = build_engine("sqlite:///:memory:")
    metadata.create_all(eng)
    yield eng
    eng.dispose()


# ---------------------------------------------------------------------------
# get_cycles — list filters + default lookback
# ---------------------------------------------------------------------------


def test_get_cycles_returns_newest_first(engine) -> None:
    with get_connection(engine) as conn:
        write_journal_record(conn, _make_journal_record(cycle_id="c1", timestamp=_NOW))
        write_journal_record(
            conn, _make_journal_record(cycle_id="c2", timestamp=_NOW.replace(hour=15))
        )
        items = get_cycles(conn, now=_NOW)

    assert [i.cycle_id for i in items] == ["c2", "c1"]


def test_get_cycles_defaults_date_from_to_30_days_lookback(engine) -> None:
    with get_connection(engine) as conn:
        write_journal_record(
            conn,
            _make_journal_record(cycle_id="old", timestamp=_NOW - timedelta(days=31)),
        )
        write_journal_record(
            conn,
            _make_journal_record(cycle_id="recent", timestamp=_NOW - timedelta(days=1)),
        )
        items = get_cycles(conn, now=_NOW)

    assert [i.cycle_id for i in items] == ["recent"]


def test_get_cycles_explicit_date_from_overrides_default_lookback(engine) -> None:
    with get_connection(engine) as conn:
        write_journal_record(
            conn,
            _make_journal_record(cycle_id="old", timestamp=_NOW - timedelta(days=31)),
        )
        items = get_cycles(conn, now=_NOW, date_from=_NOW - timedelta(days=40))

    assert [i.cycle_id for i in items] == ["old"]


def test_get_cycles_filters_by_symbol_and_action_type(engine) -> None:
    with get_connection(engine) as conn:
        write_journal_record(
            conn, _make_journal_record(cycle_id="spy", timestamp=_NOW, underlying="SPY")
        )
        write_journal_record(
            conn,
            _make_journal_record(
                cycle_id="aapl",
                timestamp=_NOW,
                underlying="AAPL",
                action=ActionTaken.REJECTED,
                rejection_rule_ids=[ValidationRuleId.EVENT_BLACKOUT],
            ),
        )
        items = get_cycles(conn, now=_NOW, symbol="AAPL")

    assert [i.cycle_id for i in items] == ["aapl"]
    assert items[0].action_taken == ActionTaken.REJECTED


def test_get_cycles_list_item_is_slim_projection(engine) -> None:
    with get_connection(engine) as conn:
        write_journal_record(conn, _make_journal_record(cycle_id="c1", timestamp=_NOW))
        items = get_cycles(conn, now=_NOW)

    assert not hasattr(items[0], "tool_calls_transcript")
    assert not hasattr(items[0], "proposal")


# ---------------------------------------------------------------------------
# get_cycle_detail — full trace
# ---------------------------------------------------------------------------


def test_get_cycle_detail_returns_none_when_not_found(engine) -> None:
    with get_connection(engine) as conn:
        assert get_cycle_detail(conn, "missing") is None


def test_get_cycle_detail_renders_proposal_and_transcript(engine) -> None:
    transcript = [
        ToolCallRecord(
            tool_name="get_portfolio_state",
            tool_input={},
            result_json='{"equity": 51029}',
        )
    ]
    with get_connection(engine) as conn:
        write_journal_record(
            conn,
            _make_journal_record(
                cycle_id="c1", timestamp=_NOW, tool_calls_transcript=transcript
            ),
        )
        detail = get_cycle_detail(conn, "c1")

    assert detail is not None
    assert detail.proposal is not None
    assert detail.proposal.thesis.startswith("Consolidation")
    assert len(detail.tool_calls_transcript) == 1
    assert detail.tool_calls_transcript[0].tool_name == "get_portfolio_state"
    assert detail.model_id == "claude-sonnet-5"
    assert detail.prompt_version == "v2.1.0"
    assert detail.limits_version == "v1.0.0"
    assert detail.context_hash == "sha256:9f3ac21e"


def test_get_cycle_detail_exposes_data_quality_flags(engine) -> None:
    with get_connection(engine) as conn:
        write_journal_record(
            conn,
            _make_journal_record(
                cycle_id="c1",
                timestamp=_NOW,
                data_quality_flags=["phantom_net_delta"],
            ),
        )
        detail = get_cycle_detail(conn, "c1")

    assert detail is not None
    assert detail.data_quality_flags == ["phantom_net_delta"]


def test_get_cycle_detail_default_data_quality_flags_empty(engine) -> None:
    with get_connection(engine) as conn:
        write_journal_record(conn, _make_journal_record(cycle_id="c1", timestamp=_NOW))
        detail = get_cycle_detail(conn, "c1")

    assert detail is not None
    assert detail.data_quality_flags == []


def test_get_cycle_detail_pre_wp64_record_has_empty_transcript(engine) -> None:
    """Pre-WP-6.4 records have no transcript field populated — must render
    as an empty list, not error, per the card's acceptance criteria."""
    with get_connection(engine) as conn:
        write_journal_record(conn, _make_journal_record(cycle_id="c1", timestamp=_NOW))
        detail = get_cycle_detail(conn, "c1")

    assert detail is not None
    assert detail.tool_calls_transcript == []


def test_get_cycle_detail_rejected_cycle_exposes_rule_ids_and_reasons(engine) -> None:
    validation_result = ValidationResult(
        passed=False,
        reasons=[
            RejectionReason(
                rule_id=ValidationRuleId.EVENT_BLACKOUT,
                severity=Severity.ERROR,
                human_message="Earnings in 4 days, inside blackout window.",
                observed=4,
                limit=5,
            )
        ],
    )
    with get_connection(engine) as conn:
        write_journal_record(
            conn,
            _make_journal_record(
                cycle_id="c1",
                timestamp=_NOW,
                underlying="AAPL",
                action=ActionTaken.REJECTED,
                rejection_rule_ids=[ValidationRuleId.EVENT_BLACKOUT],
                validation_result=validation_result,
            ),
        )
        detail = get_cycle_detail(conn, "c1")

    assert detail is not None
    assert detail.action_taken == ActionTaken.REJECTED
    assert detail.rejection_rule_ids == [ValidationRuleId.EVENT_BLACKOUT]
    assert detail.validation_result is not None
    assert detail.validation_result.reasons[0].human_message.startswith("Earnings")


def test_get_cycle_detail_no_action_gated_has_no_proposal(engine) -> None:
    with get_connection(engine) as conn:
        write_journal_record(
            conn,
            _make_journal_record(
                cycle_id="c1",
                timestamp=_NOW,
                underlying=None,
                action=ActionTaken.NO_ACTION_GATED,
            ),
        )
        detail = get_cycle_detail(conn, "c1")

    assert detail is not None
    assert detail.proposal is None


def test_get_cycle_detail_sizing_result_included(engine) -> None:
    sizing = SizingResult(
        contracts=2,
        sized_max_loss=730.0,
        sized_max_profit=270.0,
        risk_budget_used=0.014,
        binding_constraint=SizingConstraint.RISK_BUDGET,
    )
    with get_connection(engine) as conn:
        write_journal_record(
            conn,
            _make_journal_record(cycle_id="c1", timestamp=_NOW, sizing_result=sizing),
        )
        detail = get_cycle_detail(conn, "c1")

    assert detail is not None
    assert detail.sizing_result is not None
    assert detail.sizing_result.contracts == 2
    assert detail.sizing_result.binding_constraint == SizingConstraint.RISK_BUDGET


def test_get_cycle_detail_resolves_position_and_order_links_with_outcomes(
    engine,
) -> None:
    with get_connection(engine) as conn:
        insert_position(conn, _make_position())
        insert_order(conn, _make_order())
        write_outcome_record(
            conn,
            OutcomeRecord(
                id="out-001",
                position_id="pos-001",
                event_type=OutcomeEventType.FULL_CLOSE,
                recorded_at=_NOW,
                contracts_closed=2,
                realized_pnl=142.0,
            ),
        )
        write_journal_record(
            conn,
            _make_journal_record(
                cycle_id="c1",
                timestamp=_NOW,
                position_ids=["pos-001"],
                order_ids=["ord-001"],
            ),
        )
        detail = get_cycle_detail(conn, "c1")

    assert detail is not None
    assert len(detail.positions) == 1
    assert detail.positions[0].anomaly is False
    assert detail.positions[0].position is not None
    assert detail.positions[0].position.id == "pos-001"
    assert len(detail.positions[0].outcomes) == 1
    assert detail.positions[0].outcomes[0].realized_pnl == 142.0

    assert len(detail.orders) == 1
    assert detail.orders[0].anomaly is False
    assert detail.orders[0].order is not None
    assert detail.orders[0].order.id == "ord-001"


def test_get_cycle_detail_unresolvable_position_id_is_flagged_as_anomaly(
    engine,
) -> None:
    """A position_id on the journal record that doesn't resolve to a stored
    Position is a broken-history case, not a 500 — surface it explicitly."""
    with get_connection(engine) as conn:
        write_journal_record(
            conn,
            _make_journal_record(
                cycle_id="c1", timestamp=_NOW, position_ids=["missing-pos"]
            ),
        )
        detail = get_cycle_detail(conn, "c1")

    assert detail is not None
    assert len(detail.positions) == 1
    assert detail.positions[0].anomaly is True
    assert detail.positions[0].position is None
    assert detail.positions[0].id == "missing-pos"


def test_get_cycle_detail_unresolvable_order_id_is_flagged_as_anomaly(engine) -> None:
    with get_connection(engine) as conn:
        write_journal_record(
            conn,
            _make_journal_record(
                cycle_id="c1", timestamp=_NOW, order_ids=["missing-ord"]
            ),
        )
        detail = get_cycle_detail(conn, "c1")

    assert detail is not None
    assert len(detail.orders) == 1
    assert detail.orders[0].anomaly is True
    assert detail.orders[0].order is None


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


def test_get_cycles_endpoint_filters_by_query_params(engine) -> None:
    with get_connection(engine) as conn:
        write_journal_record(
            conn, _make_journal_record(cycle_id="spy", timestamp=_NOW, underlying="SPY")
        )
        write_journal_record(
            conn,
            _make_journal_record(cycle_id="aapl", timestamp=_NOW, underlying="AAPL"),
        )

    app = create_app(engine=engine)
    client = TestClient(app)

    resp = client.get("/api/cycles", params={"symbol": "SPY"})

    assert resp.status_code == 200
    body = resp.json()
    assert [c["cycle_id"] for c in body] == ["spy"]


def test_get_cycle_detail_endpoint_200(engine) -> None:
    with get_connection(engine) as conn:
        write_journal_record(conn, _make_journal_record(cycle_id="c1", timestamp=_NOW))

    app = create_app(engine=engine)
    client = TestClient(app)

    resp = client.get("/api/cycles/c1")

    assert resp.status_code == 200
    assert resp.json()["cycle_id"] == "c1"


def test_get_cycle_detail_endpoint_404_when_not_found(engine) -> None:
    app = create_app(engine=engine)
    client = TestClient(app)

    resp = client.get("/api/cycles/does-not-exist")

    assert resp.status_code == 404
