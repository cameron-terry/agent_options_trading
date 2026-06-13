"""Tests for WP-1.3: multi-leg order construction and BrokerClient.submit_multi_leg().

All broker tests mock TradingClient — no network calls are made.
time.sleep and time.monotonic are patched for deterministic poll-timeout behaviour.
"""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import pytest
from alpaca.trading.enums import OrderClass, OrderSide, OrderType

from options_agent.config import Config
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.state import OrderRole, OrderStatus
from options_agent.execution.broker import BrokerClient
from options_agent.execution.orders import (
    build_multi_leg_request,
    compute_multi_leg_limit_price,
)

# ---------------------------------------------------------------------------
# Helpers shared across sections
# ---------------------------------------------------------------------------


def _config(**kwargs: object) -> Config:
    defaults: dict[str, object] = {
        "order_poll_interval_secs": 0.001,
        "order_poll_timeout_secs": 0.001,
    }
    defaults.update(kwargs)
    return Config(**defaults)  # type: ignore[arg-type]


def _broker(
    monkeypatch: pytest.MonkeyPatch,
    config: Config | None = None,
) -> tuple[BrokerClient, MagicMock]:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    mock_client = MagicMock()
    with patch(
        "options_agent.execution.broker.TradingClient",
        return_value=mock_client,
    ):
        broker = BrokerClient(config or _config())
    return broker, mock_client


def _legs_bull_call_spread() -> list[Leg]:
    """Bull call spread: buy 150C, sell 160C (net debit)."""
    return [
        Leg(right="call", side="buy", strike=150.0, expiration=date(2024, 1, 19)),
        Leg(right="call", side="sell", strike=160.0, expiration=date(2024, 1, 19)),
    ]


def _legs_bear_call_spread() -> list[Leg]:
    """Bear call spread: sell 150C, buy 160C (net credit)."""
    return [
        Leg(right="call", side="sell", strike=150.0, expiration=date(2024, 1, 19)),
        Leg(right="call", side="buy", strike=160.0, expiration=date(2024, 1, 19)),
    ]


def _proposal(legs: list[Leg] | None = None) -> TradeProposal:
    return TradeProposal(
        action="OPEN",
        underlying="AAPL",
        strategy="bull_call_spread",
        legs=legs or _legs_bull_call_spread(),
        thesis="test",
        iv_rationale="test iv",
        catalyst_check="no earnings",
        conviction=0.6,
        est_max_loss=100.0,
        est_max_profit=200.0,
        breakevens=[155.0],
        net_delta=0.3,
        net_theta=-0.5,
        net_vega=0.2,
        exit_plan=ExitPlan(profit_target_pct=0.5, stop_loss_mult=2.0, time_stop_dte=21),
        informed_by=[],
    )


def _alpaca_order(
    order_id: str = "broker-id-mleg-001",
    status: str = "filled",
    filled_qty: int = 1,
    filled_avg_price: float | None = -0.50,
    submitted_at: datetime | None = None,
    filled_at: datetime | None = None,
) -> MagicMock:
    order = MagicMock()
    order.id = order_id
    order.status.value = status
    order.filled_qty = filled_qty
    order.filled_avg_price = filled_avg_price
    order.submitted_at = submitted_at or datetime(2024, 1, 19, 10, 0, tzinfo=UTC)
    order.filled_at = filled_at
    return order


# ---------------------------------------------------------------------------
# compute_multi_leg_limit_price — sign convention and basic arithmetic
# ---------------------------------------------------------------------------


def test_compute_multi_leg_debit_spread() -> None:
    # Buy 150C mid=2.00, sell 160C mid=1.00 → net = +2.00 - 1.00 = +1.00 (debit)
    legs = _legs_bull_call_spread()
    quotes = [(1.80, 2.20), (0.80, 1.20)]  # mids: 2.00, 1.00
    result = compute_multi_leg_limit_price(legs, quotes)
    assert result == 1.00


def test_compute_multi_leg_credit_spread() -> None:
    # Sell 150C mid=2.00, buy 160C mid=1.00 → net = -2.00 + 1.00 = -1.00 (credit)
    legs = _legs_bear_call_spread()
    quotes = [(1.80, 2.20), (0.80, 1.20)]  # mids: 2.00, 1.00
    result = compute_multi_leg_limit_price(legs, quotes)
    assert result == -1.00


