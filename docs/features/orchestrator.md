# Orchestration & Scheduling

**Module:** `options_agent/orchestrator.py`, `options_agent/scheduler.py`, `options_agent/data/tools.py`  
**Credentials required:** Alpaca keys (broker calls); `DISCORD_WEBHOOK_URL` (optional, alerting)  
**Status:** entry-cycle wiring complete (WP-8.2); monitor-cycle wiring complete (WP-8.3); scheduler complete (WP-8.4); stub→real swap complete (WP-8.5); daily IV capture + context assembler IV enrichment complete (WP-8.10)

The orchestration layer wires all sub-systems together into two runtime loops and drives them at the correct cadences. `orchestrator.py` implements the logic of each cycle; `scheduler.py` drives both cycles at the configured intervals.

## Sub-modules

| File | Responsibility |
|---|---|
| `orchestrator.py` | `run_entry_cycle()` — full 10-step entry pipeline; `run_monitor_cycle()` — exit evaluation loop; `run_daily_iv_job()` — session-relative ATM IV capture for all universe symbols |
| `scheduler.py` | `CycleScheduler` — APScheduler-backed driver with lock-and-skip, kill-switch awareness, observable skip counters |
| `data/tools.py` | `build_real_tool_impls()` — real WP-3 tool implementation factory (AlpacaDataClient + yfinance) |
| `__main__.py` | Process entry point: load config, wire engine + alerting + scheduler, block until signal |

---

## Entry cycle (`run_entry_cycle`)

The full 10-step pipeline executed a few times per day at configured ET times.

```
1. KILL_SWITCH      — bail on HALT / FLATTEN; fail closed if DB unreadable
2. RECONCILE        — broker.reconcile() → StateDiff; dispatch fill alerts
3. STATE_INTEGRITY  — act on StateDiff anomalies before any expensive work:
   3a. WORKING open orders → cancel (fill-race → proceed; cancel-fail → skip)
   3b. unmatched_local     → HALT + CRITICAL (split-brain risk)
   3c. orphans             → WARN alert + skip entry this cycle
   3d. assigned_positions  → HALT + CRITICAL (equity not modeled)
4. TEMPORAL GATES   — market_is_open → within_blackout_window (no portfolio needed)
5. ASSEMBLE         — context/assembler.py; tool impls selected by use_real_data_tools flag
6. PORTFOLIO GATES  — has_buying_power → under_position_cap
7. REASON           — agent/reasoner.py; ReasonerError → CycleError(REASON)
8. VALIDATE         — risk/validator.py; rejection journals + REJECTION alert
9. SIZE             — risk/sizing.py
10. EXECUTE + JOURNAL — broker.submit_multi_leg(); fill alert; journal record written
```

**Short-circuit invariant:** every early exit journals a `NO_ACTION_GATED` record and returns a `CycleResult` with `short_circuit_reason` set. The LLM is never called unless steps 1–6 all pass.

**Kill-switch semantics (entry):** `HALT` and `FLATTEN` both short-circuit before any broker call. If the DB read for the kill-switch fails, the cycle treats it as `HALT` (fail closed).

## Monitor cycle (`run_monitor_cycle`)

The fast, deterministic exit loop. No LLM call. Runs every `monitor_interval_minutes` during market hours.

```
1. KILL_SWITCH    — read state; fail-safe: proceed as NONE on DB error (never auto-FLATTEN)
2. MARKET OPEN    — return empty MonitorResult if market is closed
3. RECONCILE      — refresh marks, detect fills; HALT + CRITICAL on assignments
4. FRESH POSITIONS — re-read open positions post-reconcile
5. EXIT LOOP      — for each position:
   FLATTEN mode   → flatten_position() immediately
   Normal mode    → check_stop_loss → check_profit_target → check_time_stop
   On trigger     → dispatch EXIT_SUBMITTED alert (order may be WORKING)
6. FINALIZE       — write OutcomeRecords for fills confirmed this cycle; dispatch FILL alerts
```

