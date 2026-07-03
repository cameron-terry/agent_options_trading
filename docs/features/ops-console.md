# Ops Console

**Module:** `options_agent/ui/` (backend), `frontend/` (React SPA source)
**Credentials required:** none — reads the DB only; `ANTHROPIC_API_KEY` becomes required starting WP-9.8 (ask-the-journal)
**Status:** Overview screen complete (WP-9.2) — `/api/overview` + `/api/positions` + the Overview screen (tiles, equity curve, activity feed, open-positions table). Skeleton (`/api/health` + static SPA shell + read-only engine + compose wiring) landed in WP-9.1. Further screens land in later WP-9 cards.

A read-only web console over the trading journal, served by a FastAPI service that runs beside the scheduler in docker-compose. Zero changes to the trading loop; the only planned write path (kill switch, WP-9.7) reuses `obs/killswitch.py` verbatim.

## Sub-modules

| File | Responsibility |
|---|---|
| `ui/app.py` | FastAPI app factory: `/api/health`, `/api/overview`, `/api/positions`, static SPA mount, engine wiring |
| `ui/overview.py` | Overview API aggregation (WP-9.2): tiles, equity curve, activity feed, distance-to-trigger meter |
| `ui/__main__.py` | CLI entry point: `python -m options_agent.ui [--config path] [--host] [--port]` |
| `frontend/` | Vite + React + TypeScript SPA source; builds to `dist/`, copied into the image at `options_agent/ui/static/` |
| `Dockerfile.console` | Multi-stage build: Node stage builds the SPA, Python stage serves it |

---

## Read-only engine (WP-9.1)

`state.db.build_engine(url, read_only=True)` enforces read-only access at the **session/connection level**, not the filesystem level — same `DB_URL` and credentials as the writable scheduler engine, no separate DB role to provision:

- **SQLite:** `PRAGMA query_only=ON` on every new connection.
- **Postgres:** `SET default_transaction_read_only = on` on every new connection, explicitly committed (the `SET` lives inside psycopg2's implicit transaction — without a commit, SQLAlchemy's pool-checkin `ROLLBACK` silently discards it).

A write attempted through a `read_only=True` engine raises `sqlalchemy.exc.DBAPIError` for both backends (both DDL and DML).

Writable, file-based SQLite connections also get `PRAGMA journal_mode=WAL`, so the scheduler's writes don't block the console's reads — this is a DB-wide, idempotent setting, so it only needs to happen on *some* writable connection, not specifically the first. `read_only=True` engines deliberately never issue this pragma (or any other file-modifying statement) so they perform no write of their own — an earlier version ran `journal_mode=WAL` unconditionally before `query_only`, so a read-only engine connecting first to a brand-new file would flip it to WAL before read-only was ever applied. (This isn't an absolute read-only guarantee: SQLite creates a brand-new file on connect regardless of `query_only` — a driver behaviour, not a statement this code issues. In practice this never happens because the writable/scheduler engine always creates the file first.) `:memory:` URLs skip WAL (unsupported by SQLite for in-memory databases) but still honor `read_only`.

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

`GET /api/health` queries the `alembic_version` table (not a bare `SELECT 1`, which is a constant expression that touches no table and would report healthy against a completely unmigrated, zero-table DB) — an unmigrated or unreachable DB surfaces as a `503 {"status": "error"}` at request time instead of silently serving broken data from later endpoints. Querying `alembic_version` rather than a specific application table keeps the check decoupled from WP-9.2+ schema changes.

If `options_agent/ui/static/` exists (populated by the Docker build), the app mounts it at `/` via `StaticFiles(html=True)` so the SPA's `index.html` serves client-side routes. In source form (no build run), `/` 404s — the skeleton doesn't crash without a built frontend.

### CLI

```bash
python -m options_agent.ui --config config.toml --host 0.0.0.0 --port 8000
```

Same `--config` / logging conventions as `python -m options_agent`.

---

## Overview API (WP-9.2)

`GET /api/overview` and `GET /api/positions` back the landing screen — kill-switch state, four summary tiles, an equity curve, a recent-activity feed, and the open-positions table with distance-to-trigger meters. Both are pure reads over existing tables; no broker or market-data call anywhere in `ui/overview.py` (enforced by a static import-check test).

