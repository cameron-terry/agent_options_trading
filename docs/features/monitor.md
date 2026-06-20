# Monitor — Exit Rules

**Module:** `options_agent/monitor/`  
**Credentials required:** none (exit rule evaluators are pure logic against cached position state)  
**Status:** in progress (WP-5.1 stop-loss ✓, WP-5.2 profit-target ✓, WP-5.3 DTE ✓, WP-5.4 idempotency ✓; WP-5.5 cycle body pending)

The fast, deterministic exit loop. No LLM, no context assembly — per-position rule evaluation that runs every 1–5 minutes during market hours. Rules read cached state from the last reconcile cycle; the monitor never makes live broker quote calls for individual positions.

## Sub-modules

| File | Responsibility |
|---|---|
| `exits.py` | Per-position stop-loss, profit-target, and DTE time-stop evaluators; `MarkStaleError` |

## Design invariants

**Reconcile-at-cycle-top is mandatory for price-based exits.** `check_stop_loss` and `check_profit_target` read `pos.unrealized_pnl` and `pos.current_mark` from the last-reconciled `Position`. If `pos.marked_at` is older than `max_mark_age`, they raise `MarkStaleError` — a surfaced, alertable error, not a silent no-op. `check_time_stop` does not enforce staleness (DTE is mark-independent), but still benefits from fresh status reads. WP-5.5 / WP-8 must guarantee reconcile runs before any evaluator call.

**Two-layer idempotency prevents duplicate closing orders.** Each evaluator checks two complementary guards before calling `submit()`:

1. **Position-status layer** (`_SKIPPABLE_STATUSES`): cheap in-memory check. Skips positions in `PENDING_CLOSE`, `CLOSED`, etc. Catches the normal case where position status correctly reflects a prior close.

2. **Order-table layer** (`has_pending_close`): queries the `Order` table for any non-terminal `CLOSE` or `ROLL` order on the position. Catches the desync window where `insert_order` succeeded but `update_position` → `PENDING_CLOSE` did not (e.g., a crash between the two writes). `ROLL` is included because a working roll is mechanically closing the position. `PARTIALLY_FILLED` is in the pending set — stacking a second close on top of a partial-fill close would double the closing quantity.

The two layers resolve disagreement toward caution: if either says a close is in flight, no new close is submitted. The position-status check short-circuits before the Order-table query on the common path.

**EQUITY positions always skip.** All evaluators return `None` immediately if `pos.asset_class != OPTION_STRATEGY`. EQUITY positions (from assignment) have no `exit_plan`.

## Idempotency guard (`has_pending_close`)

**Signature:** `has_pending_close(conn: Connection, position_id: str) -> bool`  
**Location:** `state/crud.py` (imported by `monitor/exits.py`)

Returns `True` if any non-terminal `CLOSE` or `ROLL` order exists for `position_id` in the `Order` table. Called by all three exit evaluators after the position-status check and (for `check_stop_loss` / `check_profit_target`) after `MarkStaleError` — ensuring a stale Order table is never consulted.

```python
from options_agent.state.crud import has_pending_close

with get_connection(engine) as conn:
    if has_pending_close(conn, pos.id):
        # a closing order is already in flight — skip
        pass
```

**Pending statuses:** `PENDING_SUBMIT`, `WORKING`, `PARTIALLY_FILLED`  
**Closing roles:** `CLOSE`, `ROLL` (defined as `_EXPOSURE_CLOSING_ROLES` in `state/crud.py`)

## Stop-loss (`check_stop_loss`)

**Trigger formula:** `unrealized_pnl <= -(stop_loss_max_loss_fraction × est_max_loss)`

`est_max_loss` is always positive and represents the maximum the position can lose, so the threshold is always a reachable negative P&L value — uniform across credit and debit strategies.

