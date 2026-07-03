# WORKSTREAMS — AI Options Trading Agent

> Companion to `options-agent-plan.md`. That doc is the design; this doc is the **work breakdown**.
> **Last updated:** 2026-07-02

---

## How to use this doc

Each work package (WP) below is written to be lifted directly into a tracker (Trello) and then **broken down further** into concrete sub-tasks by whoever owns it. Every WP states:

- **Consumes / Produces** — the typed contracts it depends on and the contracts it delivers. *These are the seams.* If you're writing sub-issues, write them against these contracts, not against other people's internals.
- **Suggested issue breakdown** — a starting point for splitting the WP into tickets.
- **Definition of done (DoD)** — a checklist that makes the WP independently verifiable, ideally without other WPs being finished.

**Golden rule:** build against the *contract* (WP-0), not against another team's code. If a contract is missing or wrong, fix it in WP-0 via the change policy below — don't work around it locally.

### Contract-change policy
WP-0 contracts are frozen after initial sign-off. Changes after freeze are treated like public API changes: proposed in a PR against WP-0, reviewed by the lead + any consuming WP owner, versioned, and announced. Silent contract drift is the single biggest risk to a parallel build.

### Labels suggested
`area:broker` `area:data` `area:risk` `area:agent` `area:state` `area:obs` `area:orchestration` `area:ui` `contract` `blocker` `good-first-issue`

---

## Dependency graph

```
                         ┌─────────────────────────────┐
                         │  WP-0  Contracts & Scaffold  │  (keystone — do first, then freeze)
                         └──────────────┬──────────────┘
                                        │ unblocks everything
        ┌──────────────┬───────────────┼───────────────┬──────────────┬─────────────┐
        ▼              ▼               ▼                ▼              ▼             ▼
   WP-1 Broker    WP-2 State     WP-3 Data        WP-4 Risk      WP-6 Agent    WP-7 Obs
   & Execution    & Persist.     & Signals        & Guardrails   & Reasoning   (alerts,
        │              │               │                │              │         review)
        └──────┬───────┘               │                │              │
               ▼                       │                │              │
        WP-0.5 Vertical Slice  ◄───────┴────(min)───────┴──────(stub)──┘
        (thin end-to-end, week 1)
               │
               ▼
        WP-5 Monitor  ──►  WP-8 Orchestration & Integration  ◄── (everything lands here)
```

## Critical path & milestones

- **Critical path to first paper fill:** WP-0 → (WP-1 + WP-2) → minimal WP-4 → **WP-0.5 vertical slice**.
- **WP-0.5 is a week-one milestone, not a late one.** It's the cheapest proof that the WP-0 contracts are correct. Everything heavy (full WP-3/4, WP-5, WP-6, WP-7) builds off the slice in parallel.
- **WP-8 is the only true "last" package** (full wiring), but it owns WP-0.5 early.

---

## WP-0 — Contracts & scaffolding `contract` `blocker`

**Owner:** lead · **Status:** _complete_ · **Can start:** immediately · **Blocks:** all

**Goal.** Define every shared type and empty interface so all other WPs can build against stubs in parallel. **No business logic** — types and signatures only.

**Scope (in)**
- `TradeProposal`, `Leg`, `ExitPlan` models.
- Typed **return models** for every data tool: `FilteredChain`, `PortfolioState`, `UniverseSnapshot`, `EventInfo`, etc. (the *shape*, not the fetch logic).
- State models: `Position`, `Order`, `Decision`, `ContextSnapshot`.
- `JournalRecord` schema (everything one cycle writes).
- `Config` / `Limits` shape.
- Result types: `ValidationResult`, `SizingResult`.
- Orchestrator entrypoint signatures: `run_entry_cycle()`, `run_monitor_cycle()`.
- Repo skeleton (the directory layout from the plan), linting/test/CI config, dependency manifest.

**Out of scope**
- Any implementation. Any provider/broker calls. Prompts.

**Produces**
- All shared contracts (consumed by every other WP).
- Green CI on an empty skeleton.

**Depends on**
- Nothing.

**Suggested issue breakdown**
- One issue per model group (proposal types / data return types / state models / result types).
- One issue for repo skeleton + CI + dependency manifest.
- One issue for the contract-change policy doc + CODEOWNERS on the contracts module.

**Definition of done**
- [x] All models compile and are importable.
- [x] Every data tool has a typed signature and return model.
- [x] `TradeProposal` round-trips (serialize → deserialize) in a test.
- [x] CI green on the skeleton.
- [x] Contracts module is sign-off-frozen; change policy documented; CODEOWNERS set.

