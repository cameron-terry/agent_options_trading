# Ops Console

**Module:** `options_agent/ui/` (backend), `frontend/` (React SPA source)
**Credentials required:** none — reads the DB only; `ANTHROPIC_API_KEY` becomes required starting WP-9.8 (ask-the-journal)
**Status:** Overview (WP-9.2) and Decision explorer (WP-9.3) screens complete. Skeleton (`/api/health` + static SPA shell + read-only engine + compose wiring) landed in WP-9.1. Further screens (Performance & bias, Ask the journal) land in later WP-9 cards.

A read-only web console over the trading journal, served by a FastAPI service that runs beside the scheduler in docker-compose. Zero changes to the trading loop; the only planned write path (kill switch, WP-9.7) reuses `obs/killswitch.py` verbatim.

## Sub-modules

| File | Responsibility |
|---|---|
| `ui/app.py` | FastAPI app factory: `/api/health`, `/api/overview`, `/api/positions`, `/api/cycles`, `/api/cycles/{cycle_id}`, static SPA mount, engine wiring |
| `ui/overview.py` | Overview API aggregation (WP-9.2): tiles, equity curve, activity feed, distance-to-trigger meter |
| `ui/cycles.py` | Decision explorer API (WP-9.3): filtered cycle list + full-trace detail, with position/order/outcome joins |
| `ui/__main__.py` | CLI entry point: `python -m options_agent.ui [--config path] [--host] [--port]` |
| `frontend/` | Vite + React + TypeScript SPA source; builds to `dist/`, copied into the image at `options_agent/ui/static/` |
| `Dockerfile.console` | Multi-stage build: Node stage builds the SPA, Python stage serves it |
| `scripts/seed_console_demo_data.py` | Reusable demo-data fixture for docker visual verification (not part of the image) |

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

**Trading mode** (`mode: "paper" | "live"` on `OverviewResponse`) reads `Config.alpaca_paper`, resolved once in `create_app` and threaded into `get_overview`. Not fabricated — the design reference's `/ paper` header badge is real deployment config, not a placeholder.

**Strike detail** (`PositionSummary.strikes`, e.g. `"530/525"` or `"485/480 · 560/565"` for an iron condor) groups `pos.legs` by option right, preserving each leg's original order — short-leg-then-long-leg by construction, since every strategy in this codebase opens the short leg first per right. Groups join in first-seen order; a single-leg position (cash-secured put) renders as just its strike.

---

## Decision explorer API (WP-9.3)

`GET /api/cycles` (filtered list) and `GET /api/cycles/{cycle_id}` (full trace) back the flagship replay screen — every entry cycle's tool-call transcript, proposal, validation verdict, sizing, and linked position/order/outcome, rendered from `JournalRecord` alone.

```python
from options_agent.state.db import get_connection
from options_agent.ui.cycles import get_cycles, get_cycle_detail

with get_connection(engine) as conn:
    cycles = get_cycles(conn, symbol="SPY", action_type=ActionTaken.REJECTED)  # slim list, newest first
    detail = get_cycle_detail(conn, "c-2026-07-11-1435-a7f3")                  # full CycleDetail | None
```

**List vs. detail payload shape.** `CycleListItem` is a slim projection (`cycle_id`, `timestamp`, `action_taken`, `underlying`, `strategy`, `conviction`) — the full record's `assembled_context` blob and tool-call transcript aren't fetched for every row, only for the one cycle the detail panel renders. This keeps the list endpoint cheap regardless of journal size.

**Default lookback.** `GET /api/cycles` defaults `date_from` to 30 days before `now` when the caller passes no date filter — the same reasoning as `get_activity`'s cap in `overview.py`: `query_journal` has no `LIMIT`/`OFFSET`, so an unbounded list grows with the journal. Passing `date_from` explicitly overrides the default.

**Position/order joins surface broken history rather than hiding or erroring on it.** `JournalRecord.position_ids`/`order_ids` are resolved via `state.crud.get_position`/`get_order`; when an id doesn't resolve to a stored row, the response includes it with `anomaly: true` and a `null` position/order rather than 500ing or silently dropping it — the explorer's job is to replay history faithfully, including anomalies. This mirrors `agent/tools.py`'s `PositionHistory` precedent (a missing opening record is a system anomaly to report), but deliberately does **not** reuse that agent-tool model — `PositionLink`/`OrderLink` are UI-response types defined in `ui/cycles.py`.

**Validation rendering reflects what's actually stored, not a synthesized rule catalog.** `ValidationResult.reasons` only contains rules that fired (ERROR or WARNING severity) — there's no record of which rules were evaluated and passed silently. REJECTED cycles show every failing rule's `rule_id` + `human_message` (+ `observed`/`limit` when present); passing cycles with no WARNING reasons show "no rule reasons recorded" rather than fabricating a full green checklist.

---

## Frontend (WP-9.1, WP-9.2, WP-9.3)

Vite + React + TypeScript, scaffolded via `npm create vite@latest -- --template react-ts`. Source lives at the repo root in `frontend/` (not under `options_agent/`) so Node tooling stays out of the Python package tree.

```bash
cd frontend
npm install
npm run dev      # local dev server; proxies /api/* to http://127.0.0.1:8000 (see vite.config.ts)
npm run build    # -> frontend/dist/
```

`frontend/src/App.tsx` renders the Overview screen: kill-switch chip, four tiles (`components/Tiles.tsx`), an inline-SVG equity curve (`components/EquityCurve.tsx`, no chart library dependency), the activity feed (`components/ActivityFeed.tsx`), and the open-positions table with distance-to-trigger bars (`components/PositionsTable.tsx`). `src/api.ts` is a hand-written typed client mirroring `ui/overview.py`'s Pydantic models field-for-field — keep the two in sync by hand until a schema generator is wired up.

**Refresh strategy (v1):** plain client polling every 20s (`App.tsx`'s `POLL_INTERVAL_MS`) for Overview. WP-9.4's SSE stream isn't built yet; swapping the poll for a push subscription later shouldn't require touching the components, only the fetch trigger in `App.tsx`. The Decision explorer fetches on demand (filter change, cycle selection) rather than polling — it's a historical replay view, not a live tick.

**Screen switching (WP-9.3):** `App.tsx` holds `screen` (`'overview' | 'decisions'`) and `selectedCycleId` as local React state — the header tabs for built screens are wired to click handlers that flip `screen`; tabs for screens that don't exist yet (Performance, Ask) stay inert `<span>`s per the design reference. No client-side router yet; both pieces of state are centralized in `App.tsx` so the eventual router swap (likely landing with WP-9.9, whose citations must deep-link into the Decision explorer) is a contained change.

`components/DecisionsScreen.tsx` orchestrates the Decision explorer: `components/CycleFilters.tsx` (symbol/action/date filters — symbol commits on blur/Enter rather than per-keystroke, since `query_journal`'s symbol filter is an exact match, not a substring search), `components/CycleList.tsx` (left-pane cycle list), and `components/CycleTrace.tsx` (right-pane trace renderer: header metadata, expandable tool-call transcript with a truncated result preview, proposal blockquotes + leg table, validation rule chips + reasons, and the sizing/order/position join, including the anomaly case). A filter change always re-selects the newest matching cycle rather than leaving a stale selection in place — otherwise reverting a filter can strand the view on a sparse cycle (e.g. a `NO_ACTION` cycle with no proposal) that reads as "the trace disappeared."

Remaining screens (Performance & bias, Ask the journal) land in later WP-9 cards per the [design reference](https://claude.ai/code/artifact/ba602f8d-fd08-4c36-8fc5-93fa8a3efd3a).

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