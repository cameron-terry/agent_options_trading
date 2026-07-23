# Ops Console

**Module:** `options_agent/ui/` (backend), `frontend/` (React SPA source)
**Credentials required:** `ANTHROPIC_API_KEY` (ask-the-journal); `DISCORD_WEBHOOK_URL` optional (kill-switch alert — falls back to `NullChannel`). Otherwise reads the DB only.
**Status:** complete

A read-only web console over the trading journal, served by a FastAPI service that runs beside the scheduler in docker-compose. Zero changes to the trading loop; the only write path is the kill switch, which reuses `obs/killswitch.py` verbatim. Four screens: Overview, Decision explorer, Performance & bias, Ask the journal.

## Sub-modules

| File | Responsibility |
|---|---|
| `ui/app.py` | FastAPI app factory: all `/api/*` routes, static SPA mount, engine wiring |
| `ui/overview.py` | Overview screen: tiles, equity curve, activity feed, distance-to-trigger meters |
| `ui/cycles.py` | Decision explorer: filtered cycle list + full-trace detail with position/order/outcome joins |
| `ui/events.py` | Live activity stream: `GET /api/events` SSE body |
| `ui/review.py` | Performance & bias: thin wrappers over `obs/review.py`'s pure functions |
| `ui/killswitch.py` | Kill-switch console — the console's only write — plus alert-delivery health |
| `ui/ask.py` | Ask-the-journal: `POST /api/ask` SSE framing around `agent/ask/` |
| `agent/ask/` | SELECT-only SQL analyst: sqlglot guard, hand-written schema prompt, exploration/commit loop |
| `ui/__main__.py` | CLI entry: `python -m options_agent.ui [--config path] [--host] [--port]` |
| `frontend/` | Vite + React + TypeScript SPA; builds to `dist/`, copied into the image at `options_agent/ui/static/` |
| `scripts/seed_console_demo_data.py` | Reusable demo-data fixture for docker visual verification |

## Architecture invariants

- **Read-only engine.** `state.db.build_engine(url, read_only=True)` enforces read-only at the connection level (`PRAGMA query_only=ON` on SQLite; `SET default_transaction_read_only = on`, explicitly committed, on Postgres). Same `DB_URL` and credentials as the scheduler; any write through it raises `DBAPIError`. Writable file-based SQLite engines also set WAL mode so scheduler writes never block console reads; read-only engines deliberately issue no file-modifying statement at all.
- **One write path.** `POST /api/killswitch` is handed its own write-capable engine; every other route closes over the read-only one (enforced by a source-scan test). Arming HALT needs only a reason; RESUME and FLATTEN additionally require typing the action word — stricter than the CLI, because a button click has no equivalent of an explicitly typed command. The endpoint dispatches the kill-switch CRITICAL alert through the same channel wiring as the scheduler.
- **CLI parity.** `/api/review/*` reproduces `obs/__main__.py`'s fetch exactly, so console numbers always match `python -m options_agent.obs review`/`bias` on the same DB (asserted by parity tests). NaN from the pure report functions becomes `null` at the API boundary. "Insufficient sample" gating (below `Limits.bias_min_sample_size`) is applied in the `ui/` wrapper layer — the pure `obs/` functions stay untouched, and insufficient cells render explicitly, never as 0% or hidden rows.
- **SSE ticks, not payloads.** `GET /api/events` polls high-water marks (journal timestamps, kill-switch log, position marks) and tells the browser *when* to re-fetch through the existing typed REST client — the event never carries row data, so there is no second source of truth for response shapes. Reconnect means re-fetch; there is no replay cursor.
- **Faithful replay.** The Decision explorer renders whatever the journal contains: journal ids that don't resolve to stored rows are returned with `anomaly: true` rather than hidden or 500ed, validation rendering shows only rules that actually fired (no synthesized green checklist), and cycles tainted by a known historical data bug surface their `data_quality_flags` as warning chips (see [observability.md](observability.md#data-quality-flags)).
- **Health check hits a real table.** `GET /api/health` queries `alembic_version` (not `SELECT 1`), so an unmigrated or unreachable DB surfaces as a 503 instead of broken data from later endpoints.
- **No broker access.** No Alpaca call exists anywhere in `ui/` (enforced by a static import-check test). Values that would need live market data (portfolio Greeks, intra-cycle equity) are either omitted or read from what the scheduler last journaled, and can lag between cycles.

## Ask-the-journal analyst

`POST /api/ask` (SSE) answers natural-language questions over the journal with a Claude agent whose only tool is a SELECT-only `run_sql`:

- **Allow-list guard:** `agent/ask/sql_guard.py` parses submitted SQL with sqlglot and accepts exactly one statement whose top-level node is `Select`/`Union`/`Intersect`/`Except`. The read-only engine is an independent second backstop.
- **Bounded execution:** row cap (500, truncation reported, never silent) and a 5 s wall-clock timeout via SQLite's progress handler. The same numbers are interpolated into the tool description so what the model reads always matches what's enforced.
- **Nothing checkable is model-reported:** `executed_sql` and `tables_touched` are derived server-side from the actual tool transcript. `cited_cycle_ids` (which only the model can supply) are cross-checked against cycle ids actually seen in query results — one corrective round-trip, then ungrounded ids are dropped. Citations deep-link into the Decision explorer.
- **Shared vocabulary:** the system prompt's schema and conventions text mirrors `obs/review.py`'s definitions verbatim (e.g. hit = `realized_pnl > 0`) so the analyst can never answer with a competing definition. It is hand-written — update it alongside any migration that changes column semantics.
- **Per-phase streaming:** events are query-started / query-result / query-error, then the final answer — matching the loop's natural boundaries. Failed queries are streamed too, never silently retried away. Conversation history is client-held (last 5 exchanges).

## Running it

```bash
# Local dev
python -m options_agent.ui --config config.toml --host 0.0.0.0 --port 8000
cd frontend && npm install && npm run dev   # SPA dev server; proxies /api/* to :8000

# Docker
docker compose up -d options-agent console
curl http://127.0.0.1:8000/api/health       # {"status": "ok"}
```

`frontend/src/api.ts` is a hand-written typed client mirroring the `ui/` Pydantic response models — keep the two in sync by hand. Screens re-fetch on SSE tick (Overview) or on demand (Decision explorer, Performance filters); screen switching is local React state in `App.tsx`, no client-side router.

## Docker / compose

`Dockerfile.console` is a two-stage build: Node builds the SPA, Python serves it (no Node in the final image). The `console` compose service:

- Sets `DB_URL` and `ANTHROPIC_API_KEY` explicitly — **no `env_file: .env`** — so `ALPACA_*` never reaches the UI container.
- Mounts `agent_data` read-write despite the read-only engine: SQLite WAL requires write access to `-wal`/`-shm` files for all connections, including readers. Enforcement is at the connection level, not the filesystem level.
- Binds to `127.0.0.1:8000` only — no auth; single-operator deployment.
