"""Entry point: python -m options_agent [--config path/to/config.toml]

Starts the CycleScheduler, which drives both the entry and monitor loops at
the cadences defined in config.  All credentials must be in environment
variables (ALPACA_API_KEY, ALPACA_SECRET_KEY, and optionally
DISCORD_WEBHOOK_URL).

Logs go to stdout at INFO level by default.  Set LOG_LEVEL=DEBUG for verbose
output including per-cycle APScheduler and orchestrator detail.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from options_agent.config import Config
from options_agent.obs.alerts import AlertDispatcher, DiscordChannel, NullChannel
from options_agent.scheduler import CycleScheduler
from options_agent.state.db import build_engine


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="options_agent",
        description="AI-driven options trading agent (paper mode)",
    )
    parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to config.toml (default: config.toml in cwd)",
    )
    args = parser.parse_args()

    _setup_logging()
    logger = logging.getLogger(__name__)

    config_path = Path(args.config)
    if config_path.exists():
        config = Config.from_toml(config_path)
        logger.info("Loaded config from %s", config_path)
    else:
        config = Config()
        logger.warning(
            "Config file %s not found — using defaults (paper mode, SQLite)",
            config_path,
        )

    engine = build_engine(config.db_url)

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    channel = DiscordChannel(webhook_url) if webhook_url else NullChannel()
    if isinstance(channel, NullChannel):
        logger.info("DISCORD_WEBHOOK_URL not set — alerts suppressed (NullChannel)")

    with AlertDispatcher(channel, engine) as dispatcher:
        with CycleScheduler(config, engine=engine, dispatcher=dispatcher) as scheduler:
            scheduler.run_forever()


if __name__ == "__main__":
    main()
