"""Entry point: python -m options_agent.ui [--config path/to/config.toml]

Starts the read-only ops console (WP-9.1 skeleton): serves /api/health and,
once the SPA is built into options_agent/ui/static/, the static frontend.
This process reads no broker credentials — its only input is the DB, plus
(WP-9.7) an optional DISCORD_WEBHOOK_URL for the kill-switch console's
CRITICAL alert, same env var and channel selection as the top-level
`python -m options_agent` scheduler entry point.

Logs go to stdout at INFO level by default. Set LOG_LEVEL=DEBUG for verbose
output.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from options_agent.config import Config
from options_agent.obs.alerts import AlertDispatcher, DiscordChannel, NullChannel
from options_agent.state.db import build_engine
from options_agent.ui.app import create_app


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="options_agent.ui",
        description="Read-only ops console for the options agent",
    )
    parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to config.toml (default: config.toml in cwd)",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
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
            "Config file %s not found — using defaults (SQLite)", config_path
        )

    # Explicit write engine + alert channel for the kill-switch console (WP-9.7)
    # — the console's one write path. Same DB_URL and DISCORD_WEBHOOK_URL
    # selection as the top-level `python -m options_agent` scheduler entry
    # point, so a kill-switch change made through the UI dispatches the
    # identical CRITICAL alert a CLI-triggered one would.
    db_url = os.environ.get("DB_URL", config.db_url)
    write_engine = build_engine(db_url, read_only=False)
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    channel = DiscordChannel(webhook_url) if webhook_url else NullChannel()
    if isinstance(channel, NullChannel):
        logger.info("DISCORD_WEBHOOK_URL not set — alerts suppressed (NullChannel)")
    alert_dispatcher = AlertDispatcher(channel, write_engine)

    app = create_app(
        config=config, write_engine=write_engine, alert_dispatcher=alert_dispatcher
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
