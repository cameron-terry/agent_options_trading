from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from alpaca.common.exceptions import APIError

from options_agent.config import Config
from options_agent.contracts.state import Order, OrderRole, OrderStatus
from options_agent.execution.broker import BrokerClient

_NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def _order(
    order_id: str = "local-001",
    broker_order_id: str = "broker-abc",
    position_id: str = "pos-001",
    role: OrderRole = OrderRole.OPEN,
    status: OrderStatus = OrderStatus.WORKING,
) -> Order:
    return Order(
        id=order_id,
        broker_order_id=broker_order_id,
        position_id=position_id,
        role=role,
        status=status,
        broker_status_raw="new",
        submitted_at=_NOW,
        filled_at=None,
        limit_price=1.25,
        legs_filled=[],
        net_fill_price=None,
        filled_qty=0,
    )


def _alpaca_order(
    broker_id: str = "broker-abc",
    status: str = "canceled",
    filled_qty: int = 0,
    filled_avg_price: float | None = None,
    filled_at: datetime | None = None,
) -> MagicMock:
    o = MagicMock()
    o.id = broker_id
    o.status.value = status
    o.filled_qty = filled_qty
    o.filled_avg_price = filled_avg_price
    o.filled_at = filled_at
    return o


def _api_error(status_code: int, retry_after: str | None = None) -> APIError:
    """Build an APIError with a specific HTTP status code for testing."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.headers = {"Retry-After": retry_after} if retry_after else {}
    mock_http_error = MagicMock()
    mock_http_error.response = mock_response
    return APIError("test error body", mock_http_error)


def _config(paper: bool = True) -> Config:
    return Config(alpaca_paper=paper)


# ---------------------------------------------------------------------------
# Missing credentials — fail at construction, not mid-cycle
# ---------------------------------------------------------------------------


def test_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    with pytest.raises(OSError, match="ALPACA_API_KEY"):
        BrokerClient(_config())


def test_missing_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(OSError, match="ALPACA_SECRET_KEY"):
        BrokerClient(_config())


def test_missing_both_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(OSError, match="ALPACA_API_KEY"):
        BrokerClient(_config())


# ---------------------------------------------------------------------------
# is_paper reflects the config flag
# ---------------------------------------------------------------------------


def test_is_paper_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    with patch("options_agent.execution.broker.TradingClient"):
        broker = BrokerClient(_config(paper=True))
    assert broker.is_paper is True


def test_is_paper_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    with patch("options_agent.execution.broker.TradingClient"):
        broker = BrokerClient(_config(paper=False))
    assert broker.is_paper is False


# ---------------------------------------------------------------------------
# TradingClient init failure → RuntimeError with no key leakage
# ---------------------------------------------------------------------------


def test_init_failure_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "bad_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "bad_secret")
    with patch(
        "options_agent.execution.broker.TradingClient",
        side_effect=Exception("sdk internal error"),
    ):
        with pytest.raises(RuntimeError, match="check credentials are valid"):
            BrokerClient(_config())


def test_error_message_does_not_leak_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "super_secret_key_value")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "super_secret_value")
    with patch(
        "options_agent.execution.broker.TradingClient",
        side_effect=Exception("sdk internal error"),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            BrokerClient(_config())
    error_text = str(exc_info.value)
    assert "super_secret_key_value" not in error_text
    assert "super_secret_value" not in error_text


# ---------------------------------------------------------------------------
# get_account delegates to TradingClient
# ---------------------------------------------------------------------------


def test_get_account_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    mock_account = MagicMock()
    mock_client = MagicMock()
    mock_client.get_account.return_value = mock_account
    with patch(
        "options_agent.execution.broker.TradingClient", return_value=mock_client
    ):
        broker = BrokerClient(_config())
        result = broker.get_account()
    assert result is mock_account
    mock_client.get_account.assert_called_once()


# ---------------------------------------------------------------------------
# cancel() — helpers shared across cancel tests
# ---------------------------------------------------------------------------


def _broker_with_mock_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[BrokerClient, MagicMock]:
    """Return (BrokerClient, mock_trading_client) with env vars set."""
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    mock_client = MagicMock()
    with patch(
        "options_agent.execution.broker.TradingClient", return_value=mock_client
    ):
        broker = BrokerClient(_config())
    return broker, mock_client


# ---------------------------------------------------------------------------
# cancel() — successful cancel
# ---------------------------------------------------------------------------


def test_cancel_success_returns_cancelled_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, mock_client = _broker_with_mock_client(monkeypatch)
    mock_client.cancel_order_by_id.return_value = None  # 204 No Content
    mock_client.get_order_by_id.return_value = _alpaca_order(status="canceled")

    result = broker.cancel(_order())

    assert result.status == OrderStatus.CANCELLED
    assert result.broker_status_raw == "canceled"
    mock_client.cancel_order_by_id.assert_called_once_with("broker-abc")


def test_cancel_preserves_local_order_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker_with_mock_client(monkeypatch)
    mock_client.cancel_order_by_id.return_value = None
    mock_client.get_order_by_id.return_value = _alpaca_order(status="canceled")

    local = _order(
        order_id="my-local-id",
        broker_order_id="broker-abc",
        position_id="pos-xyz",
        role=OrderRole.CLOSE,
    )
    result = broker.cancel(local)

    assert result.id == "my-local-id"
    assert result.broker_order_id == "broker-abc"
    assert result.position_id == "pos-xyz"
    assert result.role == OrderRole.CLOSE
    assert result.submitted_at == _NOW
    assert result.limit_price == 1.25


def test_cancel_fetches_state_after_successful_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, mock_client = _broker_with_mock_client(monkeypatch)
    mock_client.cancel_order_by_id.return_value = None
    mock_client.get_order_by_id.return_value = _alpaca_order(status="canceled")

    broker.cancel(_order())

    # cancel_order_by_id returns None (204); must follow up with get_order_by_id
    mock_client.get_order_by_id.assert_called_once_with("broker-abc")


# ---------------------------------------------------------------------------
# cancel() — fill race: cancel arrives after fill
# ---------------------------------------------------------------------------


def test_cancel_fill_race_returns_filled_not_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """422 from Alpaca means the order is already terminal.  If it was filled
    before our cancel arrived, the returned Order must show FILLED so callers
    (reconcile / WP-8) see the real position that now exists."""
    broker, mock_client = _broker_with_mock_client(monkeypatch)
    mock_client.cancel_order_by_id.side_effect = _api_error(422)
    mock_client.get_order_by_id.return_value = _alpaca_order(
        status="filled", filled_qty=5, filled_avg_price=1.30
    )

    result = broker.cancel(_order())

    assert result.status == OrderStatus.FILLED
    assert result.filled_qty == 5
    assert result.net_fill_price == pytest.approx(1.30)


def test_cancel_already_cancelled_422_no_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, mock_client = _broker_with_mock_client(monkeypatch)
    mock_client.cancel_order_by_id.side_effect = _api_error(422)
    mock_client.get_order_by_id.return_value = _alpaca_order(status="canceled")

    result = broker.cancel(_order())  # must not raise

    assert result.status == OrderStatus.CANCELLED


def test_cancel_422_then_order_not_found_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, mock_client = _broker_with_mock_client(monkeypatch)
    mock_client.cancel_order_by_id.side_effect = _api_error(422)
    mock_client.get_order_by_id.side_effect = _api_error(404)

    with pytest.raises(APIError):
        broker.cancel(_order())


# ---------------------------------------------------------------------------
# cancel() — non-422 errors re-raise immediately
# ---------------------------------------------------------------------------


def test_cancel_non_422_error_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker_with_mock_client(monkeypatch)
    mock_client.cancel_order_by_id.side_effect = _api_error(500)

    with pytest.raises(APIError):
        broker.cancel(_order())


# ---------------------------------------------------------------------------
# cancel() — 429 retry with exponential back-off
# ---------------------------------------------------------------------------


def test_cancel_429_retries_and_eventually_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, mock_client = _broker_with_mock_client(monkeypatch)
    mock_client.cancel_order_by_id.side_effect = [
        _api_error(429),
        _api_error(429),
        None,  # succeeds on third attempt
    ]
    mock_client.get_order_by_id.return_value = _alpaca_order(status="canceled")

    with patch("options_agent.execution.broker.sleep") as mock_sleep:
        result = broker.cancel(_order())

    assert result.status == OrderStatus.CANCELLED
    assert mock_client.cancel_order_by_id.call_count == 3
    assert mock_sleep.call_count == 2


def test_cancel_429_exhausted_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker_with_mock_client(monkeypatch)
    mock_client.cancel_order_by_id.side_effect = _api_error(429)

    with patch("options_agent.execution.broker.sleep"):
        with pytest.raises(APIError):
            broker.cancel(_order())

    # 3 delays in _RATE_LIMIT_DELAYS → 4 total attempts before giving up
    assert mock_client.cancel_order_by_id.call_count == 4


def test_cancel_429_honours_retry_after_header(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker_with_mock_client(monkeypatch)
    mock_client.cancel_order_by_id.side_effect = [
        _api_error(429, retry_after="7"),
        None,
    ]
    mock_client.get_order_by_id.return_value = _alpaca_order(status="canceled")

    with patch("options_agent.execution.broker.sleep") as mock_sleep:
        broker.cancel(_order())

    mock_sleep.assert_called_once_with(7.0)


# ---------------------------------------------------------------------------
# cancel() — 401 triggers one re-auth then retries
# ---------------------------------------------------------------------------


def test_cancel_401_reinits_client_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    mock_client = MagicMock()
    mock_client.cancel_order_by_id.side_effect = [_api_error(401), None]
    mock_client.get_order_by_id.return_value = _alpaca_order(status="canceled")

    with patch(
        "options_agent.execution.broker.TradingClient", return_value=mock_client
    ) as MockTC:
        broker = BrokerClient(_config())
        result = broker.cancel(_order())

    assert result.status == OrderStatus.CANCELLED
    # One reinit call after the initial construction → total TradingClient calls = 2
    assert MockTC.call_count == 2


def test_cancel_401_twice_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, mock_client = _broker_with_mock_client(monkeypatch)
    # Always raise 401 — both before and after reinit.
    mock_client.cancel_order_by_id.side_effect = _api_error(401)

    # Patch TradingClient so _reinit_client returns the same mock (still raises 401).
    with patch(
        "options_agent.execution.broker.TradingClient", return_value=mock_client
    ):
        with pytest.raises(APIError):
            broker.cancel(_order())