**Post-freeze amendments** — changes made inside WP-5/WP-7 PRs without a dedicated WP-0 PR, documented retroactively per the contract-change policy (PRs WP-0.A1 and WP-0.A2):

| # | Change | File | PR | Consuming WPs |
|---|--------|------|----|---------------|
| A1-1 | `stop_loss_mult` → `stop_loss_max_loss_fraction` — fraction of `est_max_loss` in `(0, 1]`; formula uniform across credit and debit strategies | `contracts/proposal.py` (`ExitPlan`) | #59 (WP-5.1) | WP-6 (confirmed clean 2026-06-20), WP-4 |
| A1-2 | `ExitReason` enum added (`STOP_LOSS`, `PROFIT_TARGET`, `DTE`, `FLATTEN`) — stored as `VARCHAR NULL` on `orders` and `outcome_records` (migration 005); written in finalize step after fill confirmation | `contracts/state.py`, `contracts/journal.py` | #65 (WP-5.5) | WP-7 (implicit sign-off via usage in #69, #70) |
| A1-3 | `monitor_max_mark_age_minutes` added to `Config` — configurable staleness window for `MarkStaleError`; default tracks `2 × monitor_interval_minutes` | `config.py` (`Config`) | #65 (WP-5.5) | WP-5 |
| A2-1 | `contracts/alerts.py` (new file) — `AlertEventType` (`FILL`, `REJECTION`, `KILL_SWITCH_CHANGE`), `AlertSeverity`, `AlertEvent` (Pydantic model with `event_type`, `severity`, `timestamp`, `symbol`, `order_id`, `detail`), `DEFAULT_SEVERITY`; all exported from `contracts/__init__.py` | `contracts/alerts.py` | #68 (WP-7.2) | WP-7 (dispatches), WP-8 (constructs in entry + monitor cycles) |
| A2-2 | `AlertEventType` extended: `ENTRY_SUBMITTED` (order submitted to broker, not yet filled), `STATE_INTEGRITY` (reconcile anomaly detected) | `contracts/alerts.py` | #72 (WP-8.2) | WP-8 |
| A2-3 | `AlertEventType` extended: `EXIT_SUBMITTED` (closing order sent to broker — fires at submit time; `FILL` fires later when reconcile confirms close and `realized_pnl` is available; the two are distinct moments) | `contracts/alerts.py` | #73 (WP-8.3) | WP-8 |
| A2-4 | `AlertEventType` extended: `SCHEDULER_SKIP` (scheduler cadence skip, e.g. market closed or cycle already running) | `contracts/alerts.py` | #74 (WP-8.4) | WP-8 |
| A4-1 | `bias_min_sample_size: int = 10` added to `Limits`; `limits_version` bumped to `0.3.0` — bias analysis minimum sample threshold; kept in `Limits` rather than a separate obs config (WP-4.A1 Option A: `Limits` already imported by `obs/review.py`, consistent with `event_blackout_days`) | `risk/limits.py` | #70 (WP-7.4) | WP-7 (`obs/review.py`) |

---

## WP-0.5 — Vertical slice (thin end-to-end) `blocker`

**Owner:** integrator · **Status:** _complete_ · **Can start:** after WP-0 + minimal WP-1/WP-2/WP-4 · **Target:** week 1

**Goal.** Prove the seams connect with trivial logic, before anyone builds deep. A hardcoded proposal must round-trip to a paper fill and a journal entry.

**Scope (in)**
- Stub reasoner emits **one hardcoded** `TradeProposal`.
- Pass it through real (minimal) validator → real (minimal) sizing → real broker (paper) → reconcile → journal write.
- A trivial `run_entry_cycle()` that chains these.

**Out of scope**
- Real data, real model calls, real strategy logic, full validation rules.

**Consumes**
- WP-0 contracts; minimal WP-1 (place + reconcile one order); minimal WP-2 (write one journal record); minimal WP-4 (validate one known-good proposal).

**Produces**
- A working end-to-end skeleton + a documented "first paper fill" runbook.

**Definition of done**
- [x] Running `run_entry_cycle()` against Alpaca **paper** places the hardcoded order.
- [x] Reconcile detects the fill; a `JournalRecord` is written with the broker order ID.
- [x] The slice is documented so each WP owner knows the integration target is real.

---

