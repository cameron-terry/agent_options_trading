# Monitor — Exit Rules

**Module:** `options_agent/monitor/`  
**Credentials required:** none (exit rule evaluators are pure logic against cached position state)  
**Status:** in progress (WP-5.1 stop-loss ✓, WP-5.2 profit-target ✓; WP-5.3 DTE, WP-5.4 idempotency, WP-5.5 cycle body pending)

The fast, deterministic exit loop. No LLM, no context assembly — per-position rule evaluation that runs every 1–5 minutes during market hours. Rules read cached state from the last reconcile cycle; the monitor never makes live broker quote calls for individual positions.

## Sub-modules

| File | Responsibility |
|---|---|
| `exits.py` | Per-position stop-loss and profit-target evaluators; `MarkStaleError` |

## Design invariants

**Reconcile-at-cycle-top is mandatory.** All exit evaluators read `pos.unrealized_pnl` and `pos.current_mark` from the last-reconciled `Position`. If `pos.marked_at` is older than `max_mark_age` (the acceptable staleness window), evaluators raise `MarkStaleError` — a surfaced, alertable error, not a silent no-op. WP-5.5 / WP-8 must guarantee reconcile runs before any evaluator.

**Inline `PENDING_CLOSE` guard is the idempotency floor.** Each evaluator checks `pos.status in _SKIPPABLE_STATUSES` before evaluating. This prevents re-submission while a closing order is pending fill. WP-5.4 will formalize `has_pending_close(position_id)`; until then the inline status check is load-bearing.

**EQUITY positions always skip.** All evaluators return `None` immediately if `pos.asset_class != OPTION_STRATEGY`. EQUITY positions (from assignment) have no `exit_plan`.

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

## Evaluator ordering in a cycle

Both `check_stop_loss` and `check_profit_target` check `pos.status` at entry. When the monitor loop calls them sequentially for the same position:

- P&L cannot simultaneously be at stop-loss (negative) and profit-target (positive), so both cannot fire on P&L grounds.
- If either fires and sets `PENDING_CLOSE`, the other sees that status and bails — ordering is robust regardless of which runs first.

The freshness check (`MarkStaleError`) applies to each evaluator independently. WP-5.5 should surface this once per position per cycle, not per evaluator call.
