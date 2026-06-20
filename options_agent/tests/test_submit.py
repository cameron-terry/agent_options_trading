"""Tests for WP-1.2: BrokerClient.submit() and orders.py utilities.

All tests mock the Alpaca TradingClient so no network calls are made.
time.sleep and time.monotonic are patched to avoid real delays and to give
deterministic control over the poll-timeout logic.
"""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import pytest
from alpaca.common.exceptions import APIError as AlpacaAPIError

from options_agent.config import Config
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.state import OrderRole, OrderStatus
from options_agent.execution.broker import BrokerClient
from options_agent.execution.orders import (
    build_single_leg_request,
    compute_limit_price,
    occ_symbol,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _config(**kwargs: object) -> Config:
    """Return a Config with small poll window so tests don't actually sleep."""
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
    """Return a BrokerClient with a mocked TradingClient."""
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    mock_client = MagicMock()
    with patch(
        "options_agent.execution.broker.TradingClient",
        return_value=mock_client,
    ):
        broker = BrokerClient(config or _config())
    return broker, mock_client


def _proposal(legs: list[Leg] | None = None) -> TradeProposal:
    """Minimal single-leg TradeProposal for testing."""
    if legs is None:
        legs = [
            Leg(
                right="call",
                side="buy",
                strike=150.0,
                expiration=date(2024, 1, 19),
            )
        ]
    return TradeProposal(
        action="OPEN",
        underlying="AAPL",
        strategy="bull_call_spread",
        legs=legs,
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
        exit_plan=ExitPlan(
            profit_target_pct=0.5,
            stop_loss_max_loss_fraction=0.5,
            time_stop_dte=21,
        ),
        informed_by=[],
    )


def _alpaca_order(
    order_id: str = "broker-id-001",
    status: str = "filled",
    filled_qty: int = 1,
    filled_avg_price: float | None = 1.25,
    submitted_at: datetime | None = None,
    filled_at: datetime | None = None,
) -> MagicMock:
    """Build a mock AlpacaOrder with the given fields."""
    order = MagicMock()
    order.id = order_id
    order.status.value = status
    order.filled_qty = filled_qty
    order.filled_avg_price = filled_avg_price
    order.submitted_at = submitted_at or datetime(2024, 1, 19, 10, 0, tzinfo=UTC)
    order.filled_at = filled_at
    return order


def _make_api_error(
    status_code: int,
    retry_after: str | None = None,
) -> AlpacaAPIError:
    """Build an AlpacaAPIError with a given HTTP status and Retry-After."""
    http_error = MagicMock()
    http_error.response.status_code = status_code
    http_error.response.headers = (
        {"Retry-After": retry_after} if retry_after is not None else {}
    )
    return AlpacaAPIError('{"code":40310000,"message":"rate limited"}', http_error)


# ---------------------------------------------------------------------------
# orders.occ_symbol
# ---------------------------------------------------------------------------


def test_occ_symbol_call() -> None:
    leg = Leg(right="call", side="buy", strike=150.0, expiration=date(2024, 1, 19))
    assert occ_symbol("AAPL", leg) == "AAPL240119C00150000"


def test_occ_symbol_put() -> None:
    leg = Leg(right="put", side="sell", strike=140.0, expiration=date(2024, 3, 15))
    assert occ_symbol("SPY", leg) == "SPY240315P00140000"


def test_occ_symbol_fractional_strike() -> None:
    # $0.50 strike → 0.5 × 1000 = 500 → "00000500"
    leg = Leg(right="call", side="buy", strike=0.5, expiration=date(2025, 6, 20))
    assert occ_symbol("X", leg) == "X250620C00000500"


def test_occ_symbol_round_numbers() -> None:
    # $500.00 → 500000 → "00500000"
    leg = Leg(right="put", side="buy", strike=500.0, expiration=date(2024, 12, 20))
    assert occ_symbol("SPX", leg) == "SPX241220P00500000"


# ---------------------------------------------------------------------------
# orders.compute_limit_price
# ---------------------------------------------------------------------------


def test_compute_limit_price_buy_at_mid() -> None:
    assert compute_limit_price(1.00, 1.50, "buy", 0.0) == 1.25


def test_compute_limit_price_sell_at_mid() -> None:
    assert compute_limit_price(1.00, 1.50, "sell", 0.0) == 1.25


def test_compute_limit_price_buy_with_offset() -> None:
    # mid = 1.25; offset = 0.05 → 1.30
    assert compute_limit_price(1.00, 1.50, "buy", 0.05) == 1.30


def test_compute_limit_price_sell_with_offset() -> None:
    # mid = 1.25; offset = 0.05 → 1.20
    assert compute_limit_price(1.00, 1.50, "sell", 0.05) == 1.20


def test_compute_limit_price_rounding() -> None:
    # mid = 1.20; + 0.009 = 1.209 → rounds to 1.21
    assert compute_limit_price(1.00, 1.40, "buy", 0.009) == 1.21


# ---------------------------------------------------------------------------
# orders.build_single_leg_request
# ---------------------------------------------------------------------------


def test_build_single_leg_request_fields() -> None:
    proposal = _proposal()
    req = build_single_leg_request(proposal, qty=2, limit_price=1.25)
    assert req.symbol == "AAPL240119C00150000"
    assert req.qty == 2
    assert req.limit_price == 1.25
    assert req.side is not None
    assert str(req.side.value) == "buy"
    assert str(req.time_in_force.value) == "day"
    assert req.client_order_id is not None


def test_build_single_leg_request_custom_client_order_id() -> None:
    proposal = _proposal()
    req = build_single_leg_request(
        proposal, qty=1, limit_price=1.00, client_order_id="my-id"
    )
    assert req.client_order_id == "my-id"


def test_build_single_leg_request_multi_leg_raises() -> None:
    legs = [
        Leg(right="call", side="buy", strike=150.0, expiration=date(2024, 1, 19)),
        Leg(right="call", side="sell", strike=160.0, expiration=date(2024, 1, 19)),
    ]
    proposal = _proposal(legs=legs)
    with pytest.raises(ValueError, match="exactly one leg"):
        build_single_leg_request(proposal, qty=1, limit_price=1.00)


# ---------------------------------------------------------------------------
# BrokerClient.submit — single-leg validation
# ---------------------------------------------------------------------------


def test_submit_rejects_multi_leg_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _ = _broker(monkeypatch)
    legs = [
        Leg(right="call", side="buy", strike=150.0, expiration=date(2024, 1, 19)),
        Leg(right="call", side="sell", strike=160.0, expiration=date(2024, 1, 19)),
    ]
    proposal = _proposal(legs=legs)
    with pytest.raises(ValueError, match="single-leg"):
        broker.submit(proposal, qty=1, limit_price=1.25, position_id="pos-1")


# ---------------------------------------------------------------------------
# BrokerClient.submit — fill within timeout
# ---------------------------------------------------------------------------


def test_submit_returns_filled_order(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker(monkeypatch, _config(order_poll_timeout_secs=30.0))
    proposal = _proposal()

    submitted = _alpaca_order(status="new", filled_qty=0, filled_avg_price=None)
    filled = _alpaca_order(
        status="filled",
        filled_qty=1,
        filled_avg_price=1.25,
        filled_at=datetime(2024, 1, 19, 10, 0, 1, tzinfo=UTC),
    )
    mock_client.submit_order.return_value = submitted
    mock_client.get_order_by_id.side_effect = [submitted, filled]

    with patch("options_agent.execution.broker.sleep"):
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 0.0, 0.0, 35.0],
        ):
            order = broker.submit(
                proposal, qty=1, limit_price=1.25, position_id="pos-1"
            )

    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == 1
    assert order.net_fill_price == 1.25
    assert order.limit_price == 1.25
    assert order.position_id == "pos-1"
    assert order.broker_order_id == "broker-id-001"
    assert len(order.legs_filled) == 1
    assert order.legs_filled[0].fill_price == 1.25