def test_compute_multi_leg_buy_only_all_debit() -> None:
    legs = [
        Leg(right="call", side="buy", strike=150.0, expiration=date(2024, 1, 19)),
        Leg(right="put", side="buy", strike=140.0, expiration=date(2024, 1, 19)),
    ]
    quotes = [(0.90, 1.10), (0.40, 0.60)]  # mids: 1.00, 0.50
    result = compute_multi_leg_limit_price(legs, quotes)
    assert result == 1.50  # +1.00 + 0.50


def test_compute_multi_leg_sell_only_all_credit() -> None:
    legs = [
        Leg(right="call", side="sell", strike=160.0, expiration=date(2024, 1, 19)),
        Leg(right="put", side="sell", strike=130.0, expiration=date(2024, 1, 19)),
    ]
    quotes = [(0.90, 1.10), (0.40, 0.60)]  # mids: 1.00, 0.50
    result = compute_multi_leg_limit_price(legs, quotes)
    assert result == -1.50  # -1.00 - 0.50


def test_compute_multi_leg_iron_condor_net_credit() -> None:
    # Sell 145P, buy 140P (put spread credit), sell 155C, buy 160C (call spread credit)
    legs = [
        Leg(right="put", side="sell", strike=145.0, expiration=date(2024, 1, 19)),
        Leg(right="put", side="buy", strike=140.0, expiration=date(2024, 1, 19)),
        Leg(right="call", side="sell", strike=155.0, expiration=date(2024, 1, 19)),
        Leg(right="call", side="buy", strike=160.0, expiration=date(2024, 1, 19)),
    ]
    quotes = [
        (0.90, 1.10),  # 145P mid=1.00 sell
        (0.40, 0.60),  # 140P mid=0.50 buy
        (0.90, 1.10),  # 155C mid=1.00 sell
        (0.40, 0.60),  # 160C mid=0.50 buy
    ]
    # net = -1.00 + 0.50 - 1.00 + 0.50 = -1.00
    result = compute_multi_leg_limit_price(legs, quotes)
    assert result == -1.00


# ---------------------------------------------------------------------------
# compute_multi_leg_limit_price — conservative rounding
# ---------------------------------------------------------------------------


def test_compute_multi_leg_debit_rounds_down() -> None:
    # net = 1.005 (exactly) — should floor to 1.00, not round to 1.01
    # Buy mid=2.005, sell mid=1.00 → net = +2.005 - 1.00 = 1.005
    legs = _legs_bull_call_spread()
    quotes = [(2.00, 2.01), (0.90, 1.10)]  # mids: 2.005, 1.00
    result = compute_multi_leg_limit_price(legs, quotes)
    assert result == 1.00  # floor, not round; pay slightly less than mid


def test_compute_multi_leg_credit_floors_more_negative() -> None:
    # net = -1.005 — should floor (more negative) to -1.01, receiving more than mid
    legs = _legs_bear_call_spread()
    quotes = [(2.00, 2.01), (0.90, 1.10)]  # sell mid=2.005, buy mid=1.00
    result = compute_multi_leg_limit_price(legs, quotes)
    assert result == -1.01  # floor toward -∞, we receive more credit


# ---------------------------------------------------------------------------
# compute_multi_leg_limit_price — ratio > 1
# ---------------------------------------------------------------------------


def test_compute_multi_leg_ratio_multiplies_contribution() -> None:
    # 1×2 ratio spread: buy 1× 150C, sell 2× 155C
    exp = date(2024, 1, 19)
    legs = [
        Leg(right="call", side="buy", strike=150.0, expiration=exp, ratio=1),
        Leg(right="call", side="sell", strike=155.0, expiration=exp, ratio=2),
    ]
    quotes = [(1.80, 2.20), (0.90, 1.10)]  # mids: 2.00, 1.00
    # net = +2.00×1 - 1.00×2 = 2.00 - 2.00 = 0.00
    result = compute_multi_leg_limit_price(legs, quotes)
    assert result == 0.00


