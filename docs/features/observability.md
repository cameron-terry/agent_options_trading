# Observability & Safety

**Module:** `options_agent/obs/`
**Credentials required:** `DISCORD_WEBHOOK_URL` (alerting only; optional for review/kill-switch)
**Status:** complete

Runtime safety controls and operational visibility: anything that lets an operator observe, pause, or stop the system without touching the core trading logic.

## Sub-modules

| File | Responsibility |
|---|---|
| `obs/killswitch.py` | Kill-switch core API: state helpers, DB reads/writes |
| `obs/__main__.py` | Observability CLI: kill-switch commands + `review` + `bias` |
| `obs/alerts.py` | Alerting: Discord webhook channel, async dispatcher, durable failure recording |
| `obs/review.py` | Journal analytics: hit rate, P&L attribution, cycle funnel, bias detection |
| `obs/data_quality.py` | Registry of known `JournalRecord.data_quality_flags` values |

---

## Kill-switch

Append-only safety lever (`kill_switch_log` table — INSERT only, current state = most recent row) that gates new entries and, under FLATTEN, forces closure of all open positions. The orchestrator reads it at the top of every cycle.

States, CLI commands, and escalation tiers (CLI → raw SQL → Alpaca key revocation) live in the operator-facing [kill-switch runbook](../runbook_kill_switch.md).

**Invariants:**

- **FLATTEN implies HALT.** `is_halted()` returns `True` under both `HALT` and `FLATTEN`. Implementing it as `state == HALT` is wrong and dangerous — under `FLATTEN` the entry cycle would proceed while positions are being force-closed.
- **HALT does not freeze the monitor.** Exit rules still fire under `HALT` — an unmanaged position under a "safety halt" is the opposite of safe.
- **Fail-safe is asymmetric** (enforced in `orchestrator.py`; `killswitch.py` itself propagates exceptions): on a DB read failure the **entry** cycle treats the state as `HALT` (fail closed — cannot confirm `NONE`), while the **monitor** cycle treats it as `NONE` (never auto-FLATTEN on an unreadable flag).

API surface: `get_current_state`, `is_halted`, `is_flatten`, `set_state`, `resume`, `list_history` — all `Connection`-based, with an optional `dispatcher` argument to fire the state-change alert.

---

## Alerting

Non-blocking notification layer that fires on fills, rejections, and kill-switch changes. Types live in `contracts/alerts.py` (`AlertEvent`, `AlertEventType`, `AlertSeverity`, `DEFAULT_SEVERITY`); the channel is an injectable protocol — `DiscordChannel` in production, `NullChannel` for tests/disabled.

**Key invariant: alerting is strictly subordinate to trading.** A channel failure must never crash or stall a cycle:

- `dispatch()` enqueues and returns immediately; delivery happens on a worker thread with bounded retry.
- On retry exhaustion the alert is dropped but the failure is durably recorded to the `alert_delivery_failures` table (queryable by review and the console) — logs are the medium alerting exists to not depend on.
- Channel exceptions never propagate to the caller.
- `shutdown()` (or context-manager exit) drains the queue, so a CRITICAL fired just before process exit isn't lost.

```python
with AlertDispatcher(channel, engine) as dispatcher:
    dispatcher.dispatch(AlertEvent(event_type=..., severity=..., detail=...))
```

---

## Journal review

Pure, deterministic functions in `obs/review.py` over pre-fetched `JournalRecord`/`OutcomeRecord` objects — no DB calls, no live data, fixture-testable. Exposed via `python -m options_agent.obs review` (three tables: cycle funnel, hit rate by strategy, P&L attribution) with `--since` and `--prompt-version` filters, and consumed by the ops console's Performance screen.

**Design invariants:**

- **Hit definition:** `realized_pnl > 0` on a fully-closed position. `ExitReason` is deliberately not used — it measures exit plumbing, not trade quality (a stop-loss close at a small profit is a hit).
- **Hit rate is never standalone.** Every report carries `avg_win`, `avg_loss`, and `expectancy` alongside — credit strategies win often and lose big, so a bare hit rate actively misleads.
- **Open positions are never mixed into closed stats** — partial-close proceeds appear in a clearly separated `open_summary`.
- **Pure over stored data.** No live marks, no broker calls; if unrealized P&L is ever needed, read the monitor's cached marks from the DB.

**Functions:** `hit_rate_by_strategy(records, outcomes, *, since, prompt_version)`, `pnl_attribution(...)` (by underlying and strategy, plus `total_realized_pnl`), and `cycle_funnel(records, *, since)` — the full entry-cycle funnel from `action_taken` (`total → gated → reasoned → proposed → rejected / sized_to_zero / execution_failed / opened`). The funnel counts *all* cycles and is the primary diagnostic during warm-up, when hit-rate samples are too few to mean anything. Both filters key off the **opening** record's timestamp/prompt-version, enabling "hit rate since the v3 prompt" comparisons.

---

## Bias / failure-mode detection

`detect_bias()` in `obs/review.py` is the measurement layer that the design doc makes a prerequisite for any future multi-agent challenger: the challenger is only justified by a *specific, measured* failure mode. Exposed via `python -m options_agent.obs bias` (same `--since`/`--prompt-version` filters).

**Design invariants:**

- **Evidence, never action.** `BiasReport` carries measurements and uncertainty — no `halt_recommended` or any action field. A human step between evidence and HALT is a safety feature.
- **"Insufficient data" is the default verdict.** Every cell ships `sample_size` + a `sufficient` flag; cells below `Limits.bias_min_sample_size` (default 10) return NaN and `sufficient=False`. Mostly-insufficient output during warm-up is correct, not a malfunction.
- **Skew ≠ bias.** A bullish lean in an uptrend may be correct regime-reading; the report names the lean, the human judges it.
- **One proximity definition.** `earnings_within_dte` is baked in at journal-write time using `Limits.event_blackout_days`, so "near catalyst" here means exactly what "near earnings" means in the validator.

**Metrics:** (a) delta skew — mean `net_delta_at_open` across OPENED proposals vs. a 0.0 neutral baseline (accumulates fast; no closed positions needed); (b) direction win rates — realized hit rate for bullish vs. bearish proposals; plus an event-proximity cohort comparing trades opened with `earnings_within_dte=True` against baseline (the "killed by IV crush" failure mode).

The inputs (`iv_rank_at_open`, `net_delta_at_open`, `earnings_within_dte`) are populated by the orchestrator at journal-write time for `OPENED` and `SIZED_TO_ZERO` cycles; `scripts/backfill_iv_rank_at_open.py` patched rows written before that wiring existed.

---

## Data-quality flags

Retroactive, append-only annotations on `JournalRecord` (`data_quality_flags`, JSON `list[str]`, default `[]`) for cycles known to carry bad denormalized/context data from a bug fixed after the fact. They don't change runtime behaviour — they mark historical rows so downstream consumers don't reason from corrupted numbers.

`obs/data_quality.py::DATA_QUALITY_FLAG_DESCRIPTIONS` is the single source of truth for flag names → descriptions; add an entry there whenever a data bug is found and backfilled. Current flags: `phantom_net_delta` (unreliable `net_dollar_delta` on 4 cycles 2026-07-09/10; fixed by the held-leg Greek fetch).

Consumers — the ask-the-journal prompt (excludes/caveats flagged cycles), the Decision explorer (warning chip per flag), and any future eval loop — should check this field before trusting a flagged cycle's numbers.