# ---------------------------------------------------------------------------
# BrokerClient.submit — working at timeout (no cancel)
# ---------------------------------------------------------------------------


def test_submit_returns_working_at_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, mock_client = _broker(monkeypatch, _config(order_poll_timeout_secs=30.0))
    proposal = _proposal()

    working = _alpaca_order(status="new", filled_qty=0, filled_avg_price=None)
    mock_client.submit_order.return_value = working
    mock_client.get_order_by_id.return_value = working

    with patch("options_agent.execution.broker.sleep"):
        # deadline = 0 + 30 = 30; after first poll remaining = 30 - 31 ≤ 0
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            order = broker.submit(
                proposal, qty=1, limit_price=1.25, position_id="pos-1"
            )

    assert order.status == OrderStatus.WORKING
    assert order.filled_qty == 0
    assert order.net_fill_price is None
    assert order.limit_price == 1.25
    # Broker order must NOT have been cancelled.
    mock_client.cancel_order_by_id.assert_not_called()


# ---------------------------------------------------------------------------
# BrokerClient.submit — partial fill at timeout (remainder stays working)
# ---------------------------------------------------------------------------


def test_submit_returns_partial_fill_at_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, mock_client = _broker(monkeypatch, _config(order_poll_timeout_secs=30.0))
    proposal = _proposal()

    partial = _alpaca_order(
        status="partially_filled",
        filled_qty=1,
        filled_avg_price=1.24,
    )
    mock_client.submit_order.return_value = _alpaca_order(
        status="new", filled_qty=0, filled_avg_price=None
    )
    mock_client.get_order_by_id.return_value = partial

    with patch("options_agent.execution.broker.sleep"):
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            order = broker.submit(
                proposal, qty=2, limit_price=1.25, position_id="pos-1"
            )

    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.filled_qty == 1
    assert order.net_fill_price == 1.24
    assert order.limit_price == 1.25
    # Remainder was NOT cancelled.
    mock_client.cancel_order_by_id.assert_not_called()