def test_compute_multi_leg_ratio_2_net_credit() -> None:
    # sell 2× 155C, buy 1× 150C — reversed
    exp = date(2024, 1, 19)
    legs = [
        Leg(right="call", side="sell", strike=155.0, expiration=exp, ratio=2),
        Leg(right="call", side="buy", strike=150.0, expiration=exp, ratio=1),
    ]
    quotes = [(0.90, 1.10), (1.80, 2.20)]  # mids: 1.00, 2.00
    # net = -1.00×2 + 2.00×1 = -2.00 + 2.00 = 0.00
    result = compute_multi_leg_limit_price(legs, quotes)
    assert result == 0.00


# ---------------------------------------------------------------------------
# compute_multi_leg_limit_price — validation errors
# ---------------------------------------------------------------------------


def test_compute_multi_leg_quotes_length_mismatch_raises() -> None:
    legs = _legs_bull_call_spread()
    with pytest.raises(ValueError, match="quotes length"):
        compute_multi_leg_limit_price(legs, [(1.0, 2.0)])


def test_compute_multi_leg_zero_bid_raises() -> None:
    legs = _legs_bull_call_spread()
    # ask ≤ 0 is invalid
    with pytest.raises(ValueError, match="invalid quote"):
        compute_multi_leg_limit_price(legs, [(0.0, 0.0), (0.90, 1.10)])


def test_compute_multi_leg_inverted_spread_raises() -> None:
    legs = _legs_bull_call_spread()
    with pytest.raises(ValueError, match="pathological quote"):
        compute_multi_leg_limit_price(legs, [(1.50, 1.00), (0.90, 1.10)])


def test_compute_multi_leg_bid_equals_ask_raises() -> None:
    legs = _legs_bull_call_spread()
    with pytest.raises(ValueError, match="pathological quote"):
        compute_multi_leg_limit_price(legs, [(1.00, 1.00), (0.90, 1.10)])


# ---------------------------------------------------------------------------
# build_multi_leg_request — structure and field values
# ---------------------------------------------------------------------------


def test_build_multi_leg_request_order_class_and_type() -> None:
    proposal = _proposal()
    req = build_multi_leg_request(proposal, qty=2, limit_price=-0.50)
    assert req.order_class == OrderClass.MLEG
    # Statically LIMIT; no code path produces a market order.
    assert req.type == OrderType.LIMIT


def test_build_multi_leg_request_no_market_order() -> None:
    # Invariant: the request type must never be MARKET.
    proposal = _proposal()
    req = build_multi_leg_request(proposal, qty=1, limit_price=1.00)
    assert req.type != OrderType.MARKET


def test_build_multi_leg_request_qty_and_limit_price() -> None:
    proposal = _proposal()
    req = build_multi_leg_request(proposal, qty=5, limit_price=-0.75)
    assert req.qty == 5
    assert req.limit_price == -0.75


def test_build_multi_leg_request_leg_count() -> None:
    proposal = _proposal(_legs_bull_call_spread())
    req = build_multi_leg_request(proposal, qty=1, limit_price=1.00)
    assert len(req.legs) == 2  # type: ignore[arg-type]


def test_build_multi_leg_request_occ_symbols() -> None:
    proposal = _proposal()
    req = build_multi_leg_request(proposal, qty=1, limit_price=1.00)
    assert req.legs is not None
    symbols = [leg.symbol for leg in req.legs]
    assert "AAPL240119C00150000" in symbols  # buy 150C
    assert "AAPL240119C00160000" in symbols  # sell 160C


def test_build_multi_leg_request_side_mapping() -> None:
    proposal = _proposal()
    req = build_multi_leg_request(proposal, qty=1, limit_price=1.00)
    assert req.legs is not None
    sides = {leg.symbol: leg.side for leg in req.legs}
    assert sides["AAPL240119C00150000"] == OrderSide.BUY
    assert sides["AAPL240119C00160000"] == OrderSide.SELL


def test_build_multi_leg_request_ratio_qty_default() -> None:
    # Default ratio=1 → ratio_qty=1.0
    proposal = _proposal()
    req = build_multi_leg_request(proposal, qty=1, limit_price=1.00)
    assert req.legs is not None
    for leg in req.legs:
        assert leg.ratio_qty == 1.0