**Kill-switch semantics (monitor):**
- `NONE` — normal exit evaluation
- `HALT` — monitor runs normally; new entries blocked by entry cycle
- `FLATTEN` — all open OPTION_STRATEGY positions closed immediately, bypassing rule checks

**Re-entrant safe:** running twice does not duplicate closing orders. `exits.py` guards this via `_SKIPPABLE_STATUSES` (position status check) and `has_pending_close` (Order table check).

**Per-position error isolation:** one failing position (MarkStaleError, broker error) is recorded in `MonitorResult.errors` and the loop continues — other positions still have their exits checked.

## Scheduler (`CycleScheduler`)

APScheduler 3.x `BackgroundScheduler` driving both loops.

### Cadence

| Loop | Trigger | Config key |
|---|---|---|
| Monitor | `IntervalTrigger(minutes=N)` | `config.monitor_interval_minutes` (default 2) |
| Entry | `CronTrigger(hour=H, minute=M, timezone=tz)` per time | `config.entry_times` (default `[10:30, 13:00, 15:00]` ET) |
| Daily IV | `DateTrigger` computed from `session_close + offset`; reschedules in `finally` | `config.daily_iv_capture_offset_minutes` (default 15) |

### Lock-and-skip

Separate `threading.Lock` for entry and monitor — **entry can never block the monitor safety loop**. A slow LLM reasoning step must not delay stop-loss checks.

If a cycle is still running when the next scheduled fire arrives: skip, log a warning, and increment the consecutive skip counter. After `_SKIP_WARN_THRESHOLD = 3` consecutive skips a `SCHEDULER_SKIP / WARN` alert fires — persistent skips mean cycles run longer than the interval.

The skip counter resets to zero on the next successful (non-skipped) run.

### Kill-switch at scheduler level

The entry job checks the kill-switch **before acquiring the entry lock**. Under `HALT` or `FLATTEN`, the entry cycle is not invoked at all — makes system-halted visible at the scheduler layer, not only inside the cycle. DB read failure → skip (fail closed, same as the cycle).

The monitor job and the daily IV job are **not blocked** at the scheduler layer. IV history accumulation must continue during HALT/FLATTEN so the 252-day window stays intact for resume.

### Daily IV capture (`run_daily_iv_job`)

Scheduled at `session_close + daily_iv_capture_offset_minutes` (default 15 min) using a `DateTrigger` computed from `exchange_calendars.next_close()`. The job reschedules itself in a `finally` block so it always fires the following session even if the current run raises.

- **Session-relative, not wall-clock** — on a half-day the close is 13:00 ET, so the job fires at 13:15 ET, not 16:15 ET. Consistent sampling is essential: IV history rows must represent the same point in the day relative to session close or rank/percentile comparisons are corrupted.
- **Idempotent** — `record_daily_iv()` is an upsert; running twice on the same date updates the row without creating a duplicate.
- **No null/zero fill** — if `get_atm_iv()` returns `None` for a symbol, no row is written. Missing rows are the correct representation of unavailable data (consistent with WP-3.4).
- **Skip = permanent history gap** — a skipped session cannot be reconstructed after the fact. A `SCHEDULER_SKIP / WARN` alert fires if the previous job is still running when the next session fires.

### APScheduler settings

`coalesce=True` on all jobs: if multiple fires were missed while the scheduler was paused or a cycle overran, collapse them into one on recovery (skip, not queue). `misfire_grace_time` is set to the job's interval in seconds (monitor) or 300 s (entry).

### Usage

```python
from options_agent.config import Config
from options_agent.scheduler import CycleScheduler
from options_agent.state.db import build_engine

config = Config.from_toml("config.toml")
engine = build_engine(config.db_url)

with CycleScheduler(config, engine=engine) as scheduler:
    scheduler.run_forever()   # blocks until SIGINT / SIGTERM
```

## Entry point

```bash
python -m options_agent                        # uses defaults + config.toml if present
python -m options_agent --config path/to/config.toml
```

Credentials via environment:
```bash
export ALPACA_API_KEY="..."
export ALPACA_SECRET_KEY="..."
export DISCORD_WEBHOOK_URL="..."   # optional; alerts silently suppressed without it
```

