# Ops Console

**Module:** `options_agent/ui/` (backend), `frontend/` (React SPA source)
**Credentials required:** none — reads the DB only; `ANTHROPIC_API_KEY` becomes required starting WP-9.8 (ask-the-journal)
**Status:** Overview (WP-9.2), Decision explorer (WP-9.3), the live activity stream (WP-9.4), the Performance & bias screen (WP-9.5), and the Ask-the-journal analyst backend (WP-9.8) are complete. Skeleton (`/api/health` + static SPA shell + read-only engine + compose wiring) landed in WP-9.1. The Ask screen (chat UI, WP-9.9) is still to come.

A read-only web console over the trading journal, served by a FastAPI service that runs beside the scheduler in docker-compose. Zero changes to the trading loop; the only planned write path (kill switch, WP-9.7) reuses `obs/killswitch.py` verbatim.

## Sub-modules

| File | Responsibility |
|---|---|
| `ui/app.py` | FastAPI app factory: `/api/health`, `/api/overview`, `/api/positions`, `/api/cycles`, `/api/cycles/{cycle_id}`, `/api/events`, `/api/review/*`, `/api/ask`, static SPA mount, engine wiring |
| `ui/overview.py` | Overview API aggregation (WP-9.2): tiles, equity curve, activity feed, distance-to-trigger meter |
| `ui/cycles.py` | Decision explorer API (WP-9.3): filtered cycle list + full-trace detail, with position/order/outcome joins |
| `ui/events.py` | Live activity stream (WP-9.4): `GET /api/events` SSE body — polls journal_records/kill_switch_log/positions for high-water-mark changes |
| `ui/review.py` | Performance & bias API (WP-9.5): thin wrappers over `obs/review.py`'s four pure functions — funnel, hit rate, P&L attribution, bias |
| `ui/ask.py` | Ask-the-journal API (WP-9.8): `POST /api/ask` request/response shaping around `agent/ask.py`'s analyst loop |
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

## Live activity stream (WP-9.4)