def test_build_multi_leg_request_ratio_qty_non_unit() -> None:
    # ratio=2 → ratio_qty=2.0; Alpaca multiplies by qty for actual contract count
    exp = date(2024, 1, 19)
    legs = [
        Leg(right="call", side="buy", strike=150.0, expiration=exp, ratio=1),
        Leg(right="call", side="sell", strike=155.0, expiration=exp, ratio=2),
    ]
    proposal = _proposal(legs)
    req = build_multi_leg_request(proposal, qty=5, limit_price=0.00)
    assert req.legs is not None
    ratio_by_symbol = {leg.symbol: leg.ratio_qty for leg in req.legs}
    assert ratio_by_symbol["AAPL240119C00150000"] == 1.0
    assert ratio_by_symbol["AAPL240119C00155000"] == 2.0
    # qty=5 is the base; Alpaca computes 5×1=5 and 5×2=10 contracts respectively.
    assert req.qty == 5


def test_build_multi_leg_request_custom_client_order_id() -> None:
    proposal = _proposal()
    req = build_multi_leg_request(
        proposal, qty=1, limit_price=1.00, client_order_id="my-coid"
    )
    assert req.client_order_id == "my-coid"


def test_build_multi_leg_request_auto_client_order_id() -> None:
    proposal = _proposal()
    req = build_multi_leg_request(proposal, qty=1, limit_price=1.00)
    assert req.client_order_id is not None
    assert len(req.client_order_id) > 0


def test_build_multi_leg_request_single_leg_raises() -> None:
    legs = [Leg(right="call", side="buy", strike=150.0, expiration=date(2024, 1, 19))]
    proposal = _proposal(legs)
    with pytest.raises(ValueError, match="at least 2 legs"):
        build_multi_leg_request(proposal, qty=1, limit_price=1.00)


def test_build_multi_leg_request_five_legs_raises() -> None:
    exp = date(2024, 1, 19)
    legs = [
        Leg(right="call", side="buy", strike=float(100 + i * 5), expiration=exp)
        for i in range(5)
    ]
    proposal = _proposal(legs)
    with pytest.raises(ValueError, match="at most 4 legs"):
        build_multi_leg_request(proposal, qty=1, limit_price=1.00)


# ---------------------------------------------------------------------------
# BrokerClient.submit_multi_leg — fill / status outcomes
# ---------------------------------------------------------------------------


def test_submit_multi_leg_filled(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker(monkeypatch, _config(order_poll_timeout_secs=30.0))
    proposal = _proposal()

    submitted = _alpaca_order(status="new", filled_qty=0, filled_avg_price=None)
    filled = _alpaca_order(
        status="filled",
        filled_qty=2,
        filled_avg_price=-0.55,
        filled_at=datetime(2024, 1, 19, 10, 0, 1, tzinfo=UTC),
    )
    mock_client.submit_order.return_value = submitted
    mock_client.get_order_by_id.side_effect = [submitted, filled]

    with patch("options_agent.execution.broker.sleep"):
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 0.0, 0.0, 35.0],
        ):
            order = broker.submit_multi_leg(
                proposal, qty=2, limit_price=-0.55, position_id="pos-ml-1"
            )

    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == 2
    assert order.net_fill_price == -0.55
    assert order.limit_price == -0.55
    assert order.position_id == "pos-ml-1"
    assert order.broker_order_id == "broker-id-mleg-001"


def test_submit_multi_leg_working_at_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker(monkeypatch, _config(order_poll_timeout_secs=30.0))
    proposal = _proposal()

    working = _alpaca_order(status="new", filled_qty=0, filled_avg_price=None)
    mock_client.submit_order.return_value = working
    mock_client.get_order_by_id.return_value = working

    with patch("options_agent.execution.broker.sleep"):
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            order = broker.submit_multi_leg(
                proposal, qty=1, limit_price=-0.50, position_id="pos-ml-2"
            )

    assert order.status == OrderStatus.WORKING
    assert order.filled_qty == 0
    assert order.net_fill_price is None
    mock_client.cancel_order_by_id.assert_not_called()


