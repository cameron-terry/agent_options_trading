# Observability & Safety

**Module:** `options_agent/obs/`  
**Credentials required:** `DISCORD_WEBHOOK_URL` (alerting only; optional for review/kill-switch)  
**Status:** kill-switch complete (WP-7.1); alerting complete (WP-7.2); review metrics complete (WP-7.3)

Runtime safety controls and operational visibility into the running agent. The module owns anything that lets an operator observe, pause, or stop the system without touching the core trading logic.

## Sub-modules

| File | Responsibility |
|---|---|
| `obs/killswitch.py` | Kill-switch core API: state helpers, DB reads/writes |
| `obs/__main__.py` | Observability CLI: kill-switch commands + `review` |
| `obs/alerts.py` | Alerting channel integration: Discord webhook, dispatcher, durable failure recording |
| `obs/review.py` | Journal analytics: hit rate, P&L attribution, cycle funnel |

---

## Kill-switch (WP-7.1)

Append-only safety lever that gates new entries and (under FLATTEN) forces closure of all open positions. State is persisted to the `kill_switch_log` table so it survives restarts. The orchestrator reads it at the top of every cycle.

See [docs/runbook_kill_switch.md](../runbook_kill_switch.md) for the three-tier escalation procedure (CLI → raw SQL → Alpaca key revocation).

### States

| State | New entries | Monitor cycle | Use when |
|---|---|---|---|
| `NONE` | Allowed | Runs normally | Normal operating state |
| `HALT` | Blocked | Runs normally — stop-loss/profit-target/DTE exits still fire | Reconcile anomaly, broker surprise, manual review needed |
| `FLATTEN` | Blocked | Closes all open positions | Uncontrolled risk, emergency exit, critical system failure |

**FLATTEN implies HALT.** `is_halted()` returns `True` under both `HALT` and `FLATTEN`. Implementing it as `state == HALT` is wrong and dangerous — under `FLATTEN`, the entry cycle would proceed while positions are being force-closed.

**HALT does not freeze the monitor.** Exit rules (stop-loss, profit-target, DTE expiry) still fire under `HALT`. An unmanaged position under a "safety halt" is the opposite of safe.

### CLI

```bash
# Check current state and recent history
python -m options_agent.obs status

# Arm HALT (no confirmation required — zero friction)
python -m options_agent.obs set HALT --reason "broker reconcile mismatch"

# Arm FLATTEN
python -m options_agent.obs set FLATTEN --reason "vega band breached"

# Resume trading (shows current state, prompts for confirmation)
python -m options_agent.obs resume --reason "issue resolved, positions reviewed"

# Skip interactive prompt (for scripted use)
python -m options_agent.obs resume --reason "issue resolved" --yes

# View full history
python -m options_agent.obs history --n 20
```

`--set-by` defaults to `$USER`. Override with `--set-by <name>` when running under a service account.

### Python API

```python
from options_agent.state.db import build_engine, get_connection, metadata
from options_agent.contracts.state import KillSwitchState
from options_agent.obs.killswitch import (
    get_current_state,
    is_halted,
    is_flatten,
    set_state,
    resume,
    list_history,
)

engine = build_engine("sqlite:///options_agent.db")
metadata.create_all(engine)  # no-op if table already exists

# Read current state
with get_connection(engine) as conn:
    state = get_current_state(conn)   # KillSwitchState.NONE when log is empty

print(is_halted(state))   # True under HALT or FLATTEN
print(is_flatten(state))  # True only under FLATTEN

# Arm the switch
with get_connection(engine) as conn:
    entry = set_state(
        conn, KillSwitchState.HALT, set_by="operator", reason="broker mismatch"
    )
print(entry.id, entry.state, entry.created_at)

# Resume
with get_connection(engine) as conn:
    entry = resume(conn, set_by="operator", reason="reconcile complete")

# Read history (newest first)
with get_connection(engine) as conn:
    history = list_history(conn, limit=10)
for e in history:
    print(e.created_at, e.state, e.set_by, e.reason)
```

### DB schema

`kill_switch_log` is **append-only** — never UPDATE an existing row. The current state is always the most recent row ordered by `(created_at DESC, id DESC)`.

