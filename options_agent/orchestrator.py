import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.engine import Engine

from options_agent.agent.stub_reasoner import stub_reasoner
from options_agent.config import Config
from options_agent.contracts.data import PortfolioState
from options_agent.contracts.journal import JournalRecord
from options_agent.contracts.orchestrator import (
    CycleError,
    CycleResult,
    CycleStage,
    MonitorResult,
    ShortCircuitReason,
)
from options_agent.contracts.state import (
    ActionTaken,
    AssetClass,
    ContextSnapshot,
    Decision,
    LegStatus,
    OrderStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.execution.broker import BrokerClient
from options_agent.execution.reconcile import reconcile as _reconcile
from options_agent.risk.sizing import size as _size
from options_agent.risk.validator import validate_structural
from options_agent.state.crud import insert_order, insert_position
from options_agent.state.db import build_engine, get_connection
from options_agent.state.journal import write_journal_record

logger = logging.getLogger(__name__)

# WP-0.5 slice constants -----------------------------------------------------
# _SLICE_LIMIT_PRICE removed: limit price is now config.slice_limit_price so
# the smoke test (WP-0.5.3) can pass -0.01 to guarantee a paper fill without
# adding a test-only parameter to the production signature.
# Sign convention: negative = net credit received. Matches:
#   - Alpaca mleg limit_price (build_multi_leg_request: sell legs contribute −mid)
#   - WP-0.3 Position.entry_net_amount (negative = credit received)
#   - compute_multi_leg_limit_price sign semantics
_SLICE_PROMPT_VERSION: str = "stub-0.5.2"
_SLICE_MODEL_ID: str = "stub"


def _stub_context_snapshot(now: datetime) -> ContextSnapshot:
    """Minimal context snapshot for the WP-0.5.2 stub slice.

    The real assembler (WP-6) populates assembled_context with live market data.
    """
    raw: dict = {}
    context_hash = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[
        :16
    ]
    return ContextSnapshot(
        assembled_context=raw,
        context_hash=context_hash,
        model_id=_SLICE_MODEL_ID,
        prompt_version=_SLICE_PROMPT_VERSION,
        assembled_at=now,
    )


def run_entry_cycle(
    config: Config,
    *,
    broker: BrokerClient | None = None,
    engine: Engine | None = None,
) -> CycleResult:
    """Run the WP-0.5.2 vertical-slice entry pipeline.

    WP-0.5.2 SLICE — manually-invoked, paper-only, unguarded.
    No kill-switch check, no pre-flight gates, no context assembly.
    WP-8 replaces this body with the full 9-step flow.

    Pipeline: stub_reasoner → validate_structural → size → submit_multi_leg
              → reconcile → write_journal_record.

    Optional DI parameters (broker, engine) are built from config when absent.
    Broker is constructed lazily — only if validation passes and sizing proceeds,
    so test paths that trigger an early return never need live credentials.

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
    if engine is None:
        engine = build_engine(config.db_url)

    now = datetime.now(UTC)
    cycle_id = str(uuid.uuid4())

    # ── Step 1: REASON ──────────────────────────────────────────────────────
    proposal = stub_reasoner()
    logger.info(
        "cycle %s: stub_reasoner emitted %s on %s",
        cycle_id,
        proposal.strategy,
        proposal.underlying,
    )

    # ── Step 2: VALIDATE ────────────────────────────────────────────────────
    validation = validate_structural(proposal, config.limits)
    logger.info(
        "cycle %s: validation %s",
        cycle_id,
        "passed"
        if validation.passed
        else f"REJECTED {[r.rule_id for r in validation.reasons]}",
    )

    if not validation.passed:
        rejection_ids = [r.rule_id for r in validation.reasons]
        decision = Decision(
            proposal=proposal,
            validation_result=validation,
            sizing_result=None,
            action_taken=ActionTaken.REJECTED,
        )
        journal_record = JournalRecord(
            cycle_id=cycle_id,
            timestamp=now,
            action_taken=ActionTaken.REJECTED,
            decision=decision,
            context_snapshot=_stub_context_snapshot(now),
            rejection_rule_ids=rejection_ids,
            limits_version=config.limits.limits_version,
            prompt_version=_SLICE_PROMPT_VERSION,
            model_id=_SLICE_MODEL_ID,
            strategy=proposal.strategy,
            underlying=proposal.underlying,
            conviction=proposal.conviction,
        )
        with get_connection(engine) as conn:
            write_journal_record(conn, journal_record)
        logger.info("cycle %s: REJECTED; journal written", cycle_id)
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.REJECTED,
            proposal=proposal,
            validation=validation,
            journal_record_id=cycle_id,
        )

    # ── Step 3: SIZE ────────────────────────────────────────────────────────
    # Broker constructed lazily so validation-failure tests need no credentials.
    if broker is None:
        broker = BrokerClient(config)

    account = broker.get_account()
    portfolio_state = PortfolioState(
        positions=[],
        # slice: Greeks unused by size(); do NOT read them off this object.
        account_equity=float(account.equity or "0"),
        buying_power=float(account.buying_power or "0"),
        options_buying_power=float(account.options_buying_power or "0"),
        unrealized_pnl=0.0,
        realized_pnl_today=0.0,
        approval_level=int(account.options_approved_level or 0),
        net_dollar_delta=0.0,
        net_dollar_gamma=0.0,
        net_dollar_theta=0.0,
        net_dollar_vega=0.0,
    )

    sizing = _size(proposal, portfolio_state, config.limits)
    logger.info(
        "cycle %s: sized to %d contracts (capped=%s binding=%s)",
        cycle_id,
        sizing.contracts,
        sizing.capped_to_zero,
        sizing.binding_constraint,
    )

    if sizing.capped_to_zero:
        decision = Decision(
            proposal=proposal,
            validation_result=validation,
            sizing_result=sizing,
            action_taken=ActionTaken.SIZED_TO_ZERO,
        )
        journal_record = JournalRecord(
            cycle_id=cycle_id,
            timestamp=now,
            action_taken=ActionTaken.SIZED_TO_ZERO,
            decision=decision,
            context_snapshot=_stub_context_snapshot(now),
            limits_version=config.limits.limits_version,
            prompt_version=_SLICE_PROMPT_VERSION,
            model_id=_SLICE_MODEL_ID,
            strategy=proposal.strategy,
            underlying=proposal.underlying,
            conviction=proposal.conviction,
        )
        with get_connection(engine) as conn:
            write_journal_record(conn, journal_record)
        logger.info("cycle %s: SIZED_TO_ZERO; journal written", cycle_id)
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.SIZED_TO_ZERO,
            proposal=proposal,
            validation=validation,
            sizing=sizing,
            journal_record_id=cycle_id,
        )

    # ── Step 4: EXECUTE ─────────────────────────────────────────────────────
    limit_price = config.slice_limit_price
    position_id = str(uuid.uuid4())
    order = broker.submit_multi_leg(
        proposal, sizing.contracts, limit_price, position_id
    )
    logger.info(
        "cycle %s: order submitted broker_id=%s status=%s",
        cycle_id,
        order.broker_order_id,
        order.status,
    )

    if order.status == OrderStatus.REJECTED:
        # Broker rejected the order synchronously — no Position is persisted.
        # action_taken = EXECUTION_FAILED distinguishes broker rejection (post-sizing)
        # from validation rejection (pre-sizing), per the WP-0.4 ActionTaken contract.
        cycle_error = CycleError(
            stage=CycleStage.EXECUTE,
            message=(
                f"Broker rejected order broker_id={order.broker_order_id!r} "
                f"status_raw={order.broker_status_raw!r}"
            ),
            recoverable=False,
        )
        decision = Decision(
            proposal=proposal,
            validation_result=validation,
            sizing_result=sizing,
            action_taken=ActionTaken.EXECUTION_FAILED,
        )
        journal_record = JournalRecord(
            cycle_id=cycle_id,
            timestamp=now,
            action_taken=ActionTaken.EXECUTION_FAILED,
            decision=decision,
            context_snapshot=_stub_context_snapshot(now),
            limits_version=config.limits.limits_version,
            prompt_version=_SLICE_PROMPT_VERSION,
            model_id=_SLICE_MODEL_ID,
            strategy=proposal.strategy,
            underlying=proposal.underlying,
            conviction=proposal.conviction,
        )
        with get_connection(engine) as conn:
            write_journal_record(conn, journal_record)
        logger.warning(
            "cycle %s: EXECUTION_FAILED broker_id=%s; journal written",
            cycle_id,
            order.broker_order_id,
        )
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.EXECUTION_FAILED,
            proposal=proposal,
            validation=validation,
            sizing=sizing,
            error=cycle_error,
            journal_record_id=cycle_id,
        )

    # ── Steps 5–7: PERSIST, RECONCILE, JOURNAL ──────────────────────────────
    # Insert Position first (PENDING_OPEN), then Order, to satisfy the FK
    # constraint (orders.position_id → positions.id).
    # order.id is only known after submit returns; it becomes opening_order_id.
    position = Position(
        id=position_id,
        underlying=proposal.underlying,
        strategy=proposal.strategy,
        legs=[
            PositionLeg(
                leg=leg, filled_qty=0, avg_fill_price=0.0, status=LegStatus.OPEN
            )
            for leg in proposal.legs
        ],
        quantity=sizing.contracts,
        # entry_net_amount = intended limit price; negative = credit received (WP-0.3).
        entry_net_amount=limit_price,
        current_mark=limit_price,
        marked_at=now,
        unrealized_pnl=0.0,
        realized_pnl=None,
        exit_plan=proposal.exit_plan,
        status=PositionStatus.PENDING_OPEN,
        opened_at=now,
        closed_at=None,
        nearest_expiration=min(leg.expiration for leg in proposal.legs),
        est_max_loss=proposal.est_max_loss,
        est_max_profit=proposal.est_max_profit,
        opening_order_id=order.id,
        asset_class=AssetClass.OPTION_STRATEGY,
        equity_legs=[],
        assigned_from_position_id=None,
    )

    decision = Decision(
        proposal=proposal,
        validation_result=validation,
        sizing_result=sizing,
        action_taken=ActionTaken.OPENED,
    )
    journal_record = JournalRecord(
        cycle_id=cycle_id,
        timestamp=now,
        action_taken=ActionTaken.OPENED,
        decision=decision,
        context_snapshot=_stub_context_snapshot(now),
        position_ids=[position_id],
        order_ids=[order.id],
        limits_version=config.limits.limits_version,
        prompt_version=_SLICE_PROMPT_VERSION,
        model_id=_SLICE_MODEL_ID,
        strategy=proposal.strategy,
        underlying=proposal.underlying,
        net_delta_at_open=proposal.net_delta,
        conviction=proposal.conviction,
    )

    with get_connection(engine) as conn:
        insert_position(conn, position)
        insert_order(conn, order)
        # reconcile() detects fills and transitions Position: PENDING_OPEN → OPEN.
        _reconcile(broker, conn)
        write_journal_record(conn, journal_record)

    logger.info(
        "cycle %s: OPENED broker_order_id=%s; journal written",
        cycle_id,
        order.broker_order_id,
    )
    return CycleResult(
        cycle_id=cycle_id,
        action_taken=ActionTaken.OPENED,
        proposal=proposal,
        validation=validation,
        sizing=sizing,
        journal_record_id=cycle_id,
    )


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
    "CycleError",
    "CycleResult",
    "CycleStage",
    "MonitorResult",
    "ShortCircuitReason",
    "run_entry_cycle",
    "run_monitor_cycle",
]
