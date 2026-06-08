from unittest.mock import MagicMock, patch

import pytest

from options_agent.config import Config
from options_agent.execution.broker import BrokerClient


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