| Column | Type | Notes |
|---|---|---|
| `id` | `TEXT` (UUID) | Primary key |
| `state` | `TEXT` | `KillSwitchState` value: `NONE`, `HALT`, `FLATTEN` |
| `set_by` | `TEXT` | Operator name; must not be empty |
| `reason` | `TEXT` | Reason for the state change; must not be empty |
| `created_at` | `DATETIME` (tz) | Indexed; UTC timestamp of the INSERT |

Migration: `alembic/versions/004_kill_switch_log.py`

### Fail-safe contracts

The orchestrator enforces these at the top of each cycle. The core module (`killswitch.py`) propagates exceptions to the caller — fail-safe logic lives in `orchestrator.py`.

| Cycle | DB read failure | Rationale |
|---|---|---|
| Entry | Treat as `HALT` — fail closed | Cannot confirm `NONE`, so refuse new positions |
| Monitor | Treat as `NONE` — proceed with normal exits | Never auto-FLATTEN on an unreadable flag |

---

## Alerting (WP-7.2)

Non-blocking notification layer that fires on fills, rejections, and kill-switch state changes. The channel is behind an injectable protocol so Discord is the default but swapping to any other backend is a one-class change.

**Key invariant:** alerting is strictly subordinate to trading. A channel failure must never crash or stall a cycle.

### Contracts (`contracts/alerts.py`)

| Type | Description |
|---|---|
| `AlertEventType` | `FILL`, `REJECTION`, `KILL_SWITCH_CHANGE`, `ALERT_DELIVERY_FAILED` |
| `AlertSeverity` | `INFO`, `WARN`, `CRITICAL` |
| `AlertEvent` | Pydantic model: `event_type`, `severity`, `timestamp`, `symbol?`, `order_id?`, `detail` |
| `DEFAULT_SEVERITY` | Default severity per event type (fills→INFO, rejections→WARN, kill-switch→CRITICAL) |

### Delivery behaviour

- **Non-blocking:** `dispatch()` enqueues and returns immediately — the cycle thread is never stalled.
- **Bounded retry:** up to `max_attempts=2` with `retry_delay_s=1.0` backoff for transient webhook blips.
- **Durable failure recording:** on exhaustion the alert is dropped but the fact of failure is written to `alert_delivery_failures` (queryable by WP-7 review). Logs are the medium alerting exists to not depend on — a log-only failure record defeats the purpose.
- **Never propagates:** channel failures are always caught inside the worker and never raised into the caller.
- **Shutdown flush:** `shutdown()` drains the queue before joining the worker thread — a CRITICAL fired just before process exit is not silently lost.

### Python API

```python
import os
from options_agent.state.db import build_engine, metadata
from options_agent.contracts.alerts import AlertEvent, AlertEventType, AlertSeverity
from options_agent.obs.alerts import AlertDispatcher, DiscordChannel, NullChannel

engine = build_engine("sqlite:///options_agent.db")
metadata.create_all(engine)

# Production: Discord webhook URL from environment
channel = DiscordChannel(os.environ["DISCORD_WEBHOOK_URL"])

# Tests / alerts disabled: in-memory fake
# channel = NullChannel()

with AlertDispatcher(channel, engine) as dispatcher:
    dispatcher.dispatch(AlertEvent(
        event_type=AlertEventType.KILL_SWITCH_CHANGE,
        severity=AlertSeverity.CRITICAL,
        detail="HALT engaged by operator",
    ))
    dispatcher.dispatch(AlertEvent(
        event_type=AlertEventType.FILL,
        severity=AlertSeverity.INFO,
        detail="SPY bull_put_spread filled at credit 1.35",
        symbol="SPY",
        order_id="broker-ord-abc123",
    ))
# shutdown() called on __exit__; pending alerts flushed before worker stops
```

### DB schema

`alert_delivery_failures` is **append-only**. One row per exhausted-retry send attempt.

| Column | Type | Notes |
|---|---|---|
| `id` | `TEXT` (UUID) | Primary key |
| `event_type` | `TEXT` | `AlertEventType` value |
| `severity` | `TEXT` | `AlertSeverity` value |
| `detail` | `TEXT` | Alert detail string |
| `attempted_at` | `DATETIME` (tz) | Indexed; UTC timestamp of the last attempt |
| `attempts` | `INTEGER` | Number of delivery attempts made |
| `last_error` | `TEXT` | `str(exception)` from the final failed attempt |

Migration: `alembic/versions/006_alert_delivery_failures.py`

### Configuration

