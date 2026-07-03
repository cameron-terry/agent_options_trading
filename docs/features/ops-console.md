# Ops Console

**Module:** `options_agent/ui/` (backend), `frontend/` (React SPA source)
**Credentials required:** none — reads the DB only; `ANTHROPIC_API_KEY` becomes required starting WP-9.8 (ask-the-journal)
**Status:** skeleton complete (WP-9.1) — `/api/health` + static SPA shell + read-only engine + compose wiring. Data endpoints land in WP-9.2+.

A read-only web console over the trading journal, served by a FastAPI service that runs beside the scheduler in docker-compose. Zero changes to the trading loop; the only planned write path (kill switch, WP-9.7) reuses `obs/killswitch.py` verbatim.

## Sub-modules

| File | Responsibility |
|---|---|
| `ui/app.py` | FastAPI app factory: `/api/health`, static SPA mount, engine wiring |
| `ui/__main__.py` | CLI entry point: `python -m options_agent.ui [--config path] [--host] [--port]` |
| `frontend/` | Vite + React + TypeScript SPA source; builds to `dist/`, copied into the image at `options_agent/ui/static/` |
| `Dockerfile.console` | Multi-stage build: Node stage builds the SPA, Python stage serves it |

---

## Read-only engine (WP-9.1)

`state.db.build_engine(url, read_only=True)` enforces read-only access at the **session/connection level**, not the filesystem level — same `DB_URL` and credentials as the writable scheduler engine, no separate DB role to provision:

- **SQLite:** `PRAGMA query_only=ON` on every new connection.
- **Postgres:** `SET default_transaction_read_only = on` on every new connection, explicitly committed (the `SET` lives inside psycopg2's implicit transaction — without a commit, SQLAlchemy's pool-checkin `ROLLBACK` silently discards it).

A write attempted through a `read_only=True` engine raises `sqlalchemy.exc.DBAPIError` for both backends (both DDL and DML).

File-based SQLite connections (from *either* engine — this is a DB-wide setting, not a per-engine one) also get `PRAGMA journal_mode=WAL`, so the scheduler's writes don't block the console's reads. `:memory:` URLs skip WAL (unsupported by SQLite for in-memory databases) but still honor `read_only`.

```python
from options_agent.state.db import build_engine

# Console wiring — read-only, same DB_URL as the scheduler
engine = build_engine("sqlite:////app/data/options_agent.db", read_only=True)
```

## FastAPI app (WP-9.1)

```python
from options_agent.config import Config
from options_agent.ui.app import create_app

# Production: builds its own read-only engine from DB_URL / config.db_url
app = create_app(config=Config.from_toml("config.toml"))

# Tests: inject an engine directly
app = create_app(engine=my_test_engine)
```

`GET /api/health` runs a real `SELECT 1` against the engine (not just a connection open) — a mid-migration or unreachable DB surfaces as a `503 {"status": "error"}` at request time instead of silently serving broken data from later endpoints.

If `options_agent/ui/static/` exists (populated by the Docker build), the app mounts it at `/` via `StaticFiles(html=True)` so the SPA's `index.html` serves client-side routes. In source form (no build run), `/` 404s — the skeleton doesn't crash without a built frontend.

### CLI

```bash
python -m options_agent.ui --config config.toml --host 0.0.0.0 --port 8000
```

Same `--config` / logging conventions as `python -m options_agent`.

---

## Frontend (WP-9.1)

Vite + React + TypeScript, scaffolded via `npm create vite@latest -- --template react-ts`. Source lives at the repo root in `frontend/` (not under `options_agent/`) so Node tooling stays out of the Python package tree.

```bash
cd frontend
npm install
npm run dev      # local dev server, proxies nothing yet — /api/health is same-origin only when served by FastAPI
npm run build    # -> frontend/dist/
```

The current shell (`frontend/src/App.tsx`) is a placeholder that pings `/api/health` and renders the result — real screens (Overview, Decision explorer, Performance & bias, Ask the journal) land in later WP-9 cards per the [design reference](https://claude.ai/code/artifact/ba602f8d-fd08-4c36-8fc5-93fa8a3efd3a).

---

## Docker / compose (WP-9.1)

`Dockerfile.console` is a two-stage build: a `node:22-slim` stage runs `npm ci && npm run build`, then a `python:3.12-slim` stage installs the backend and copies the built `dist/` into `options_agent/ui/static/`. The console image ships no Node runtime.

```bash
docker compose up -d options-agent console
curl http://127.0.0.1:8000/api/health   # {"status": "ok"}
curl http://127.0.0.1:8000/             # SPA shell
```

The `console` compose service:
- Sets only `DB_URL` — **no `env_file: .env`**, so no `ALPACA_*`/`ANTHROPIC_API_KEY`/`DISCORD_WEBHOOK_URL` reach the container.
- Mounts `agent_data` **read-write** despite the engine being read-only: SQLite WAL mode requires write access to the `-wal`/`-shm` files for *all* connections, including readers — enforcement is at the connection level (`PRAGMA query_only`), not the filesystem level. A `:ro` volume mount would break WAL reads.
- Binds its port to `127.0.0.1:8000` only — no auth, single-operator deployment per the WP-9 epic's stated scope.