```python
from options_agent.state.db import get_connection
from options_agent.ui.overview import get_overview, get_positions

with get_connection(engine) as conn:
    overview = get_overview(conn)     # kill_switch, tiles, equity_curve, activity
    positions = get_positions(conn)   # list[PositionSummary], one per open position
```

**Portfolio Greeks are not a tile.** The design reference's "Net Δ/θ per day" tile requires `context.portfolio.aggregate_portfolio_greeks`, which needs a live `FilteredChain` per underlying — a market-data fetch this read-only service must not make, and there is no cached per-leg Greeks store on `Position`/`PositionLeg` today. Adding one is a WP-0 contract change; out of scope for this card. (WP-9 epic decision, 2026-07-03.)

**Distance-to-trigger** reuses `monitor.exits.stop_loss_threshold` / `profit_target_threshold` directly — two pure functions extracted from `check_stop_loss`/`check_profit_target` in WP-9.2 so the console's meter can never drift from the monitor's actual trigger math. Direction follows the sign of `unrealized_pnl`: non-negative measures distance toward the profit target, negative measures distance toward the stop. `pct` is `unrealized_pnl / threshold`, uncapped (a position that hasn't yet been closed by the monitor can show `>1.0`); the frontend clamps the bar width but shows the true percentage as text.

**Account equity** reads `assembled_context["portfolio"]["account_equity"]` off the most recent `JournalRecord` — a stable, documented key (`PortfolioState.model_dump(mode="json")`, see `context/assembler.py:to_context_snapshot`), not an arbitrary blob lookup. It only updates once per entry cycle (a few times/day), so the tile can lag real account state between cycles; there is no live-broker alternative available to a read-only service. `null` when no `JournalRecord` exists yet.

**Equity curve** is cumulative realized P&L from `query_outcome_records`, anchored so the last point equals the current account-equity tile (`offset = latest_equity - total_realized_pnl`). Falls back to un-anchored cumulative P&L (`equity: null` on every point) when no `account_equity` reading exists yet.

**Activity feed** merges `query_journal` and `query_outcome_records`, newest first, capped at 20 rows. Two mockup rows from the design reference are not sourced from this card's data: alert-delivery events (`FILL alert delivered → Discord` — belongs to WP-9.7's `alert_delivery_failures` panel) and synthetic "armed" events on a position nearing its trigger (nothing persists that state; it would have to be recomputed and re-synthesized on every poll). `NO_ACTION_GATED` rows have no gate-reason text — the orchestrator writes that `JournalRecord` with `decision.validation_result=None`, so there's nothing stored to report beyond the action type.

---

## Frontend (WP-9.1, WP-9.2)

Vite + React + TypeScript, scaffolded via `npm create vite@latest -- --template react-ts`. Source lives at the repo root in `frontend/` (not under `options_agent/`) so Node tooling stays out of the Python package tree.

```bash
cd frontend
npm install
npm run dev      # local dev server; proxies /api/* to http://127.0.0.1:8000 (see vite.config.ts)
npm run build    # -> frontend/dist/
```

`frontend/src/App.tsx` renders the Overview screen: kill-switch chip, four tiles (`components/Tiles.tsx`), an inline-SVG equity curve (`components/EquityCurve.tsx`, no chart library dependency), the activity feed (`components/ActivityFeed.tsx`), and the open-positions table with distance-to-trigger bars (`components/PositionsTable.tsx`). `src/api.ts` is a hand-written typed client mirroring `ui/overview.py`'s Pydantic models field-for-field — keep the two in sync by hand until a schema generator is wired up.

**Refresh strategy (v1):** plain client polling every 20s (`App.tsx`'s `POLL_INTERVAL_MS`). WP-9.4's SSE stream isn't built yet; swapping the poll for a push subscription later shouldn't require touching the components, only the fetch trigger in `App.tsx`.

Remaining screens (Decision explorer, Performance & bias, Ask the journal) land in later WP-9 cards per the [design reference](https://claude.ai/code/artifact/ba602f8d-fd08-4c36-8fc5-93fa8a3efd3a).

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