# ---------------------------------------------------------------------------
# BrokerClient.submit — broker rejection
# ---------------------------------------------------------------------------


def test_submit_rejected_returns_order_not_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            order = broker.submit(
                proposal, qty=1, limit_price=1.25, position_id="pos-1"
            )

    assert order.status == OrderStatus.REJECTED
    assert order.broker_status_raw == "rejected"
    assert order.filled_qty == 0
    assert order.net_fill_price is None


# ---------------------------------------------------------------------------
# BrokerClient.submit — 429 rate-limit retry
# ---------------------------------------------------------------------------


def test_submit_rate_limit_retries_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, mock_client = _broker(monkeypatch)
    proposal = _proposal()

    filled = _alpaca_order(status="filled", filled_qty=1, filled_avg_price=1.25)
    # First two submit_order calls raise 429; third succeeds.
    mock_client.submit_order.side_effect = [
        _make_api_error(429),
        _make_api_error(429),
        filled,
    ]
    mock_client.get_order_by_id.return_value = filled

    with patch("options_agent.execution.broker.sleep") as mock_sleep:
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            order = broker.submit(
                proposal, qty=1, limit_price=1.25, position_id="pos-1"
            )

    assert order.status == OrderStatus.FILLED
    # sleep called for the two 429 back-offs.
    assert mock_sleep.call_count >= 2


def test_submit_rate_limit_exhausted_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, mock_client = _broker(monkeypatch)
    proposal = _proposal()

    mock_client.submit_order.side_effect = _make_api_error(429)

    with patch("options_agent.execution.broker.sleep"):
        with pytest.raises(AlpacaAPIError):
            broker.submit(proposal, qty=1, limit_price=1.25, position_id="pos-1")


def test_submit_rate_limit_honours_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, mock_client = _broker(monkeypatch)
    proposal = _proposal()

    filled = _alpaca_order(status="filled", filled_qty=1, filled_avg_price=1.25)
    mock_client.submit_order.side_effect = [
        _make_api_error(429, retry_after="7"),
        filled,
    ]
    mock_client.get_order_by_id.return_value = filled

    with patch("options_agent.execution.broker.sleep") as mock_sleep:
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            broker.submit(proposal, qty=1, limit_price=1.25, position_id="pos-1")

    # The sleep after the 429 should use the Retry-After value of 7.0 s.
    sleep_calls = [c.args[0] for c in mock_sleep.call_args_list]
    assert 7.0 in sleep_calls


