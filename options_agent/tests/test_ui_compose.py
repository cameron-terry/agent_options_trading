"""WP-9.1: the console compose service must carry no broker credentials.

A lightweight text-level check rather than a full YAML parse — avoids adding
a PyYAML dependency for one assertion, and the compose file is small and
hand-authored, so a block-scoped substring check is a reliable proxy for
"what env does docker compose actually inject into this service." Comment
lines are stripped before the substring checks below (WP-9.7) — explanatory
prose referencing ALPACA_*/DISCORD_WEBHOOK_URL by name (e.g. explaining why
one is absent or present) must not itself trip an assertion about actual env
var keys; only YAML content past a `#` would ever be injected by compose.
"""

from __future__ import annotations

import re
from pathlib import Path

_COMPOSE_PATH = Path(__file__).parents[2] / "docker-compose.yml"


def _service_block(text: str, service: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{service}:")
    end = start + 1
    while end < len(lines) and (lines[end].startswith("  ") or not lines[end].strip()):
        end += 1
    return "\n".join(lines[start:end])


def _strip_comments(block: str) -> str:
    """Drop full-line and trailing `#` comments so prose mentions of an env
    var name don't read as compose actually injecting it."""
    return "\n".join(
        re.split(r"(?<!\S)#", line, maxsplit=1)[0] for line in block.splitlines()
    )


def test_console_service_has_no_alpaca_credentials():
    text = _COMPOSE_PATH.read_text()
    console_block = _strip_comments(_service_block(text, "console"))

    assert "ALPACA" not in console_block
    assert "env_file" not in console_block


def test_console_service_gets_anthropic_key_and_discord_webhook():
    # WP-9.8: the ask-the-journal analyst needs ANTHROPIC_API_KEY. WP-9.7:
    # the kill-switch console's CRITICAL alert needs DISCORD_WEBHOOK_URL to
    # be more than a no-op NullChannel dispatch. Both are declared
    # explicitly (not via a bulk secrets file — see the test above), so
    # ALPACA_* broker credentials still never reach the console container.
    text = _COMPOSE_PATH.read_text()
    console_block = _strip_comments(_service_block(text, "console"))

    assert "ANTHROPIC_API_KEY" in console_block
    assert "DISCORD_WEBHOOK_URL" in console_block
    assert "ALPACA" not in console_block


def test_console_service_exists_and_binds_localhost():
    text = _COMPOSE_PATH.read_text()
    console_block = _service_block(text, "console")

    assert "Dockerfile.console" in console_block
    assert "127.0.0.1:8000:8000" in console_block