```python
from datetime import UTC, datetime, timedelta
from options_agent.monitor.exits import check_stop_loss, MarkStaleError

now = datetime.now(UTC)
max_mark_age = timedelta(minutes=10)

# conn: SQLAlchemy Connection (from get_connection(engine))
# broker: BrokerClient instance
# pos: Position with fresh marked_at (< max_mark_age old)

try:
    order = check_stop_loss(pos, conn, broker, now, max_mark_age)
except MarkStaleError:
    # reconcile did not run at cycle-top — surface this as an alert
    raise

if order is not None:
    # stop-loss triggered: closing order submitted, pos.status → PENDING_CLOSE
    print(f"Stop-loss fired: order {order.id}")
```

## Profit-target (`check_profit_target`)

**Trigger formula:** `unrealized_pnl >= profit_target_pct × est_max_profit`

Both sides are always positive. Unlike the stop-loss formula (which required a WP-0 amendment for credit/debit symmetry), profit-target generalizes cleanly: `est_max_profit` is always the maximum gain regardless of strategy direction, so no sign adjustment is needed.

**No tolerance band.** The trigger uses exact `>=`. A tolerance band would cause an early exit (closing at `pct − 1%` leaves money on the table on every winning trade) to solve an oscillation problem that doesn't exist: the first crossing terminates the position via `PENDING_CLOSE`, so there is no pre-trigger oscillation risk across cycles.

```python
from options_agent.monitor.exits import check_profit_target, MarkStaleError

try:
    order = check_profit_target(pos, conn, broker, now, max_mark_age)
except MarkStaleError:
    raise

if order is not None:
    # profit-target triggered: closing order submitted, pos.status → PENDING_CLOSE
    print(f"Profit-target fired: order {order.id}")
```

## DTE time-stop (`check_time_stop`)

**Trigger formula:** `(pos.nearest_expiration - today_ET).days <= time_stop_dte`

`nearest_expiration` is a denormalized field set at position-open time (the minimum expiration across all legs). `today` is derived from `now` converted to `America/New_York` — not UTC — because UTC rolls to the next calendar day at ~7–8 pm ET, which would compute DTE as one day too few for several hours each evening and fire the time-stop a full day early.

`time_stop_dte` is **calendar days** (not trading days), consistent with how `ExitPlan` emits it (e.g., the standard 21-DTE close means 21 calendar days to expiration).

**Monotonic re-trigger.** Unlike price-based exits, the DTE condition only tightens: once `min_dte <= time_stop_dte`, it stays true every cycle until fill. The `PENDING_CLOSE` guard in `_SKIPPABLE_STATUSES` is therefore non-optional — without it, every monitor cycle from the trigger day onward would re-submit a closing order.

**Roll caveat.** `nearest_expiration` is denormalized at open time. If rolling is ever implemented, it must be recomputed on the roll or this evaluator will read a stale expiration date.

```python
from datetime import UTC, datetime, timedelta
from options_agent.monitor.exits import check_time_stop

now = datetime.now(UTC)
max_mark_age = timedelta(minutes=10)  # accepted for API uniformity; not enforced here

order = check_time_stop(pos, conn, broker, now, max_mark_age)

if order is not None:
    # DTE threshold breached: closing order submitted, pos.status → PENDING_CLOSE
    print(f"Time-stop fired: order {order.id}")
```

## Evaluator ordering in a cycle

All three evaluators check `pos.status in _SKIPPABLE_STATUSES` at entry. When the monitor loop calls them sequentially for the same position:

- P&L cannot simultaneously be at stop-loss (negative) and profit-target (positive), so those two cannot fire on P&L grounds on the same cycle.
- If any evaluator fires and sets `PENDING_CLOSE`, the subsequent evaluators see that status and bail — ordering is robust regardless of which runs first.
- The DTE evaluator is independent of mark price, so it can fire in the same cycle as a price-based evaluator is blocked by staleness. The `PENDING_CLOSE` guard handles that case cleanly.

The `MarkStaleError` check applies only to `check_stop_loss` and `check_profit_target`. WP-5.5 should surface it once per position per cycle, not per evaluator call.
