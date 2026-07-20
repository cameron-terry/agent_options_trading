# Monitor — Exit Rules

**Module:** `options_agent/monitor/`
**Credentials required:** none (exit rule evaluators are pure logic against cached position state)
**Status:** complete

The fast, deterministic exit loop. No LLM, no context assembly — per-position rule evaluation that runs every 1–5 minutes during market hours. Rules read cached state from the last reconcile; the monitor never makes live broker quote calls for individual positions. The cycle wiring (`run_monitor_cycle` in `orchestrator.py`) is summarized in [orchestrator.md](orchestrator.md); kill-switch states are in the [kill-switch runbook](../runbook_kill_switch.md).

## Sub-modules

| File | Responsibility |
|---|---|
| `exits.py` | Per-position stop-loss, profit-target, and DTE time-stop evaluators; `flatten_position`; `MarkStaleError` |

## Design invariants

**Reconcile-at-cycle-top is mandatory for price-based exits.** `check_stop_loss` and `check_profit_target` read `pos.unrealized_pnl` / `pos.current_mark` from the last-reconciled `Position`. If `pos.marked_at` is older than `max_mark_age`, they raise `MarkStaleError` — a surfaced, alertable error, not a silent no-op. The cycle catches it once per position, records it in `MonitorResult.errors`, and continues to the next position. `check_time_stop` is mark-independent and does not enforce staleness.

**Two-layer idempotency prevents duplicate closing orders.** Each evaluator checks two complementary guards before submitting:

1. **Position-status layer** (`_SKIPPABLE_STATUSES`): cheap in-memory skip of `PENDING_CLOSE`, `CLOSED`, etc.
2. **Order-table layer** (`has_pending_close` in `state/crud.py`): any non-terminal `CLOSE` or `ROLL` order on the position blocks a new close — this catches the crash window where the order insert succeeded but the position-status update did not. `ROLL` counts because a working roll is mechanically closing the position; `PARTIALLY_FILLED` counts because stacking a second close would double the closing quantity.

Disagreement between the layers resolves toward caution: if either says a close is in flight, none is submitted.

**EQUITY positions always skip — and are logged.** Evaluators return `None` for `asset_class != OPTION_STRATEGY` and log the position ID each cycle, so assignment-created equity positions stay visible even though the monitor takes no action on them (disposition is a future orchestration concern).

## Exit rules

All three evaluators share the signature `check_*(pos, conn, broker, now, max_mark_age) -> Order | None` — a returned `Order` means a closing order was submitted and the position moved to `PENDING_CLOSE`.

| Rule | Trigger | Notes |
|---|---|---|
| Stop-loss | `unrealized_pnl <= -(stop_loss_max_loss_fraction × est_max_loss)` | `est_max_loss` is always positive, so the threshold is a reachable negative P&L — uniform across credit and debit strategies |
| Profit-target | `unrealized_pnl >= profit_target_pct × est_max_profit` | Exact `>=`, no tolerance band — the first crossing terminates the position, so there is no oscillation risk to solve |
| DTE time-stop | `(pos.nearest_expiration - today_ET).days <= time_stop_dte` | Calendar days; `today` is computed in `America/New_York`, not UTC — UTC rolls over at ~7–8 pm ET and would fire a day early. Monotonic once true, so the `PENDING_CLOSE` guard is what stops per-cycle re-submission |

Evaluator ordering within a cycle is robust in any order: the P&L rules can't both fire, and whichever fires first flips the position to `PENDING_CLOSE`, which the rest skip.

**Roll caveat:** `nearest_expiration` is denormalized at open time. If rolling is ever implemented it must be recomputed on roll, or the time-stop reads a stale date.

## FLATTEN (`flatten_position`)

Under kill-switch FLATTEN the cycle calls `flatten_position` for every open position: submits a closing order bypassing all rule thresholds *and* the mark-staleness check (acting on a stale mark is correct in an emergency close). Guards still apply: non-option positions, positions without an exit plan, and already-closing positions return `None`.

## Position lifecycle alerts and outcome journaling

Two distinct alert events per position lifecycle, never in the same cycle:

| Event | When | Severity |
|---|---|---|
| `EXIT_SUBMITTED` | Closing order sent to broker (may still be WORKING) | INFO |
| `FILL` | A later cycle's reconcile confirms the close, with real `realized_pnl`/`fill_price` | INFO |

`OutcomeRecord` is deliberately **not** written at submit time — it is written in the finalize step of the cycle whose reconcile confirms the fill, using the real `net_fill_price`. The evaluator tags `Order.exit_reason` at submit (`ExitReason.STOP_LOSS` / `PROFIT_TARGET` / `DTE` / `FLATTEN`, stored on both `orders` and `outcome_records`), and the finalize step propagates it onto the `OutcomeRecord`, enabling `GROUP BY exit_reason` analytics.

## Assignment handling

Assignments detected during reconcile (`StateDiff.assigned_positions`) cause the cycle to engage `HALT`, dispatch a `KILL_SWITCH_CHANGE` CRITICAL alert, and journal a gated record — then **continue**, not return: HALT blocks new entries, but the remaining options book is still managed (stops evaluated, fills confirmed, outcomes journaled).
