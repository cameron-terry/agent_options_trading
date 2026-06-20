import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import exchange_calendars as xcals
from sqlalchemy.engine import Engine

from options_agent.agent.stub_reasoner import stub_reasoner
from options_agent.config import Config
from options_agent.contracts.data import PortfolioState
from options_agent.contracts.journal import (
    JournalRecord,
    OutcomeEventType,
    OutcomeRecord,
)
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
    KillSwitchState,
    LegStatus,
    OrderStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.execution.broker import BrokerClient
from options_agent.execution.reconcile import reconcile as _reconcile
from options_agent.monitor.exits import (
    MarkStaleError,
    check_profit_target,
    check_stop_loss,
    check_time_stop,
    flatten_position,
)
from options_agent.obs.killswitch import get_current_state, is_flatten, is_halted
from options_agent.risk.gates import market_is_open
from options_agent.risk.sizing import size as _size
from options_agent.risk.validator import validate_structural
from options_agent.state.crud import (
    get_closing_order,
    insert_order,
    insert_position,
    list_open_positions,
)
from options_agent.state.db import build_engine, get_connection
from options_agent.state.journal import write_journal_record, write_outcome_record

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

    # ── Step 1: KILL_SWITCH ──────────────────────────────────────────────────
    # Fail-safe: if the DB read fails we cannot confirm the switch is NONE,
    # so we fail closed — treat as HALT and block the entry cycle.
    # Infrastructure failures that prevent reading the safety lever must halt,
    # not silently allow trading to continue.
    try:
        with get_connection(engine) as _ks_conn:
            _ks_state = get_current_state(_ks_conn)
    except Exception:
        logger.critical(
            "cycle %s: kill-switch read failed — failing closed (treating as HALT)",
            cycle_id,
        )
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.NO_ACTION_GATED,
            short_circuit_reason=ShortCircuitReason.KILL_SWITCH_HALT,
        )

    if is_halted(_ks_state):
        _sc_reason = (
            ShortCircuitReason.KILL_SWITCH_FLATTEN
            if is_flatten(_ks_state)
            else ShortCircuitReason.KILL_SWITCH_HALT
        )
        logger.warning(
            "cycle %s: kill switch %s — aborting entry cycle", cycle_id, _ks_state
        )
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.NO_ACTION_GATED,
            short_circuit_reason=_sc_reason,
        )

    # ── Step 2 (stub): REASON ───────────────────────────────────────────────
    # WP-8 expands this to the full 9-step flow. Steps below retain their
    # original numbers from the WP-0.5 stub until WP-8 wires them correctly.
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


