from options_agent.config import Config
from options_agent.contracts.orchestrator import (
    ActionTaken,
    CycleResult,
    MonitorResult,
    ShortCircuitReason,
)
from options_agent.contracts.state import Position


def run_entry_cycle(config: Config) -> CycleResult:
    """Run one entry-reasoning cycle. WP-8 fills in the body.

    9-step flow:
    1. KILL_SWITCH — read kill-switch flag first. If HALT, return immediately
       with action_taken=NO_ACTION_GATED,
       short_circuit_reason=ShortCircuitReason.KILL_SWITCH_HALT.
       If FLATTEN, short-circuit with KILL_SWITCH_FLATTEN.
    2. RECONCILE — pull live account/positions/orders from Alpaca; diff
       against local DB; detect fills, expirations, assignments since the
       last run. Broker is source of truth for fills; DB for intent.
    3. GATES — pre-flight checks (market open, blackout windows, buying
       power, max open positions). If any gate fails, return immediately
       with action_taken=NO_ACTION_GATED and the matching ShortCircuitReason:
       MARKET_CLOSED, BLACKOUT_WINDOW, NO_BUYING_POWER, MAX_POSITIONS.
       If the action space is empty after gates, return with
       short_circuit_reason=EMPTY_ACTION_SPACE. These short-circuits avoid
       paying for an LLM call when there is nothing to do.
    4. ASSEMBLE — build the compact context bundle: portfolio state + net
       Greeks, then per-symbol: price, regime, IV rank, earnings-proximity
       flag, and a pre-filtered chain.
    5. REASON — single LLM call (agent/reasoner.py). Agent reads context
       via read-only tools and returns a TradeProposal. It cannot place
       orders.
    6. VALIDATE — deterministic proposal validation (risk/validator.py)
       returns a ValidationResult.
    7. SIZE — conviction + risk budget -> SizingResult (risk/sizing.py).
    8. EXECUTE — submit limit order at mid-or-better; record broker order ID.
    9. JOURNAL — write JournalRecord regardless of outcome, including
       NO_ACTION and REJECTED cycles. This step must not be skipped under
       any error path.

    Return contract:
        Returns a CycleResult for the immediate caller (scheduler / WP-8).
        The return value does NOT replace the journal. A JournalRecord is
        always written as a side-effect in step 9.
        journal_record_id on the result is the FK into that durable record.

    Error handling contract:
        Expected operational failures (data fetch timeout, broker rejection,
        tool error) must be caught and encoded in CycleResult.error so the
        scheduler can react and the loop continues. recoverable=True when the
        next cycle may succeed without intervention; False otherwise.
        Infrastructure failures that prevent journalling (DB unreachable,
        config corrupt) must raise — the loop should halt, not silently
        continue without recording.

    Short-circuit invariant:
        short_circuit_reason is not None implies
        action_taken == ActionTaken.NO_ACTION_GATED.
    """
    raise NotImplementedError


def run_monitor_cycle(positions: list[Position], config: Config) -> MonitorResult:
    """Run one monitor cycle over the supplied open positions. WP-8 fills in the body.

    Flow (for each position in positions):
    1. Evaluate stop-loss rule: if unrealized loss >= ExitPlan.stop_loss_mult
       * credit received, submit a closing order.
    2. Evaluate profit-target rule: if unrealized profit >=
       ExitPlan.profit_target_pct * est_max_profit, submit a closing order.
    3. Evaluate time-stop rule: if DTE (nearest_expiration - today) <=
       ExitPlan.time_stop_dte, submit a closing order.
    4. If any rule fires, submit a closing order via execution/broker.py and
       journal the exit decision (which rule fired, order submitted).
    5. Collect per-position errors into MonitorResult.errors.

    This function never touches the LLM and has no opinion on entries.

    Per-position error contract:
        One position failing to evaluate must NOT stop the others from having
        their stops checked. Errors are collected into MonitorResult.errors
        and the loop continues. The caller decides whether to alert.

    Idempotency:
        Running twice on the same positions must not submit duplicate closing
        orders. WP-8 must check for existing WORKING/PENDING_SUBMIT orders
        on a position before submitting a new close.

    The caller (WP-8 / scheduler) is responsible for fetching open positions
    from state and passing them here. This function does not read the DB.
    """
    raise NotImplementedError


__all__ = [
    "ActionTaken",
    "CycleResult",
    "MonitorResult",
    "ShortCircuitReason",
    "run_entry_cycle",
    "run_monitor_cycle",
]
