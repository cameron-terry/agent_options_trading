# Orchestration & Scheduling

**Module:** `options_agent/orchestrator.py`, `options_agent/scheduler.py`, `options_agent/data/tools.py`
**Credentials required:** Alpaca keys (broker calls); `ANTHROPIC_API_KEY` (reasoner); `DISCORD_WEBHOOK_URL` (optional, alerting)
**Status:** complete

The orchestration layer wires all sub-systems into two runtime loops and drives them at the configured cadences. `orchestrator.py` implements the logic of each cycle; `scheduler.py` drives both at the configured intervals. This doc is the canonical home for the entry-cycle pipeline; the monitor's exit rules are documented in [monitor.md](monitor.md).

## Sub-modules

| File | Responsibility |
|---|---|
| `orchestrator.py` | `run_entry_cycle()` — 10-step entry pipeline; `run_monitor_cycle()` — exit loop; `run_daily_iv_job()` — session-relative ATM IV capture |
| `scheduler.py` | `CycleScheduler` — APScheduler-backed driver with lock-and-skip, kill-switch awareness, observable skip counters |
| `data/tools.py` | `build_real_tool_impls()` — real data-tool implementation factory (AlpacaDataClient + yfinance) |
| `__main__.py` | Process entry point: load config, wire engine + alerting + scheduler, block until signal |

---

## Entry cycle (`run_entry_cycle`)

The full pipeline, executed a few times per day at configured ET times:

```
1. KILL_SWITCH      — bail on HALT / FLATTEN; fail closed (treat as HALT) if DB unreadable
2. RECONCILE        — broker.reconcile() → StateDiff; dispatch fill alerts
3. STATE_INTEGRITY  — act on StateDiff anomalies before any expensive work:
   3a. WORKING open orders → cancel (fill-race → proceed; cancel-fail → skip)
   3b. unmatched_local     → HALT + CRITICAL (split-brain risk)
   3c. orphans             → WARN alert + skip entry this cycle
   3d. assigned_positions  → HALT + CRITICAL (equity not modeled)
4. TEMPORAL GATES   — market_is_open → within_blackout_window
5. ASSEMBLE         — context/assembler.py; tool impls selected by use_real_data_tools
6. PORTFOLIO GATES  — has_buying_power → under_position_cap
7. REASON           — agent/reasoner.py; the only LLM call
8. VALIDATE         — risk/validator.py; rejection journals + REJECTION alert
9. SIZE             — risk/sizing.py
10. EXECUTE + JOURNAL — broker.submit_multi_leg(); fill alert; journal record written
```

**Short-circuit invariant:** every early exit journals a `NO_ACTION_GATED` record and returns a `CycleResult` with `short_circuit_reason` set (`KILL_SWITCH_HALT`, `MARKET_CLOSED`, `BLACKOUT_WINDOW`, `NO_BUYING_POWER`, `MAX_POSITIONS`, `EMPTY_ACTION_SPACE`, and the state-integrity reasons above). The LLM is never called unless steps 1–6 all pass.

**DI parameters:** `run_entry_cycle(config, *, broker=None, engine=None, dispatcher=None, _now=None)`. Absent arguments are built from config; `dispatcher=None` suppresses alerts; `_now` is a test-only clock override (inject a market-hours timestamp instead of mocking the exchange calendar — production callers must not pass it). `run_monitor_cycle` has the same shape.

## Monitor cycle (`run_monitor_cycle`)

The fast, deterministic exit loop — no LLM — runs every `monitor_interval_minutes` during market hours: kill-switch read (fail-safe: proceed as `NONE` on DB error) → market-open check → reconcile → evaluate stop-loss / profit-target / DTE per position (or `flatten_position` under FLATTEN) → finalize `OutcomeRecord`s for fills confirmed this cycle. Re-entrant safe, with per-position error isolation.

Exit-rule formulas, idempotency guards, assignment handling, and alert events are documented in [monitor.md](monitor.md); kill-switch states and semantics in the [kill-switch runbook](../runbook_kill_switch.md).

## Scheduler (`CycleScheduler`)

APScheduler `BackgroundScheduler` driving both loops plus the daily IV job.

