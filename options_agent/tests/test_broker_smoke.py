import os

import pytest

from options_agent.config import Config
from options_agent.execution.broker import BrokerClient


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("ALPACA_API_KEY"),
    reason="ALPACA_API_KEY not set — skipping live connectivity check",
)
def test_connectivity_paper_account() -> None:
    config = Config(alpaca_paper=True)
    broker = BrokerClient(config)
    # Confirm we are connected to paper, not a live account.
    assert broker.is_paper is True
    account = broker.get_account()
    assert account is not None
