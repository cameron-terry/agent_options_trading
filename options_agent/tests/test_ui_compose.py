"""WP-9.1: the console compose service must carry no broker credentials.

A lightweight text-level check rather than a full YAML parse — avoids adding
a PyYAML dependency for one assertion, and the compose file is small and
hand-authored, so a block-scoped substring check is a reliable proxy for
"what env does docker compose actually inject into this service."
"""

from __future__ import annotations

from pathlib import Path

_COMPOSE_PATH = Path(__file__).parents[2] / "docker-compose.yml"


def _service_block(text: str, service: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{service}:")
    end = start + 1
    while end < len(lines) and (lines[end].startswith("  ") or not lines[end].strip()):
        end += 1
    return "\n".join(lines[start:end])


def test_console_service_has_no_alpaca_credentials():
    text = _COMPOSE_PATH.read_text()
    console_block = _service_block(text, "console")

    assert "ALPACA" not in console_block
    assert "env_file" not in console_block


def test_console_service_exists_and_binds_localhost():
    text = _COMPOSE_PATH.read_text()
    console_block = _service_block(text, "console")

    assert "Dockerfile.console" in console_block
    assert "127.0.0.1:8000:8000" in console_block