| Loop | Trigger | Config key |
|---|---|---|
| Monitor | `IntervalTrigger(minutes=N)` | `monitor_interval_minutes` (default 2) |
| Entry | `CronTrigger` per configured time | `entry_times` (default 10:30, 13:00, 15:00 ET) |
| Daily IV | `DateTrigger` at `session_close + offset`; reschedules itself in `finally` | `daily_iv_capture_offset_minutes` (default 15) |

- **Lock-and-skip:** separate locks for entry and monitor — a slow LLM reasoning step can never delay stop-loss checks. A cycle still running when the next fire arrives is skipped with a warning; after 3 consecutive skips a `SCHEDULER_SKIP / WARN` alert fires.
- **Kill-switch at scheduler level:** the entry job checks the kill-switch before acquiring its lock (DB failure → skip, fail closed). The monitor and daily-IV jobs are *not* blocked — exits and IV-history accumulation continue under HALT/FLATTEN.
- **Daily IV capture is session-relative** (fires 15 min after the *actual* close, including half-days), idempotent (`record_daily_iv` upserts), and never writes null/zero fills — a skipped session is a permanent history gap, so skips alert.
- `coalesce=True` everywhere: missed fires collapse into one on recovery.

```python
with CycleScheduler(config, engine=engine) as scheduler:
    scheduler.run_forever()   # blocks until SIGINT / SIGTERM
```

## Entry point

```bash
python -m options_agent [--config path/to/config.toml]
```

Credentials come from the environment (see the [README](../../README.md#environment-variables)). `LOG_LEVEL=DEBUG` enables per-cycle scheduler/orchestrator detail.

## Config keys (scheduling and cadence)

| Key | Default | Notes |
|---|---|---|
| `entry_times` | `[10:30, 13:00, 15:00]` | ET times; one cron job per entry |
| `monitor_interval_minutes` | `2` | Monitor fire interval |
| `monitor_max_mark_age_minutes` | `4` | Mark-staleness guard; keep ≥ 2× monitor interval |
| `session_open_blackout_minutes` / `session_close_blackout_minutes` | `30` | Entry skipped within N min of open/close |
| `exchange_calendar` | `"XNYS"` | Drives all session detection — holidays and early closes included; blackouts are computed against the *actual* close, not a hardcoded 16:00 |
| `daily_iv_capture_offset_minutes` | `15` | Minutes after session close to sample ATM IV |

## Data-layer selection (`use_real_data_tools`)

`Config.use_real_data_tools` (default `False`) and `Config.alpaca_paper` are independent flags:

| `alpaca_paper` | `use_real_data_tools` | Outcome |
|---|---|---|
| `True` | `False` | Mock tool impls — dev/CI, no live data calls (default) |
| `True` | `True` | Real data, paper money — **the 90-day paper validation run** |
| `False` | `True` | Real data, live money — production |
| `False` | `False` | **Hard error** at Config construction (live money + fabricated data forbidden) |

`build_real_tool_impls()` wires the live implementations of all six agent tools (portfolio state, universe snapshot with IV rank/percentile enrichment, filtered chain, events, journal, position history) from the data layer — see [data-signals.md](data-signals.md), including the ~30-session IV warm-up during which entry cycles correctly `NO_ACTION`.

## Offline testing (no credentials)

Unit tests run the full entry cycle against an in-memory DB with a `MagicMock` broker and a patched reasoner (`patch("options_agent.orchestrator.reason", return_value=stub_reasoner())`) — see `options_agent/tests/test_orchestrator.py` for the canonical fixture. `stub_reasoner()` has a hardcoded expiration date with an expiry guard; when it fires, bump `_STUB_EXPIRY` to a quarterly expiration ≥ 45 days out.

## Invariants

- The entry cycle never runs under `HALT` or `FLATTEN` — blocked at both the scheduler layer and inside the cycle.
- The monitor never auto-FLATTENs on a kill-switch read failure; only explicit operator action causes FLATTEN.
- Entry and monitor locks are independent — safety checks are never starved by a long LLM call.
- All cadence parameters live in `Config`; nothing is hardcoded in scheduler or orchestrator.

## Known gaps

- **`client_order_id` not persisted** on the `Order` model, so unmatched-local recovery is impossible — any `PENDING_SUBMIT` order without a `broker_order_id` triggers HALT. Requires a WP-0 contract amendment.
