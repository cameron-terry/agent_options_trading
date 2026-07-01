# Options Agent

AI-driven options trading agent. Paper trading only until validated.

## Setup

```bash
uv sync --dev
```

## Environment variables

| Variable | Required for | Notes |
|---|---|---|
| `ALPACA_API_KEY` | broker + data | Paper or live key from Alpaca dashboard |
| `ALPACA_SECRET_KEY` | broker + data | Paired secret |
| `ANTHROPIC_API_KEY` | LLM reasoner | Required when `use_real_data_tools = true`; `reason()` calls the Claude API |
| `DISCORD_WEBHOOK_URL` | alerts | Incoming webhook URL; posts alerts to a Discord channel |
| `DB_URL` | Postgres backend | e.g. `postgresql://postgres:postgres@localhost/options_agent`; omit to use SQLite |

Secrets are never read from `config.toml` — set them in the shell or a `.env` file sourced before running.

## Database

Apply migrations before first use (creates `options_agent.db` when using SQLite):

```bash
uv run alembic upgrade head
```

To use Postgres locally, start the bundled container first:

```bash
docker compose up -d
DB_URL=postgresql://postgres:postgres@localhost/options_agent_test uv run alembic upgrade head
```

## Tests

```bash
# Unit + mocked tests only (no credentials required)
uv run pytest -m "not integration"

# Full suite including broker smoke test (requires ALPACA_API_KEY + ALPACA_SECRET_KEY)
uv run pytest

# Postgres dialect (CI runs this automatically; requires DB_URL)
DB_URL=postgresql://postgres:postgres@localhost/options_agent_test uv run pytest
```

## Lint / type-check

```bash
uv run ruff check .
uv run ruff format .
uv run pyright
```

## Entry point

`python -m options_agent [--config path/to/config.toml]` starts the full scheduler. Alpaca paper credentials required.

| Sub-system | Module | Runnable without credentials |
|---|---|---|
| Risk & guardrails | `options_agent/risk/` | Yes — [docs](docs/features/risk-guardrails.md) |
| State & journal | `options_agent/state/` | Yes (SQLite) — [docs](docs/features/state-persistence.md) |
| Data & signals | `options_agent/data/` | No (needs Alpaca keys) — [docs](docs/features/data-signals.md) |
| Broker & execution | `options_agent/execution/` | No (needs Alpaca keys) — [docs](docs/features/broker-execution.md) |
| Agent tools & mock harness | `options_agent/agent/` | Yes — [docs](docs/features/agent-tools.md) |
| Observability & safety | `options_agent/obs/` | Yes (SQLite) — [docs](docs/features/observability.md) |
| Orchestration & scheduling | `options_agent/orchestrator.py`, `options_agent/scheduler.py` | No (needs Alpaca keys) — [docs](docs/features/orchestrator.md) |
| Monitor — exit rules | `options_agent/monitor/` | Yes — [docs](docs/features/monitor.md) |
| Vertical slice | `options_agent/orchestrator.py` | No (needs Alpaca keys) — [docs](docs/features/vertical-slice.md) |
