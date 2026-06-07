# WORKSTREAMS — AI Options Trading Agent

> Companion to `options-agent-plan.md`. That doc is the design; this doc is the **work breakdown**.
> **Last updated:** 2026-06-06

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
`area:broker` `area:data` `area:risk` `area:agent` `area:state` `area:obs` `area:orchestration` `contract` `blocker` `good-first-issue`

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

**Owner:** lead · **Status:** _not started_ · **Can start:** immediately · **Blocks:** all

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
- [ ] All models compile and are importable.
- [ ] Every data tool has a typed signature and return model.
- [ ] `TradeProposal` round-trips (serialize → deserialize) in a test.
- [ ] CI green on the skeleton.
- [ ] Contracts module is sign-off-frozen; change policy documented; CODEOWNERS set.

---

## WP-0.5 — Vertical slice (thin end-to-end) `blocker`

**Owner:** integrator · **Status:** _not started_ · **Can start:** after WP-0 + minimal WP-1/WP-2/WP-4 · **Target:** week 1

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
- [ ] Running `run_entry_cycle()` against Alpaca **paper** places the hardcoded order.
- [ ] Reconcile detects the fill; a `JournalRecord` is written with the broker order ID.
- [ ] The slice is documented so each WP owner knows the integration target is real.

---

## WP-1 — Broker & Execution `area:broker`

**Owner:** _TBD_ · **Status:** _not started_ · **Can start:** immediately (needs Alpaca paper creds)

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
- [ ] Places single- and multi-leg limit orders on paper.
- [ ] Reconcile correctly classifies fill / partial / expiry / assignment in tests.
- [ ] Broker is treated as source of truth for fills; DB for intent (documented).
- [ ] Handles broker outage / rate-limit / expired-session gracefully.

---

## WP-2 — State & Persistence `area:state`

**Owner:** _TBD_ · **Status:** _not started_ · **Can start:** immediately

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
- [ ] All state models persist and reload losslessly.
- [ ] A full `JournalRecord` (context snapshot, proposal, validation, sizing, orders, outcome) writes and reads back.
- [ ] Backend swap (SQLite↔Postgres) needs no call-site changes.

---

## WP-3 — Data & Signals `area:data`

**Owner:** _TBD_ (internally parallel — can be 2–3 people) · **Status:** _not started_ · **Can start:** immediately (needs data creds)

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
- [ ] `get_filtered_chain` returns a compact, liquidity-filtered table within token budget.
- [ ] IV-rank values **sanity-checked against an external source**.
- [ ] Earnings-proximity flag correct for a set of known tickers.
- [ ] Graceful behavior on missing/stale data.

---

## WP-4 — Risk & Guardrails `area:risk`

**Owner:** _TBD_ · **Status:** _not started_ · **Can start:** immediately · **Recommended: staff first after WP-0**

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
- [ ] Fixture suite of **hand-written `TradeProposal` objects**: several valid + one tripping **each** rejection reason, all behaving correctly.
- [ ] Every rejection emits a structured, loggable reason.
- [ ] Naked short legs rejected unconditionally.
- [ ] 100% of validator branches covered by tests, **no LLM or live data involved**.

---

## WP-5 — Monitor `area:risk` `area:orchestration`

**Owner:** _TBD_ · **Status:** _not started_ · **Can start:** after WP-1 + WP-2 + WP-4 exist

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
- [ ] Each exit rule triggers correctly against simulated positions.
- [ ] Closes route through WP-1; outcomes journaled via WP-2.
- [ ] Re-entrant safe: running twice doesn't duplicate closing orders.

---

## WP-6 — Agent & Reasoning `area:agent`

**Owner:** _TBD_ · **Status:** _not started_ · **Can start:** immediately (mocks everything else)

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
- [ ] Given a fixed mocked context, emits a **schema-valid** `TradeProposal`.
- [ ] `iv_rationale` and `catalyst_check` are always populated and non-trivial.
- [ ] Strategy is always within the allowed playbook.
- [ ] Has **no** path to placing an order (verified).

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

**Owner:** integrator · **Status:** _not started_ · **Can start:** WP-0.5 early; full wiring last

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
- WP-0.5 vertical slice (do first).
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