## WP-1 — Broker & Execution `area:broker`

**Owner:** _TBD_ · **Status:** _complete_ · **Can start:** immediately (needs Alpaca paper creds)

**Goal.** Reliable order placement and broker↔local reconciliation against Alpaca paper.

**Scope (in)**
- `alpaca-py` wrapper (`execution/broker.py`).
- Multi-leg order construction; **limit pricing only** (never market on options); `orders.py`.
- `reconcile.py`: pull live account/positions/orders, diff vs. DB, detect fills / expirations / assignments.

**Out of scope**
- Deciding *what* to trade (WP-6) or *whether* it's allowed (WP-4).

**Consumes**
- WP-0: `TradeProposal` (validated), `Order`, `Position`. WP-2 state interface (writes through it).

**Produces**
- `submit(proposal) -> Order`, `cancel(order_id)`, `reconcile() -> StateDiff`.

**Depends on**
- WP-0; meets WP-2 at the reconcile boundary.

**Suggested issue breakdown**
- Broker auth/config + connectivity smoke test.
- Single-leg order submit + status polling.
- Multi-leg order construction + limit pricing.
- Reconcile: fills.
- Reconcile: expirations & assignments (the tricky cases).

**Definition of done**
- [x] Places single- and multi-leg limit orders on paper.
- [x] Reconcile correctly classifies fill / partial / expiry / assignment in tests.
- [x] Broker is treated as source of truth for fills; DB for intent (documented).
- [x] Handles broker outage / rate-limit / expired-session gracefully.

---

## WP-2 — State & Persistence `area:state`

**Owner:** _TBD_ · **Status:** _complete_ · **Can start:** immediately

**Goal.** Durable store for positions, orders, decisions, and the journal.

**Scope (in)**
- `db.py` (SQLite first, Postgres-ready), `models.py` persistence layer.
- Journal **writer** (`journal.py`): write the full per-cycle `JournalRecord`.
- Migrations / schema versioning.

