from datetime import UTC, date, datetime

from options_agent.contracts import (
    ActionTaken,
    ContextSnapshot,
    Decision,
    ExitPlan,
    Leg,
    LegFill,
    LegStatus,
    Order,
    OrderRole,
    OrderStatus,
    Position,
    PositionLeg,
    PositionStatus,
    TradeProposal,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_LEG = Leg(right="put", side="sell", strike=450.0, expiration=date(2026, 7, 18))
_EXIT_PLAN = ExitPlan(
    profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
)
_NOW = datetime(2026, 6, 7, 14, 30, tzinfo=UTC)
_TODAY = date(2026, 6, 7)


def _make_position_leg(**overrides: object) -> PositionLeg:
    defaults: dict = {
        "leg": _LEG,
        "filled_qty": 5,
        "avg_fill_price": 1.25,
        "status": LegStatus.OPEN,
    }
    defaults.update(overrides)
    return PositionLeg(**defaults)


def _make_position(**overrides: object) -> Position:
    defaults: dict = {
        "id": "pos-001",
        "underlying": "SPY",
        "strategy": "bull_put_spread",
        "legs": [_make_position_leg()],
        "quantity": 5,
        "entry_net_amount": -312.50,  # credit received
        "current_mark": -200.00,
        "marked_at": _NOW,
        "unrealized_pnl": 112.50,
        "realized_pnl": None,
        "exit_plan": _EXIT_PLAN,
        "status": PositionStatus.OPEN,
        "opened_at": _NOW,
        "closed_at": None,
        "nearest_expiration": date(2026, 7, 18),
        "est_max_loss": 2187.50,
        "est_max_profit": 312.50,
        "opening_order_id": "ord-001",
    }
    defaults.update(overrides)
    return Position(**defaults)


def _make_leg_fill(**overrides: object) -> LegFill:
    defaults: dict = {
        "leg": _LEG,
        "filled_qty": 5,
        "fill_price": 1.25,
    }
    defaults.update(overrides)
    return LegFill(**defaults)


def _make_order(**overrides: object) -> Order:
    defaults: dict = {
        "id": "ord-001",
        "broker_order_id": "alpaca-abc-123",
        "position_id": "pos-001",
        "role": OrderRole.OPEN,
        "status": OrderStatus.FILLED,
        "broker_status_raw": "filled",
        "submitted_at": _NOW,
        "filled_at": _NOW,
        "legs_filled": [_make_leg_fill()],
        "net_fill_price": -1.25,
        "filled_qty": 5,
    }
    defaults.update(overrides)
    return Order(**defaults)


def _make_proposal() -> TradeProposal:
    return TradeProposal(
        action="OPEN",
        underlying="SPY",
        strategy="bull_put_spread",
        legs=[_LEG],
        thesis="Bullish bias near support",
        iv_rationale="IV rank at 65th pct — selling premium favourable",
        catalyst_check="No earnings within 30 days",
        conviction=0.7,
        est_max_loss=2187.50,
        est_max_profit=312.50,
        breakevens=[447.50],
        net_delta=0.12,
        net_theta=8.50,
        net_vega=-0.30,
        exit_plan=_EXIT_PLAN,
        informed_by=[],
    )


def _make_decision(**overrides: object) -> Decision:
    defaults: dict = {
        "proposal": _make_proposal(),
        "validation_result": None,
        "sizing_result": None,
        "action_taken": ActionTaken.OPENED,
    }
    defaults.update(overrides)
    return Decision(**defaults)


def _make_context_snapshot(**overrides: object) -> ContextSnapshot:
    defaults: dict = {
        "assembled_context": {"iv_rank": 65, "regime": "neutral", "chain_rows": 12},
        "context_hash": "sha256:abcdef1234567890",
        "model_id": "claude-sonnet-4-6",
        "prompt_version": "v1.0.0",
        "assembled_at": _NOW,
    }
    defaults.update(overrides)
    return ContextSnapshot(**defaults)


# ---------------------------------------------------------------------------
# LegStatus
# ---------------------------------------------------------------------------


def test_leg_status_values() -> None:
    assert set(LegStatus) == {
        LegStatus.OPEN,
        LegStatus.ASSIGNED,
        LegStatus.EXERCISED,
        LegStatus.EXPIRED,
        LegStatus.CLOSED,
    }


# ---------------------------------------------------------------------------
# PositionStatus
# ---------------------------------------------------------------------------


def test_position_status_values() -> None:
    assert set(PositionStatus) == {
        PositionStatus.PENDING_OPEN,
        PositionStatus.OPEN,
        PositionStatus.PENDING_CLOSE,
        PositionStatus.CLOSED,
        PositionStatus.EXPIRED,
        PositionStatus.ASSIGNED,
    }


# ---------------------------------------------------------------------------
# OrderRole
# ---------------------------------------------------------------------------


def test_order_role_values() -> None:
    assert set(OrderRole) == {OrderRole.OPEN, OrderRole.CLOSE, OrderRole.ROLL}


# ---------------------------------------------------------------------------
# OrderStatus
# ---------------------------------------------------------------------------


def test_order_status_values() -> None:
    assert set(OrderStatus) == {
        OrderStatus.PENDING_SUBMIT,
        OrderStatus.WORKING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }


# ---------------------------------------------------------------------------
# PositionLeg
# ---------------------------------------------------------------------------


def test_position_leg_construction() -> None:
    pl = _make_position_leg()
    assert pl.leg == _LEG
    assert pl.filled_qty == 5
    assert pl.avg_fill_price == 1.25
    assert pl.status == LegStatus.OPEN


def test_position_leg_round_trip() -> None:
    pl = _make_position_leg()
    assert PositionLeg.model_validate(pl.model_dump()) == pl


# ---------------------------------------------------------------------------
# LegFill
# ---------------------------------------------------------------------------


def test_leg_fill_construction() -> None:
    lf = _make_leg_fill()
    assert lf.leg == _LEG
    assert lf.filled_qty == 5
    assert lf.fill_price == 1.25


def test_leg_fill_round_trip() -> None:
    lf = _make_leg_fill()
    assert LegFill.model_validate(lf.model_dump()) == lf


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------


def test_position_construction() -> None:
    pos = _make_position()
    assert pos.id == "pos-001"
    assert pos.underlying == "SPY"
    assert pos.quantity == 5
    assert pos.entry_net_amount == -312.50
    assert pos.status == PositionStatus.OPEN
    assert pos.realized_pnl is None
    assert pos.closed_at is None


def test_position_credit_sign_convention() -> None:
    pos = _make_position(entry_net_amount=-500.0)
    assert pos.entry_net_amount < 0  # credit received → negative


def test_position_debit_sign_convention() -> None:
    pos = _make_position(entry_net_amount=300.0)
    assert pos.entry_net_amount > 0  # debit paid → positive


def test_position_round_trip_json() -> None:
    pos = _make_position()
    restored = Position.model_validate_json(pos.model_dump_json())
    assert restored == pos


def test_position_round_trip_dict() -> None:
    pos = _make_position()
    restored = Position.model_validate(pos.model_dump())
    assert restored == pos


def test_position_pending_close_status() -> None:
    pos = _make_position(status=PositionStatus.PENDING_CLOSE)
    assert pos.status == PositionStatus.PENDING_CLOSE


def test_position_closed_has_realized_pnl() -> None:
    pos = _make_position(
        status=PositionStatus.CLOSED,
        closed_at=_NOW,
        realized_pnl=150.0,
    )
    assert pos.realized_pnl == 150.0
    assert pos.closed_at == _NOW


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


def test_order_construction() -> None:
    order = _make_order()
    assert order.broker_order_id == "alpaca-abc-123"
    assert order.position_id == "pos-001"
    assert order.role == OrderRole.OPEN
    assert order.status == OrderStatus.FILLED
    assert order.broker_status_raw == "filled"
    assert order.filled_qty == 5


def test_order_closing_role() -> None:
    order = _make_order(role=OrderRole.CLOSE, broker_order_id="alpaca-xyz-456")
    assert order.role == OrderRole.CLOSE


def test_order_rejected_status() -> None:
    order = _make_order(
        status=OrderStatus.REJECTED,
        broker_status_raw="rejected",
        filled_at=None,
        net_fill_price=None,
        filled_qty=0,
        legs_filled=[],
    )
    assert order.status == OrderStatus.REJECTED
    assert order.filled_at is None


def test_order_round_trip() -> None:
    order = _make_order()
    assert Order.model_validate(order.model_dump()) == order


def test_order_no_broker_order_id_on_position() -> None:
    # Confirm the design: Position has opening_order_id (our ID), not broker_order_id.
    pos = _make_position()
    assert hasattr(pos, "opening_order_id")
    assert not hasattr(pos, "broker_order_id")


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def test_decision_construction() -> None:
    d = _make_decision()
    assert d.proposal is not None
    assert d.action_taken == ActionTaken.OPENED
    assert d.validation_result is None
    assert d.sizing_result is None


def test_decision_no_action() -> None:
    d = _make_decision(proposal=None, action_taken=ActionTaken.NO_ACTION_AGENT)
    assert d.proposal is None
    assert d.action_taken == ActionTaken.NO_ACTION_AGENT


def test_decision_round_trip() -> None:
    d = _make_decision()
    assert Decision.model_validate(d.model_dump()) == d


# ---------------------------------------------------------------------------
# ContextSnapshot
# ---------------------------------------------------------------------------


def test_context_snapshot_construction() -> None:
    cs = _make_context_snapshot()
    assert cs.context_hash == "sha256:abcdef1234567890"
    assert cs.model_id == "claude-sonnet-4-6"
    assert cs.prompt_version == "v1.0.0"
    assert cs.assembled_context["iv_rank"] == 65


def test_context_snapshot_round_trip() -> None:
    cs = _make_context_snapshot()
    assert ContextSnapshot.model_validate(cs.model_dump()) == cs


def test_context_snapshot_json_round_trip() -> None:
    cs = _make_context_snapshot()
    assert ContextSnapshot.model_validate_json(cs.model_dump_json()) == cs
