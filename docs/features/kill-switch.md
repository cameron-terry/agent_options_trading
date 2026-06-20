# Kill-switch

**Module:** `options_agent/obs/`  
**Credentials required:** none  
**Status:** complete (WP-7.1)

Append-only safety lever that gates new entries and (under FLATTEN) forces closure of all open positions. State is persisted to the `kill_switch_log` table so it survives restarts. The orchestrator reads it at the top of every cycle.

See [docs/runbook_kill_switch.md](../runbook_kill_switch.md) for the three-tier escalation procedure (CLI → raw SQL → Alpaca key revocation).

## Sub-modules

| File | Responsibility |
|---|---|
| `obs/killswitch.py` | Core API: state helpers, DB reads/writes |
| `obs/__main__.py` | CLI: `status`, `set`, `resume`, `history` commands |

## States

| State | New entries | Monitor cycle | Use when |
|---|---|---|---|
| `NONE` | Allowed | Runs normally | Normal operating state |
| `HALT` | Blocked | Runs normally — stop-loss/profit-target/DTE exits still fire | Reconcile anomaly, broker surprise, manual review needed |
| `FLATTEN` | Blocked | Closes all open positions | Uncontrolled risk, emergency exit, critical system failure |

**FLATTEN implies HALT.** `is_halted()` returns `True` under both `HALT` and `FLATTEN`. Implementing it as `state == HALT` is wrong and dangerous — under `FLATTEN`, the entry cycle would proceed while positions are being force-closed.

**HALT does not freeze the monitor.** Exit rules (stop-loss, profit-target, DTE expiry) still fire under `HALT`. An unmanaged position under a "safety halt" is the opposite of safe.

## CLI

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

## Python API

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

## DB schema

`kill_switch_log` is **append-only** — never UPDATE an existing row. The current state is always the most recent row ordered by `(created_at DESC, id DESC)`.

| Column | Type | Notes |
|---|---|---|
| `id` | `TEXT` (UUID) | Primary key |
| `state` | `TEXT` | `KillSwitchState` value: `NONE`, `HALT`, `FLATTEN` |
| `set_by` | `TEXT` | Operator name; must not be empty |
| `reason` | `TEXT` | Reason for the state change; must not be empty |
| `created_at` | `DATETIME` (tz) | Indexed; UTC timestamp of the INSERT |

Migration: `alembic/versions/004_kill_switch_log.py`

## Fail-safe contracts

The orchestrator enforces these at the top of each cycle. The core module (`killswitch.py`) propagates exceptions to the caller — fail-safe logic lives in `orchestrator.py`.

| Cycle | DB read failure | Rationale |
|---|---|---|
| Entry | Treat as `HALT` — fail closed | Cannot confirm `NONE`, so refuse new positions |
| Monitor | Treat as `NONE` — proceed with normal exits | Never auto-FLATTEN on an unreadable flag |