**Out of scope**
- Journal *analytics* (that's WP-7). Deciding what to store (shape is WP-0).

**Consumes**
- WP-0: state models + `JournalRecord` schema.

**Produces**
- State read/write interface (used by WP-1, WP-5, WP-8) and journal-write interface (used by WP-8, consumed by WP-7).

**Depends on**
- WP-0.

**Suggested issue breakdown**
- Schema + migrations.
- Position/Order CRUD + tests.
- Journal writer (context-snapshot storage strategy **decided in WP-0.3**: inline `assembled_context: dict` with `context_hash: str` alongside — see `contracts/state.py:ContextSnapshot`).
- SQLite→Postgres switch behind one config flag.

**Definition of done**
- [x] All state models persist and reload losslessly.
- [x] A full `JournalRecord` (context snapshot, proposal, validation, sizing, orders, outcome) writes and reads back.
- [x] Backend swap (SQLite↔Postgres) needs no call-site changes.

**Post-freeze amendments** — `query_outcome_records()` was added to `state/journal.py` by WP-7.3 (PR #69) without a dedicated WP-2 card, documented retroactively per the contract-change policy (WP-2.5):

| # | Change | File | PR | Consuming WPs |
|---|--------|------|----|---------------|
| 2.5-1 | `query_outcome_records(conn, *, position_ids, since)` added — bulk-fetch `OutcomeRecord` rows by position ID and/or date window; `conn`-based (consistent with all other state module functions); satisfies the deferred WP-2.3 join-path requirement (`OutcomeRecord.position_id` is the indexed join key to `JournalRecord` via `Position`) | `state/journal.py` | #69 (WP-7.3) | WP-7 (`obs/__main__.py`), data tools (`data/tools.py`) |

---

## WP-3 — Data & Signals `area:data`

**Owner:** _TBD_ (internally parallel — can be 2–3 people) · **Status:** _in progress_ · **Can start:** immediately (needs data creds)

**Goal.** Turn raw market data into the compact, decision-ready inputs the agent consumes.

**Scope (in)**
- Provider adapters (`providers/alpaca_data.py`, later `polygon.py`).
- `chains.py`: chain fetch + **pre-filter** (liquidity, DTE window, strike range) → `FilteredChain`. **Highest-value deliverable.**
- `greeks_iv.py`: Greeks, IV, and computed **IV-rank / IV-percentile**.
- `events.py`: earnings, ex-div, macro calendar.
- `market.py`: underlying prices, VIX/regime classification.
- `news.py`: headlines/sentiment (**phase 2, optional**).

**Out of scope**
- Strategy decisions; portfolio Greek aggregation (that's `context/`, WP-6-adjacent).

**Consumes**
- WP-0: `FilteredChain`, `UniverseSnapshot`, `EventInfo` return models.

**Produces**
- The data tool implementations behind `get_filtered_chain`, `get_universe_snapshot`, `get_events`.

**Depends on**
- WP-0. (Sub-areas are near-independent of each other.)

**Suggested issue breakdown**
- Provider adapter + caching/rate-limit handling.
- Chain pre-filter (the compaction logic + thresholds).
- Greeks/IV extraction.
- IV-rank/percentile computation + historical IV storage.
- Events calendar integration.
- Regime/VIX classification.
- (Phase 2) news/sentiment.

**Definition of done**
- [x] `get_filtered_chain` returns a compact, liquidity-filtered table within token budget.
- [x] IV-rank values **sanity-checked against an external source**.
- [ ] Earnings-proximity flag correct for a set of known tickers.
- [x] Graceful behavior on missing/stale data.

---

## WP-4 — Risk & Guardrails `area:risk`

**Owner:** _TBD_ · **Status:** _complete_ · **Can start:** immediately · **Recommended: staff first after WP-0**

**Goal.** The deterministic hard layer that no proposal bypasses. Touches neither LLM nor live data → cleanest isolated track.

**Scope (in)**
- `gates.py`: pre-flight (market open, blackout windows, buying power, max open positions).
- `validator.py`: all proposal checks (schema, defined-risk-only, max-loss caps, portfolio Greek bands, concentration, liquidity, event blackout, dup/conflict, kill-switch) → `ValidationResult` with structured reasons.
- `sizing.py`: `contracts = f(conviction, risk budget, max loss)` → `SizingResult`.
- `limits.py`: all numeric limits centralized.

**Out of scope**
- Fetching data (assumes inputs provided). Placing orders.

**Consumes**
- WP-0: `TradeProposal`, `PortfolioState`, `Config`/`Limits`.

**Produces**
- `validate(proposal, state, limits) -> ValidationResult`, `size(proposal, ...) -> SizingResult`, pre-flight gate functions.

**Depends on**
- WP-0 **only**. (Can be fully complete before any data/broker code works.)

**Suggested issue breakdown**
- One issue per rejection reason (each becomes a fixture + check).
- Pre-flight gates.
- Sizing function + edge cases (zero conviction, max-loss cap binding).
- Limits config schema + defaults.

**Definition of done**
- [x] Fixture suite of **hand-written `TradeProposal` objects**: several valid + one tripping **each** rejection reason, all behaving correctly.
- [x] Every rejection emits a structured, loggable reason.
- [x] Naked short legs rejected unconditionally.
- [x] 100% of validator branches covered by tests, **no LLM or live data involved**.

---

## WP-5 — Monitor `area:risk` `area:orchestration`

**Owner:** cameron-terry · **Status:** _complete_ · **Can start:** after WP-1 + WP-2 + WP-4 exist

**Goal.** The fast, deterministic exit loop. No model, no context assembly.

**Scope (in)**
- `monitor/exits.py`: per open position, evaluate stop / profit-target / time-stop (DTE) rules; submit closing orders when triggered.
- `run_monitor_cycle()` body.

**Out of scope**
- Entry decisions. Anything LLM.

**Consumes**
- WP-0 `Position`, `ExitPlan`; WP-1 (submit close); WP-2 (read positions, write journal).

**Produces**
- A runnable monitor cycle.

**Depends on**
- WP-1, WP-2, WP-4.

**Suggested issue breakdown**
- Stop-loss trigger logic.
- Profit-target trigger logic.
- DTE/time-stop logic.
- Idempotency (don't double-submit a close).

**Definition of done**
- [x] Each exit rule triggers correctly against simulated positions.
- [x] Closes route through WP-1; outcomes journaled via WP-2.
- [x] Re-entrant safe: running twice doesn't duplicate closing orders.

---

## WP-6 — Agent & Reasoning `area:agent`

**Owner:** _TBD_ · **Status:** _complete_ · **Can start:** immediately (mocks everything else)

**Goal.** The single LLM call that consumes context and emits a `TradeProposal`. **No execution tool.**

**Scope (in)**
- `reasoner.py`: Anthropic SDK call with read-only tools + structured output.
- `prompts.py`: system prompt + the strategy **playbook** (condition→strategy mapping).
- `tools.py`: read-only tool *definitions* (consumer side).
- `context/assembler.py`: build the compact context bundle (incl. portfolio Greek aggregation).

**Out of scope**
- Implementing data fetches (WP-3) or order placement. The agent must **not** be able to place orders.

**Consumes**
- WP-0 contracts; mocked WP-3 tool returns during development.

**Produces**
- `reason(context) -> TradeProposal`.

**Depends on**
- WP-0 (develops fully against mocks; integrates with WP-3 later).

**Suggested issue breakdown**
- Tool definitions + mocked returns harness.
- Context assembler (incl. net portfolio Greeks).
- System prompt + playbook v1.
- Structured-output enforcement + retry on schema-invalid output.
- Prompt eval harness (fixed mocked context → expected proposal properties).

**Definition of done**
- [x] Given a fixed mocked context, emits a **schema-valid** `TradeProposal`.
- [x] `iv_rationale` and `catalyst_check` are always populated and non-trivial.
- [x] Strategy is always within the allowed playbook.
- [x] Has **no** path to placing an order (verified).

---

## WP-7 — Observability `area:obs`

**Owner:** _TBD_ · **Status:** _not started_ · **Can start:** immediately (needs journal schema)

**Goal.** Turn the journal into actionable improvement, plus alerting and the kill switch.

**Scope (in)**
- `obs/review.py`: hit rate by strategy, P&L attribution, directional-bias detection, event-proximity loss analysis.
- `obs/alerts.py`: notifications + kill-switch hooks.
- Kill switch: `HALT` (no new entries, monitor still exits) and `FLATTEN` (close all); flag checked at top of every cycle.

**Out of scope**
- Writing the journal (WP-2). Making trades.

**Consumes**
- WP-0 `JournalRecord`; WP-2 journal read interface.

**Produces**
- Review/analytics outputs; alert + kill-switch interfaces used by WP-8.

**Depends on**
- WP-0 (schema); reads from WP-2 once available (can develop on sample data).

**Suggested issue breakdown**
- Kill-switch flag + cycle-top check + key-revocation runbook.
- Alerting channel integration.
- Review metric: hit rate + P&L attribution.
- Review metric: bias / failure-mode detection (the input to any future multi-agent decision).

**Definition of done**
- [ ] `HALT` and `FLATTEN` both verified end-to-end (HALT lets monitor exit; FLATTEN closes all).
- [ ] Review produces hit-rate, attribution, and at least one bias/failure-mode report from sample journal data.
- [ ] Alerts fire on fills, rejections, and kill-switch state changes.

---

## WP-8 — Orchestration & Integration `area:orchestration`

**Owner:** integrator · **Status:** _in progress_ · **Can start:** WP-0.5 early; full wiring last

**Goal.** Wire the two loops, cadence, and real components together.

**Scope (in)**
- Full `run_entry_cycle()` (kill-switch → reconcile → gates → assemble → reason → validate → size → execute → journal).
- Full `run_monitor_cycle()` wiring.
- Cadence/scheduling (monitor 1–5 min; entry a few times/day; open/close blackout windows).
- Swap stubs/mocks for real WP-1/2/3/4/5/6/7 components.

**Out of scope**
- Implementing the components themselves.

**Consumes**
- All other WPs.

**Produces**
- The running system on paper.

**Depends on**
- WP-0.5 (early), then all WPs for full wiring.

**Suggested issue breakdown**
- WP-0.5 vertical slice (done — WP-8.1).
- Entry-cycle wiring + short-circuit ordering.
- Monitor-cycle wiring.
- Scheduler + blackout windows.
- Stub→real swap, component by component.

**Definition of done**
- [ ] Both loops run on schedule against paper.
- [ ] Short-circuits work (kill switch / empty action space avoid the LLM call).
- [ ] Full proposal→outcome loop journaled end-to-end with real components.
- [ ] Ready for the long paper run.

---

## WP-9 — Ops Console UI `area:ui` `area:obs`

**Owner:** cameron-terry · **Status:** _not started_ · **Can start:** immediately (reads existing tables; no trading-loop changes)

**Goal.** A read-only web console over the journal the agent already writes: trade/performance visibility, replay of the agent's reasoning per cycle, and a natural-language query agent over the DB. One new FastAPI service beside the scheduler in docker-compose; **zero changes to the trading loop**; exactly one write path (the kill switch, reusing `obs/killswitch.py`).

Design reference: UI proposal artifact (2026-07-02) — four screens (Overview, Decision explorer, Performance & bias, Ask the journal), architecture, and phasing. <https://claude.ai/code/artifact/ba602f8d-fd08-4c36-8fc5-93fa8a3efd3a>

**Scope (in)**
- FastAPI service with a **read-only** DB engine: `/api/overview`, `/api/positions`, `/api/cycles`, `/api/cycles/{cycle_id}`, `/api/review/*`, `/api/events` (SSE).
- React SPA served by the same service; four screens per the design reference.
- **Overview:** kill-switch state, equity/P&L tiles, portfolio Greeks, open positions with distance-to-trigger meters (monitor-cached marks only), live activity feed.
- **Decision explorer:** cycle list with `query_journal` filters; full-trace renderer for one `JournalRecord` — tool-call transcript, thesis/iv_rationale, per-rule validation chips, sizing, linked position → outcome.
- **Performance & bias:** thin wrappers over `obs/review.py` (`cycle_funnel`, `hit_rate_by_strategy`, `pnl_attribution`, `detect_bias`) with `since` + `prompt_version` filters and a prompt-version compare view; insufficient-sample cells rendered explicitly.
- **Kill-switch console:** the only write — `POST /api/killswitch` → `obs/killswitch.set_state()`/`resume()`, required reason, typed confirmation for RESUME and FLATTEN; history from `kill_switch_log`; alert-delivery health panel over `alert_delivery_failures`.
- **Ask the journal:** Claude-backed analyst with a single SELECT-only `run_sql` tool on a read-only connection (row cap + statement timeout); schema + WP-0 conventions in the system prompt; every answer cites the cycle IDs it drew from, linking into the Decision explorer; streamed over SSE.

**Out of scope**
- Any broker/Alpaca calls from the UI; computing or refreshing marks (read the monitor's cache); changes to cycle logic, prompts, or contracts; auth/multi-user (localhost, single-operator deployment); live trading controls beyond the kill switch.

**Consumes**
- WP-0 contracts (`JournalRecord`, `Decision`, `ContextSnapshot`, `ActionTaken`, `ValidationRuleId`); WP-2 read interfaces (`query_journal`, `query_outcome_records`, position/order CRUD); WP-7 `obs/review.py` pure functions + `obs/killswitch.py` API.

**Produces**
- Runnable console service (compose service beside the scheduler) + the read-only HTTP API (reusable by future tooling).

**Depends on**
- WP-2, WP-7 (both complete). Independent of WP-3/WP-8 progress — renders whatever the journal contains.

**Suggested issue breakdown**
- Phase 1 (read-only core): FastAPI skeleton + read-only engine + compose wiring; overview endpoints + screen; decision-explorer endpoints + trace renderer (the flagship); SSE activity tick.
- Phase 2 (analytics + control): `/api/review/*` wrappers + performance screen; prompt-version compare; kill-switch console + alert-delivery health.
- Phase 3 (the analyst): SELECT-only `run_sql` tool + schema/conventions prompt; ask screen with citations + SSE streaming.

**Definition of done**
- [ ] UI runs in docker-compose beside the scheduler; DB opened read-only; no broker credentials in the UI environment.
- [ ] Decision explorer renders any historical cycle's full trace (tool transcript, validation reasons, sizing, outcome join) from `JournalRecord` alone.
- [ ] `/api/review/*` numbers match `python -m options_agent.obs review`/`bias` output on the same DB (parity test).
- [ ] Sub-sample cells show explicit "insufficient" states, never 0% or hidden rows.
- [ ] Kill-switch arm/resume from the UI round-trips through `kill_switch_log` and fires the existing CRITICAL alert; it is the console's only write (verified by inspection/test).
- [ ] NL agent rejects non-SELECT statements and cites cycle IDs in every answer.

---

## Copy-paste sub-issue template

```markdown
### [WP-N.x] <short title>
**Parent WP:** WP-N — <name>
**Area:** area:<...>
**Consumes (contracts):** <WP-0 types this uses>
**Produces:** <function/output this delivers>
**Depends on:** <WP / sub-issue ids, or "none">

**Description**
<what and why, 2–4 sentences>

**Acceptance criteria**
- [ ] ...
- [ ] ...
- [ ] tests added / green

**Out of scope**
- ...
```

---

*Companion to `options-agent-plan.md`. Educational/engineering planning only — not financial advice.*
