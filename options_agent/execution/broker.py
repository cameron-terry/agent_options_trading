import logging
import os
from typing import cast

from alpaca.trading.client import TradingClient
from alpaca.trading.models import TradeAccount

from options_agent.config import Config

logger = logging.getLogger(__name__)


class BrokerClient:
    """Execution-only Alpaca broker wrapper.

    Owns the authenticated TradingClient (orders, account, positions).
    Does not import or touch the DB — all state writes happen at the
    caller boundary through WP-2's state interface.

    Credentials (ALPACA_API_KEY / ALPACA_SECRET_KEY) are read from the
    environment at construction time and never stored or logged.
    Config supplies non-secret settings only (alpaca_paper flag).
    """

    def __init__(self, config: Config) -> None:
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret_key = os.environ.get("ALPACA_SECRET_KEY", "")

        missing = [
            name
            for name, value in (
                ("ALPACA_API_KEY", api_key),
                ("ALPACA_SECRET_KEY", secret_key),
            )
            if not value
        ]
        if missing:
            raise OSError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Set them before constructing BrokerClient."
            )

        try:
            self._client = TradingClient(api_key, secret_key, paper=config.alpaca_paper)
        except Exception as exc:
            logger.error(
                "Alpaca TradingClient failed to initialise. "
                "Check that credentials are valid (key values withheld from log)."
            )
            raise RuntimeError(
                "Alpaca TradingClient failed to initialise; "
                "check credentials are valid."
            ) from exc

        self._is_paper = config.alpaca_paper

    @property
    def is_paper(self) -> bool:
        """True when this client is connected to Alpaca paper trading."""
        return self._is_paper

    def get_account(self) -> TradeAccount:
        """Read account information from Alpaca."""
        return cast(TradeAccount, self._client.get_account())