`LOG_LEVEL=DEBUG` enables per-cycle APScheduler and orchestrator detail.

## Config keys (scheduling and cadence)

| Key | Default | Notes |
|---|---|---|
| `entry_times` | `[time(10,30), time(13,0), time(15,0)]` | ET times; one cron job per entry |
| `timezone` | `"America/New_York"` | IANA tz for cron job scheduling |
| `monitor_interval_minutes` | `2` | Monitor fire interval; tune from paper-run data |
| `monitor_max_mark_age_minutes` | `4` | `exits.py` staleness guard; keep ≥ 2× monitor interval |
| `session_open_blackout_minutes` | `30` | Skip entry if within N min of session open |
| `session_close_blackout_minutes` | `30` | Skip entry if within N min of session close |
| `exchange_calendar` | `"XNYS"` | `exchange_calendars` calendar name; handles holidays + early closes |
| `daily_iv_capture_offset_minutes` | `15` | Minutes after session close to sample ATM IV; session-relative so half-days use the real 13:00 ET close |

### Market-hours and blackout correctness

`exchange_calendars` (already a project dep) drives all session-open/close detection, including holidays and early-close (half) days. The blackout windows in `within_blackout_window()` are computed against the **actual** session close for that date — not a hardcoded 16:00 ET. On an early-close day the close blackout window correctly starts 30 min before the real close (e.g., 12:30 ET for a 13:00 ET half-day close).

## Data-layer selection (`use_real_data_tools`)

`Config.use_real_data_tools` (default `False`) and `Config.alpaca_paper` are **independent flags**. The four combinations:

| `alpaca_paper` | `use_real_data_tools` | Outcome |
|---|---|---|
| `True` | `False` | MOCK_TOOL_IMPLS — dev/CI, no live API calls (default) |
| `True` | `True` | Real WP-3 data, paper money — **the 90-day paper validation run** |
| `False` | `True` | Real WP-3 data, live money — production |
| `False` | `False` | **Hard error** at Config construction (live money + fabricated data forbidden) |

`build_real_tool_impls()` (`data/tools.py`) wires:
- **`get_portfolio_state`** — open positions from WP-2 state DB + account equity/buying power from `BrokerClient.get_account()`
- **`get_universe_snapshot`** — prices from `AlpacaDataClient`, VIX from `YFinanceVolatilityProvider`, macro events from the hardcoded calendar; symbols from `Config.universe_file`. After fetching the base snapshot the closure enriches each `SymbolSnapshot` with `iv_rank` and `iv_percentile` from `data/iv_rank.py`, using the same `get_atm_iv(target_dte=30)` call as the daily capture job to keep the live IV numerator commensurable with stored history.
- **`get_filtered_chain`** — real chain via `AlpacaDataClient` + full WP-3.2 filter pipeline; limits from `Config.limits.chain_filter`
- **`get_events`** — earnings and ex-dividend via `YFinanceProvider`; lookahead window 60 days
- **`get_journal_by_symbol`** — WP-2 `query_journal`, capped at `JOURNAL_MAX_RECORDS`
- **`get_position_history`** — WP-2 position + journal + outcome-record lookup

**Warm-up period:** during the first ~30 trading sessions `iv_rank=None` for all symbols (fewer than `min_days=30` rows in `iv_history`). The correct behaviour is `NO_ACTION` on every entry cycle — this is expected and will be the dominant outcome early in the paper run. After 30 sessions the warm-up lifts symbol by symbol as history accumulates.

## Invariants

- The entry cycle never runs under `HALT` or `FLATTEN` — blocked at both the scheduler layer (before acquiring the lock) and inside the cycle itself (step 1).
- The monitor cycle never auto-FLATTENs on a kill-switch read failure — it proceeds with `NONE` semantics. Only explicit operator action causes FLATTEN.
- Entry and monitor locks are independent — monitor safety checks are never starved by a long LLM reasoning step.
- All cadence parameters live in `Config`; nothing is hardcoded in scheduler or orchestrator.