def run_monitor_cycle(
    config: Config,
    *,
    broker: BrokerClient | None = None,
    engine: Engine | None = None,
    _now: datetime | None = None,
) -> MonitorResult:
    """Run one monitor cycle: reconcile state, then evaluate exit rules.

    WP-5.5 implementation.

    Sequence:
    1. Kill-switch check.
    2. Market open pre-flight — no-op (returns empty MonitorResult) when closed.
    3. Reconcile state via broker to refresh position marks and detect fills.
    4. Read all open positions from DB (fresh after reconcile).
    5. For each position:
       - FLATTEN mode: submit close immediately, bypassing rule evaluation.
       - Normal mode: evaluate stop-loss → profit-target → DTE in order;
         first rule that triggers submits a close and skips the remainder.
    6. Finalize: write OutcomeRecords for positions that transitioned
       PENDING_CLOSE → CLOSED during step 3's reconcile.
    7. Return MonitorResult.

    Kill-switch semantics:
        NONE    — normal exit evaluation.
        HALT    — monitor runs normally (unmanaged positions under HALT defeats
                  the purpose of a halt); entries are blocked by entry cycle.
        FLATTEN — all open OPTION_STRATEGY positions closed immediately,
                  bypassing stop/target/DTE checks. EQUITY disposition is
                  WP-8's responsibility.

    Fail-safe on kill-switch read error:
        Proceed with NONE semantics. Never auto-FLATTEN on an unreadable flag;
        FLATTEN is an explicit operator decision, not an inference from a broken
        DB connection.

    Per-position error isolation:
        One failing position (MarkStaleError, broker error, etc.) records to
        MonitorResult.errors and the loop continues — other positions still
        have their exits checked. The caller decides whether to alert.

    Idempotency:
        Running twice must not submit duplicate closing orders. exits.py guards
        this via both _SKIPPABLE_STATUSES (position status check) and
        has_pending_close (Order table check), so re-runs after a trigger are safe.

    Outcome journaling:
        OutcomeRecords are NOT written at close-submit time because the fill has
        not occurred yet. They are written in the finalize step (step 6) for
        positions that reconcile confirmed as CLOSED this cycle, using the
        actual fill price from the filled Order. Positions triggered in this
        cycle will get their OutcomeRecord on the next (or a later) cycle when
        their closing order fills.

    _now is a test-only clock override (same pattern as reconcile's _clock).
    Production callers must not pass it. Use it in tests to inject a
    market-hours timestamp so the market_is_open gate passes without mocking.
    """
    if engine is None:
        engine = build_engine(config.db_url)

    # ── Step 1: KILL_SWITCH ──────────────────────────────────────────────────
    # Fail-safe: if the DB read fails, proceed with normal exit logic (NONE).
    # We never auto-FLATTEN on an unreadable flag; FLATTEN must be explicit.
    try:
        with get_connection(engine) as _ks_conn:
            _ks_state = get_current_state(_ks_conn)
    except Exception:
        logger.critical(
            "run_monitor_cycle: kill-switch read failed — proceeding with normal "
            "exit logic (fail-safe: never auto-FLATTEN on unreadable flag)"
        )
        _ks_state = KillSwitchState.NONE

    # Under HALT, flatten_mode is False — exits still evaluate normally.
    _flatten_mode = is_flatten(_ks_state)

    # ── Step 2: CLOCK + MARKET OPEN ─────────────────────────────────────────
    now = _now if _now is not None else datetime.now(UTC)

    _calendar = xcals.get_calendar(config.exchange_calendar)
    _market_open, _market_reason = market_is_open(now, _calendar)
    if not _market_open:
        logger.info("run_monitor_cycle: SKIP (market closed) — %s", _market_reason)
        return MonitorResult(
            positions_evaluated=0,
            exits_triggered=[],
            orders_submitted=[],
            errors=[],
        )

    logger.debug(
        "run_monitor_cycle: kill_switch=%s flatten_mode=%s",
        _ks_state,
        _flatten_mode,
    )

    # ── Step 3: BROKER + RECONCILE ──────────────────────────────────────────
    # Reconcile refreshes position marks and detects fills from the previous
    # cycle. The state_diff returned here is used in step 6 to finalize
    # OutcomeRecords for positions whose closing orders filled this cycle.
    if broker is None:
        broker = BrokerClient(config)

    with get_connection(engine) as _rec_conn:
        state_diff = _reconcile(broker, _rec_conn, _clock=now)

    # ── Step 4: FRESH POSITIONS ──────────────────────────────────────────────
    # Re-read after reconcile so marks are current. The positions parameter was
    # dropped from this function (WP-5.5 amendment to the WP-0.7 signature)
    # because keeping a silently-overridden parameter is worse than an honest
    # API: callers that passed a list thinking it was acted on were wrong.
    with get_connection(engine) as _pos_conn:
        open_positions = list_open_positions(_pos_conn)

    # ── Step 5: EXIT EVALUATION LOOP ────────────────────────────────────────
    max_mark_age = timedelta(minutes=config.monitor_max_mark_age_minutes)
    exits_triggered: list[str] = []
    orders_submitted: list[str] = []
    errors: list[CycleError] = []

    for pos in open_positions:
        try:
            with get_connection(engine) as conn:
                if _flatten_mode:
                    order = flatten_position(pos, conn, broker, now)
                else:
                    order = (
                        check_stop_loss(pos, conn, broker, now, max_mark_age)
                        or check_profit_target(pos, conn, broker, now, max_mark_age)
                        or check_time_stop(pos, conn, broker, now, max_mark_age)
                    )

                if order is not None:
                    exits_triggered.append(pos.id)
                    orders_submitted.append(order.id)
        except MarkStaleError as exc:
            logger.error(
                "run_monitor_cycle: mark stale for position %s — %s (run reconcile "
                "at cycle-top to fix; this position was skipped this cycle)",
                pos.id,
                exc,
            )
            errors.append(
                CycleError(
                    stage=CycleStage.STOP_EVAL,
                    message=str(exc),
                    recoverable=True,
                )
            )
        except Exception as exc:
            logger.error(
                "run_monitor_cycle: unexpected error evaluating position %s — %s",
                pos.id,
                exc,
            )
            errors.append(
                CycleError(
                    stage=CycleStage.STOP_EVAL,
                    message=f"Position {pos.id}: {exc}",
                    recoverable=True,
                )
            )

    # ── Step 6: FINALIZE — write OutcomeRecords for fills detected this cycle ─
    # These are positions whose closing orders filled during step 3's reconcile.
    # We write the OutcomeRecord NOW (not at trigger time) so realized_pnl uses
    # the actual fill price, not a placeholder. The exit_reason is carried on
    # the closing Order written at trigger time.
    _finalize_closed_positions(engine, now, state_diff.closed_positions)

    return MonitorResult(
        positions_evaluated=len(open_positions),
        exits_triggered=exits_triggered,
        orders_submitted=orders_submitted,
        errors=errors,
    )


