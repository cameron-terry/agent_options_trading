# Options Agent

AI-driven options trading agent. Paper trading only until validated.

## Setup

```bash
uv sync --dev
```

## Run

```bash
uv run python -m options_agent
```

## Lint / type-check / test

```bash
uv run ruff check .
uv run ruff format .
uv run pyright
uv run pytest
```