`GET /api/events` is an SSE endpoint that tells the browser *when* to re-fetch, not what changed. `ui/events.py` polls three high-water-mark columns every `POLL_INTERVAL_SECONDS` (5s, decoupled from `config.monitor_interval_minutes` — that setting is the scheduler's write cadence, not how often this read-only service should check): `journal_records.timestamp`, `kill_switch_log.created_at`, `positions.marked_at`. Each poll that finds an advanced max emits one `event: update\ndata: {"kind": "journal"|"killswitch"|"positions"}` per changed table; a poll with nothing new emits an SSE comment (`: heartbeat`) so intermediate proxies don't time out an idle connection.

```python
from options_agent.ui.events import event_stream

# wired directly into GET /api/events in ui/app.py via StreamingResponse
async for chunk in event_stream(engine, request):
    ...
```

**Why a tick, not a payload (WP-9.4 decision, 2026-07-12).** The event never carries the changed row — the client already has typed REST fetchers (`fetchOverview`, `fetchPositions`, `fetchCycles`) built for WP-9.2/9.3's polling. Duplicating row-shaping logic into the SSE encoder would create a second source of truth for response shapes and dedup/ordering concerns on top of it; a tick just triggers the existing fetch.

**Baseline established at connect, not at zero.** `_read_high_water_marks` snapshots the current max per table when a client subscribes, so pre-existing rows never generate a flood of ticks on first connect — only rows/updates *after* that baseline (including later polls) are reported.

**Reconnect semantics: re-fetch, not replay.** There is no `Last-Event-ID` / server-side per-connection cursor. A dropped connection (the browser's `EventSource` auto-reconnects) is expected to re-fetch full state via the REST endpoints — the tick protocol carries no history, only "check now." On the frontend, `EventSource.onopen` (which fires on both the initial connect and every auto-reconnect) is the single trigger for `App.tsx`'s `load()`.

**Poll-only, no LISTEN/NOTIFY.** A single code path across SQLite and Postgres; Postgres's `LISTEN`/`NOTIFY` would remove poll latency but was judged not worth a second code path for a v1 feature. `positions.marked_at` has no DB index — negligible at this system's open-position count (a handful at a time).

---

## Performance & bias API (WP-9.5)

`GET /api/review/funnel|hit-rate|attribution|bias` wrap the four pure functions in `obs/review.py` (`cycle_funnel`, `hit_rate_by_strategy`, `pnl_attribution`, `detect_bias`) with no new metrics and no new hit definition — see that module's docstring for the analytics design itself.

```python
from options_agent.state.db import get_connection
from options_agent.ui.review import get_funnel, get_hit_rate, get_attribution, get_bias

with get_connection(engine) as conn:
    funnel = get_funnel(conn, since=since)                                    # FunnelResponse
    hit_rate = get_hit_rate(conn, since=since, min_sample_size=10)            # HitRateResponse
    attribution = get_attribution(conn, since=since)                         # AttributionResponse
    bias = get_bias(conn, since=since, min_sample_size=10)                   # BiasResponse
```

**Parity with the CLI is load-bearing, not incidental.** `_fetch()` in `ui/review.py` reproduces `obs/__main__.py`'s `cmd_review`/`cmd_bias` fetch exactly: `query_journal(date_from=since)`, then `query_outcome_records` scoped to the `position_ids` touched by an `OPENED`/`CLOSED`/`ROLLED` record in that window. Any drift here would silently break the WP-9 epic's "matches `python -m options_agent.obs review`/`bias`" definition-of-done invariant; `test_ui_review.py` asserts endpoint output against direct calls into the same pure functions.

**NaN never reaches the wire.** `obs/review.py`'s dataclasses use `math.nan` for undefined stats (e.g. `avg_win` with zero wins) — valid Python, invalid JSON. Every float that can be NaN is converted to `None` before leaving `ui/review.py` (`_nn()`); the frontend renders `None` as "—", same as the CLI's dim-dash treatment.

**Insufficient-sample display gating is a wrapper-layer concern, not a pure-function one.** `hit_rate_by_strategy()` has no min-sample-size floor of its own — `StrategyStats` only goes NaN at `trade_count == 0`. The card's "insufficient (n<10)" cell requirement is applied in `ui/review.py` on top of the unmodified report: `sufficient = trade_count >= bias_min_sample_size`, nulling `hit_rate`/`avg_win`/`avg_loss`/`expectancy` (never `trade_count` or `total_pnl`) when insufficient. `detect_bias()` already enforces its own `min_sample_size` internally; its `sufficient` flags pass through unchanged. `bias_min_sample_size` (`Limits`, default 10) is reused for both — one threshold, one config knob, per the card's scope (no second hit definition, no new metric).

**`/api/review/funnel` accepts but ignores `prompt_version`.** `cycle_funnel()` has no `prompt_version` filter — and neither does the CLI's `cmd_review` call into it. The endpoint accepts the parameter anyway (for a uniform four-endpoint interface WP-9.6's compare view can drive with one filter object) but never applies it, preserving CLI parity exactly.

**Rejections-by-rule is not one of the four functions.** It's a `Counter` over `rejection_rule_ids` on the already-fetched records, folded into `FunnelResponse.rejections_by_rule` rather than a fifth endpoint — a trivial aggregation, not a new metric.

---

## Ask-the-journal analyst (WP-9.8)

`POST /api/ask` answers natural-language questions over the journal via a second, independent LLM call alongside `agent/reasoner.py`'s trade reasoner — same two-phase agentic-loop shape (exploration with an "auto" tool, then a forced-tool commit call), but with exactly one read-only tool instead of seven, and no deterministic-validation feedback loop (an SQL answer has no `TradeProposal` risk check to fail).

```python
from options_agent.state.db import get_connection
from options_agent.ui.ask import get_ask_answer

with get_connection(engine) as conn:
    response = get_ask_answer(conn, "How many bull_put_spread cycles opened last month?")
    # AskResponse(answer=..., executed_sql=[...], cited_cycle_ids=[...])
```

| File | Responsibility |
|---|---|
| `agent/sql_guard.py` | `validate_select_only` (sqlglot AST allow-list) + `execute_guarded_select` (row cap, statement timeout) |
| `agent/ask_tool.py` | `run_sql` tool definition — the analyst's only tool |
| `agent/ask_schema.py` | `AskAnswer` + the forced-commit `submit_ask_answer` tool |
| `agent/ask_prompts.py` | Hand-written schema + WP-0 semantic-conventions system prompt |
| `agent/ask.py` | `ask()` — the exploration/commit loop |
| `ui/ask.py` | `POST /api/ask` request/response models + wrapper |

**Guardrail: allow-list, not deny-list.** `validate_select_only` parses the submitted SQL with `sqlglot` and accepts it only if it is exactly one statement whose top-level node type is `Select`/`Union`/`Intersect`/`Except`. An allow-list was chosen over enumerating unsafe statement kinds because `sqlglot` has no single expression type per unsafe kind — `PRAGMA`, `ATTACH`, `VACUUM`, `REINDEX`, `EXPLAIN`, and bare `BEGIN`/`COMMIT` all parse to different, inconsistent node types (some even fall back to a generic `Command`), so a deny-list would need to track every one of them and would silently under-reject any kind added later. The read-only engine (`PRAGMA query_only=ON`) is a second, independent backstop — even a query that somehow slipped past the AST check cannot write.

**Timeout: `sqlite3.Connection.set_progress_handler`, not a native statement timeout.** SQLite has none. `execute_guarded_select` installs a progress-handler callback (polled every 1000 VM instructions) that returns non-zero once a wall-clock deadline passes, which SQLite honors by aborting the in-flight query with `OperationalError: interrupted` — the closest primitive SQLite offers to Postgres's `statement_timeout`. Targets SQLite only (the only dialect the console deploys against; see the read-only-engine section above) — `execute_guarded_select` raises `SqlGuardError` immediately if handed a non-SQLite connection rather than silently skipping the timeout.

**Row cap with an honest truncation marker.** `execute_guarded_select` fetches `row_cap + 1` rows and reports `truncated: true` (never a silent, unexplained cutoff) when the extra row exists. Default 500 rows / 5s per query — interpolated into both the `run_sql` tool description and the system prompt so the numbers the model reads always match what's actually enforced.

**`executed_sql` is server-side ground truth, not model self-report.** `AskAnswer` (the `submit_ask_answer` schema) only carries `answer_text` and `cited_cycle_ids` — no `executed_sql` field. `ask()` builds the returned `executed_sql` list itself from the actual `run_sql` tool-call transcript, so an answer can never cite a query that wasn't really run — the same "show your work" discipline as the trading agent's `tool_calls_transcript`.

**`cited_cycle_ids` is grounded, not trusted as-is.** Unlike `executed_sql`, `cited_cycle_ids` genuinely comes from the model — there's no way to derive "which cycles support this claim" server-side. `ask()` tracks every `cycle_id` value that actually appeared in a `run_sql` result this turn (`seen_cycle_ids`) and cross-checks the submitted citations against it: an ungrounded id gets one corrective round-trip, then is silently dropped from the returned list rather than passed through as an unverifiable link. This closes a gap from the initial PR #94 review — without it, a hallucinated `cycle_id` would reach WP-9.9's Decision-explorer link with nothing to stop it 404ing.

**Conventions text intentionally mirrors `obs/review.py`, not a rewritten paraphrase.** The system prompt's hit-rate/open-closed/null-`iv_rank` conventions are worded to match WP-7's review module docstring verbatim (e.g. "Hit definition: realized_pnl > 0") so the analyst can never answer a question using a competing definition of a concept the review screens already canonicalized.

**Schema section is hand-written, not generated.** `contracts/` has narrative Pydantic docstrings but nothing structured for mechanical extraction; `state/db.py`'s `Table(...)` definitions carry the richest schema commentary in the repo and are what `ask_prompts.py` was written against. Update it by hand alongside any future migration that changes column names or semantics — there is no automated sync.

**Deployment: `ANTHROPIC_API_KEY` now reaches the console container.** The compose `console` service deliberately does not use `env_file: .env` (that would also leak `ALPACA_*`/`DISCORD_WEBHOOK_URL` into the UI environment, violating the WP-9 epic's "no broker credentials in the UI environment" invariant) — instead it declares `ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}` explicitly under `environment:`, resolved by docker compose's own `.env` variable-substitution mechanism (independent of the `env_file:` directive).

---

## Frontend (WP-9.1, WP-9.2, WP-9.3, WP-9.4, WP-9.5)

Vite + React + TypeScript, scaffolded via `npm create vite@latest -- --template react-ts`. Source lives at the repo root in `frontend/` (not under `options_agent/`) so Node tooling stays out of the Python package tree.

```bash
cd frontend
npm install
npm run dev      # local dev server; proxies /api/* to http://127.0.0.1:8000 (see vite.config.ts)
npm run build    # -> frontend/dist/
```

`frontend/src/App.tsx` renders the Overview screen: kill-switch chip, four tiles (`components/Tiles.tsx`), an inline-SVG equity curve (`components/EquityCurve.tsx`, no chart library dependency), the activity feed (`components/ActivityFeed.tsx`), and the open-positions table with distance-to-trigger bars (`components/PositionsTable.tsx`). `src/api.ts` is a hand-written typed client mirroring `ui/overview.py`'s Pydantic models field-for-field — keep the two in sync by hand until a schema generator is wired up.

**Refresh strategy (WP-9.4): SSE-triggered re-fetch, not polling.** `App.tsx` opens an `EventSource('/api/events')`; both `onopen` and the `update` event call the same `load()` used by WP-9.2's original 20s poll (now removed) — no component below `App.tsx` needed to change, only the fetch trigger. Because `overview` (and the kill-switch chip that reads `overview.kill_switch.state`) is fetched at the `App.tsx` level regardless of which screen is active, a `killswitch` tick updates the chip on every screen, not just Overview. The Decision explorer still fetches on demand (filter change, cycle selection) rather than reacting to ticks — it's a historical replay view, not a live one.

**Screen switching (WP-9.3, extended WP-9.5):** `App.tsx` holds `screen` (`'overview' | 'decisions' | 'performance'`) and `selectedCycleId` as local React state — the header tabs for built screens are wired to click handlers that flip `screen`; the Ask tab stays an inert `<span>` per the design reference until WP-9.8/9.9. No client-side router yet; both pieces of state are centralized in `App.tsx` so the eventual router swap (likely landing with WP-9.9, whose citations must deep-link into the Decision explorer) is a contained change.

`components/DecisionsScreen.tsx` orchestrates the Decision explorer: `components/CycleFilters.tsx` (symbol/action/date filters — symbol commits on blur/Enter rather than per-keystroke, since `query_journal`'s symbol filter is an exact match, not a substring search), `components/CycleList.tsx` (left-pane cycle list), and `components/CycleTrace.tsx` (right-pane trace renderer: header metadata, expandable tool-call transcript with a truncated result preview, proposal blockquotes + leg table, validation rule chips + reasons, and the sizing/order/position join, including the anomaly case). A filter change always re-selects the newest matching cycle rather than leaving a stale selection in place — otherwise reverting a filter can strand the view on a sparse cycle (e.g. a `NO_ACTION` cycle with no proposal) that reads as "the trace disappeared."

`components/PerformanceScreen.tsx` (WP-9.5) composes the four `/api/review/*` panels: `PerformanceFilters.tsx` (a time-range preset `<select>` computing `since` client-side, plus a free-text `prompt_version` filter — the version-picker dropdown and side-by-side compare layout are WP-9.6 scope), `FunnelPanel.tsx` (funnel bars + rejections-by-rule table), `HitRateTable.tsx` (per-strategy stats with an "insufficient" chip per `sufficient: false`, never a bare 0% or hidden row), `AttributionPanel.tsx` (P&L bars by underlying, plus a by-strategy table), and `BiasPanel.tsx` (the delta-skew meter, clamped to ±0.5 net delta same as the design reference's band, plus the direction/event-proximity cohort table). All four panels re-fetch together on any filter change; nulls from the backend (never NaN) render as "—".

The Ask screen lands in later WP-9 cards per the [design reference](https://claude.ai/code/artifact/ba602f8d-fd08-4c36-8fc5-93fa8a3efd3a).

---

## Docker / compose (WP-9.1)

`Dockerfile.console` is a two-stage build: a `node:22-slim` stage runs `npm ci && npm run build`, then a `python:3.12-slim` stage installs the backend and copies the built `dist/` into `options_agent/ui/static/`. The console image ships no Node runtime.

```bash
docker compose up -d options-agent console
curl http://127.0.0.1:8000/api/health   # {"status": "ok"}
curl http://127.0.0.1:8000/             # SPA shell
```

The `console` compose service:
- Sets `DB_URL` and (WP-9.8) `ANTHROPIC_API_KEY` explicitly — **no `env_file: .env`**, so `ALPACA_*`/`DISCORD_WEBHOOK_URL` never reach the container even though `ANTHROPIC_API_KEY` now does (see the Ask-the-journal section above for why the two mechanisms are independent).
- Mounts `agent_data` **read-write** despite the engine being read-only: SQLite WAL mode requires write access to the `-wal`/`-shm` files for *all* connections, including readers — enforcement is at the connection level (`PRAGMA query_only`), not the filesystem level. A `:ro` volume mount would break WAL reads.
- Binds its port to `127.0.0.1:8000` only — no auth, single-operator deployment per the WP-9 epic's stated scope.