Set `DISCORD_WEBHOOK_URL` as an environment variable (same pattern as Alpaca keys — never commit to `config.toml`):

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

To run with alerting disabled (e.g., development, CI), inject `NullChannel` instead of `DiscordChannel`. No env var required.

---

## Journal review (WP-7.3)

Three pure functions in `obs/review.py` that operate on pre-fetched `JournalRecord` and `OutcomeRecord` objects — no DB calls, no live-data dependencies. All three are deterministic and fixture-testable.

### Design invariants

**Hit definition:** `realized_pnl > 0` on a fully-closed position. `ExitReason` is deliberately not used — it measures exit plumbing, not trade quality. A position closed via stop-loss at a small profit is a hit; one that hits the profit target but at a loss is a miss.

**Hit rate is never standalone.** Every call to `hit_rate_by_strategy()` returns `avg_win`, `avg_loss`, and `expectancy` alongside `hit_rate`. Credit strategies are designed to win often and lose big — a standalone hit rate actively misleads.

**Open positions are never mixed into closed stats.** Partial-close proceeds from still-open positions appear in `open_summary`, clearly separated from the closed-trade headline.

**`obs/review.py` is pure over stored data.** No live marks, no broker calls. If unrealized P&L is ever needed, read the monitor's cached marks from the DB — do not fetch fresh inside these functions.

### Functions

```python
from options_agent.obs.review import (
    hit_rate_by_strategy,
    pnl_attribution,
    cycle_funnel,
    HitRateReport,
    PnLAttributionReport,
    CycleFunnelReport,
)
```

#### `hit_rate_by_strategy(records, outcomes, *, since=None, prompt_version=None) -> HitRateReport`

Per-strategy hit rate + P&L context. Returns `StrategyStats` (trade_count, hit_count, hit_rate, avg_win, avg_loss, expectancy, total_pnl) for each strategy bucket and an overall aggregate. `NaN` fields indicate no data in that bucket.

#### `pnl_attribution(records, outcomes, *, since=None, prompt_version=None) -> PnLAttributionReport`

Net P&L broken down by underlying and by strategy. Also exposes `total_realized_pnl` and `open_summary` for still-open positions.

#### `cycle_funnel(records, *, since=None) -> CycleFunnelReport`

Full entry-cycle funnel from `action_taken`. Kept separate from hit-rate — counts *all* cycles. Stages: `total → gated → reasoned → no_action_agent → proposed → rejected / sized_to_zero / execution_failed / opened`. The funnel is the primary diagnostic during warm-up, when the hit rate has too few samples to be meaningful.

### Filters

Both `since: datetime | None` and `prompt_version: str | None` filter by the **opening** `JournalRecord`'s timestamp / prompt_version stamp. This enables queries like "hit rate since the v3 prompt" — the primary mechanism for evaluating whether a prompt or model change improved trade quality.

### CLI

```bash
# All-time review
python -m options_agent.obs review

# Since a date (opening cycle timestamp)
python -m options_agent.obs review --since 2026-06-01

# Filter to a prompt version for before/after comparison
python -m options_agent.obs review --prompt-version v2.0.0
```

Output: three rich tables (cycle funnel, hit rate by strategy, P&L attribution by underlying and strategy). Open positions appear as a labeled addendum, never blended into the closed-trade totals.

### Python API

```python
from options_agent.state.db import build_engine, get_connection, metadata
from options_agent.state.journal import query_journal, query_outcome_records
from options_agent.obs.review import hit_rate_by_strategy, pnl_attribution, cycle_funnel

engine = build_engine("sqlite:///options_agent.db")
with get_connection(engine) as conn:
    records = query_journal(conn)
    position_ids = [pid for r in records for pid in r.position_ids]
    outcomes = query_outcome_records(conn, position_ids=position_ids or None)

hit_report = hit_rate_by_strategy(records, outcomes)
attr_report = pnl_attribution(records, outcomes)
funnel = cycle_funnel(records)

print(hit_report.overall.hit_rate, hit_report.overall.expectancy)
print(attr_report.total_realized_pnl)
print(funnel.opened, funnel.gated)
```

### Forward note

The current hit definition (`realized_pnl > 0`) is the v1 baseline. A richer metric — "captured ≥N% of `est_max_profit`" — is the natural next refinement once `est_max_profit` is stored on `OutcomeRecord`. That is a small WP-0 amendment; track it there rather than here.