# ---------------------------------------------------------------------------
# BrokerClient.submit — 401 session expiry and re-auth
# ---------------------------------------------------------------------------


def test_submit_reauths_on_401_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, mock_client = _broker(monkeypatch)
    proposal = _proposal()

    filled = _alpaca_order(status="filled", filled_qty=1, filled_avg_price=1.25)
    mock_client.submit_order.side_effect = [_make_api_error(401), filled]
    mock_client.get_order_by_id.return_value = filled

    new_client = MagicMock()
    new_client.submit_order.return_value = filled
    new_client.get_order_by_id.return_value = filled

    with patch("options_agent.execution.broker.sleep"):
        with patch(
            "options_agent.execution.broker.TradingClient",
            return_value=new_client,
        ):
            with patch(
                "options_agent.execution.broker.monotonic",
                side_effect=[0.0, 31.0],
            ):
                order = broker.submit(
                    proposal, qty=1, limit_price=1.25, position_id="pos-1"
                )

    assert order.status == OrderStatus.FILLED


def test_submit_raises_on_second_401(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker(monkeypatch)
    proposal = _proposal()

    new_client = MagicMock()
    new_client.submit_order.side_effect = _make_api_error(401)
    mock_client.submit_order.side_effect = _make_api_error(401)

    with patch("options_agent.execution.broker.sleep"):
        with patch(
            "options_agent.execution.broker.TradingClient",
            return_value=new_client,
        ):
            with pytest.raises(AlpacaAPIError):
                broker.submit(proposal, qty=1, limit_price=1.25, position_id="pos-1")


# ---------------------------------------------------------------------------
# BrokerClient.submit — fields on the returned Order
# ---------------------------------------------------------------------------


def test_submit_order_has_correct_role(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker(monkeypatch)
    proposal = _proposal()

    filled = _alpaca_order(status="filled", filled_qty=1, filled_avg_price=1.25)
    mock_client.submit_order.return_value = filled
    mock_client.get_order_by_id.return_value = filled

    with patch("options_agent.execution.broker.sleep"):
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            order = broker.submit(
                proposal,
                qty=1,
                limit_price=1.25,
                position_id="pos-99",
                role=OrderRole.CLOSE,
            )

    assert order.role == OrderRole.CLOSE


def test_submit_order_id_is_fresh_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker(monkeypatch)
    proposal = _proposal()

    filled = _alpaca_order(status="filled", filled_qty=1, filled_avg_price=1.25)
    mock_client.submit_order.return_value = filled
    mock_client.get_order_by_id.return_value = filled

    with patch("options_agent.execution.broker.sleep"):
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            o1 = broker.submit(proposal, qty=1, limit_price=1.25, position_id="p1")
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            o2 = broker.submit(proposal, qty=1, limit_price=1.25, position_id="p2")

    assert o1.id != o2.id


def test_submit_uses_distinct_client_order_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each submit() call uses a fresh client_order_id for idempotency."""
    broker, mock_client = _broker(monkeypatch)
    proposal = _proposal()

    filled = _alpaca_order(status="filled", filled_qty=1, filled_avg_price=1.25)
    mock_client.submit_order.return_value = filled
    mock_client.get_order_by_id.return_value = filled

    with patch("options_agent.execution.broker.sleep"):
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            broker.submit(proposal, qty=1, limit_price=1.25, position_id="p1")
        with patch(
            "options_agent.execution.broker.monotonic",
            side_effect=[0.0, 31.0],
        ):
            broker.submit(proposal, qty=1, limit_price=1.25, position_id="p2")

    ids = [ca.args[0].client_order_id for ca in mock_client.submit_order.call_args_list]
    assert ids[0] != ids[1], "each submit() must use a distinct client_order_id"