def test_submit_multi_leg_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker(monkeypatch)
    proposal = _proposal()

    rejected = _alpaca_order(status="rejected", filled_qty=0, filled_avg_price=None)
    mock_client.submit_order.return_value = rejected
    mock_client.get_order_by_id.return_value = rejected

    with patch("options_agent.execution.broker.sleep"):
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            order = broker.submit_multi_leg(
                proposal, qty=1, limit_price=-0.50, position_id="pos-ml-3"
            )

    assert order.status == OrderStatus.REJECTED
    assert order.broker_status_raw == "rejected"
    assert order.filled_qty == 0
    assert order.net_fill_price is None


def test_submit_multi_leg_submits_mleg_request(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker(monkeypatch)
    proposal = _proposal()

    filled = _alpaca_order(status="filled", filled_qty=1, filled_avg_price=-0.50)
    mock_client.submit_order.return_value = filled
    mock_client.get_order_by_id.return_value = filled

    with patch("options_agent.execution.broker.sleep"):
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            broker.submit_multi_leg(
                proposal, qty=1, limit_price=-0.50, position_id="pos-1"
            )

    req = mock_client.submit_order.call_args[0][0]
    assert req.order_class == OrderClass.MLEG
    assert req.type == OrderType.LIMIT
    assert len(req.legs) == 2


def test_submit_multi_leg_no_cancel_on_working(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker(monkeypatch)
    proposal = _proposal()

    working = _alpaca_order(status="new", filled_qty=0, filled_avg_price=None)
    mock_client.submit_order.return_value = working
    mock_client.get_order_by_id.return_value = working

    with patch("options_agent.execution.broker.sleep"):
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            broker.submit_multi_leg(
                proposal, qty=1, limit_price=-0.50, position_id="pos-1"
            )

    mock_client.cancel_order_by_id.assert_not_called()


def test_submit_multi_leg_single_leg_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, _ = _broker(monkeypatch)
    legs = [Leg(right="call", side="buy", strike=150.0, expiration=date(2024, 1, 19))]
    proposal = _proposal(legs)
    with pytest.raises(ValueError, match="at least 2 legs"):
        broker.submit_multi_leg(proposal, qty=1, limit_price=1.00, position_id="pos-1")


def test_submit_multi_leg_role_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker(monkeypatch)
    proposal = _proposal()

    filled = _alpaca_order(status="filled", filled_qty=1, filled_avg_price=-0.50)
    mock_client.submit_order.return_value = filled
    mock_client.get_order_by_id.return_value = filled

    with patch("options_agent.execution.broker.sleep"):
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            order = broker.submit_multi_leg(
                proposal,
                qty=1,
                limit_price=-0.50,
                position_id="pos-close",
                role=OrderRole.CLOSE,
            )

    assert order.role == OrderRole.CLOSE


def test_submit_multi_leg_legs_filled_empty_at_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Per contract: per-leg fills are populated by WP-1.4 reconcile, not submit+poll.
    broker, mock_client = _broker(monkeypatch)
    proposal = _proposal()

    filled = _alpaca_order(status="filled", filled_qty=1, filled_avg_price=-0.50)
    mock_client.submit_order.return_value = filled
    mock_client.get_order_by_id.return_value = filled

    with patch("options_agent.execution.broker.sleep"):
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            order = broker.submit_multi_leg(
                proposal, qty=1, limit_price=-0.50, position_id="pos-1"
            )

    assert order.legs_filled == []


def test_submit_multi_leg_distinct_client_order_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, mock_client = _broker(monkeypatch)
    proposal = _proposal()

    filled = _alpaca_order(status="filled", filled_qty=1, filled_avg_price=-0.50)
    mock_client.submit_order.return_value = filled
    mock_client.get_order_by_id.return_value = filled

    with patch("options_agent.execution.broker.sleep"):
        with patch("options_agent.execution.broker.monotonic", side_effect=[0.0, 31.0]):
            broker.submit_multi_leg(
                proposal, qty=1, limit_price=-0.50, position_id="p1"
            )
        with patch("options_agent.execution.broker.monotonic", side_effect=[0.0, 31.0]):
            broker.submit_multi_leg(
                proposal, qty=1, limit_price=-0.50, position_id="p2"
            )

    ids = [ca.args[0].client_order_id for ca in mock_client.submit_order.call_args_list]
    assert ids[0] != ids[1]