def _finalize_closed_positions(
    engine: Engine,
    now: datetime,
    closed_positions: list[Position],
) -> None:
    """Write OutcomeRecords for positions that reconcile confirmed as CLOSED.

    Called at the end of run_monitor_cycle with StateDiff.closed_positions —
    the list of positions that transitioned PENDING_CLOSE → CLOSED during this
    cycle's reconcile pass.

    Matches each closed position to its filled closing Order (role=CLOSE,
    status=FILLED) to retrieve the actual fill price and exit_reason. The
    realized_pnl is computed from entry_net_amount and the fill price using
    the sign convention: realized_pnl = (-entry_net_amount - fill_price) *
    quantity * 100.

    Positions with no matching closing Order (e.g. expirations handled by
    WP-1.5 reconcile) are skipped — their OutcomeRecords are written by the
    expiry/assignment path.
    """
    if not closed_positions:
        return

    for pos in closed_positions:
        try:
            with get_connection(engine) as conn:
                closing_order = get_closing_order(conn, pos.id)
                if closing_order is None:
                    logger.debug(
                        "_finalize_closed_positions: no closing order for "
                        "position %s — skipping OutcomeRecord",
                        pos.id,
                    )
                    continue

                fill_price = closing_order.net_fill_price
                if fill_price is None:
                    logger.warning(
                        "_finalize_closed_positions: closing order %s for "
                        "position %s has no fill price — deferring OutcomeRecord "
                        "until fill price is available",
                        closing_order.id,
                        pos.id,
                    )
                    continue

                realized_pnl = (-pos.entry_net_amount - fill_price) * pos.quantity * 100
                outcome = OutcomeRecord(
                    id=str(uuid.uuid4()),
                    position_id=pos.id,
                    event_type=OutcomeEventType.FULL_CLOSE,
                    recorded_at=now,
                    contracts_closed=pos.quantity,
                    realized_pnl=realized_pnl,
                    fill_price=fill_price,
                    closing_order_id=closing_order.id,
                    exit_reason=closing_order.exit_reason,
                )
                write_outcome_record(conn, outcome)
                logger.info(
                    "_finalize_closed_positions: OutcomeRecord written for "
                    "position=%s exit_reason=%s realized_pnl=%.2f fill_price=%.4f",
                    pos.id,
                    closing_order.exit_reason,
                    realized_pnl,
                    fill_price,
                )
        except Exception as exc:
            logger.error(
                "_finalize_closed_positions: failed to write OutcomeRecord for "
                "position %s — %s (non-fatal; position is already CLOSED in DB)",
                pos.id,
                exc,
            )


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
