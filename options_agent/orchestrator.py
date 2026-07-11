import hashlib
import json
import logging
import uuid
from datetime import UTC, date, datetime, timedelta

import exchange_calendars as xcals
from sqlalchemy.engine import Engine

from options_agent.agent.reasoner import ReasonerError, reason
from options_agent.agent.tools import TOOL_GET_FILTERED_CHAIN
from options_agent.agent.tools_mock import MOCK_TOOL_IMPLS, ToolImpl
from options_agent.config import Config
from options_agent.context.assembler import assemble_context, to_context_snapshot
from options_agent.contracts.alerts import AlertEvent, AlertEventType, AlertSeverity
from options_agent.contracts.data import FilteredChain
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
from options_agent.contracts.proposal import TradeProposal
from options_agent.contracts.results import (
    RejectionReason,
    Severity,
    ValidationResult,
    ValidationRuleId,
)
from options_agent.contracts.state import (
    ActionTaken,
    AssetClass,
    AssignmentEvent,
    ContextSnapshot,
    Decision,
    KillSwitchState,
    LegStatus,
    OrderRole,
    OrderStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.data.greeks_iv import get_atm_iv
from options_agent.data.iv_rank import record_daily_iv
from options_agent.data.providers.alpaca_data import AlpacaDataClient
from options_agent.data.tools import build_real_tool_impls, load_universe
from options_agent.execution.broker import BrokerClient
from options_agent.execution.orders import (
    compute_limit_price,
    compute_multi_leg_limit_price,
)
from options_agent.execution.reconcile import reconcile as _reconcile
from options_agent.monitor.exits import (
    MarkStaleError,
    check_profit_target,
    check_stop_loss,
    check_time_stop,
    flatten_position,
    reprice_stale_close_orders,
)
from options_agent.obs.alerts import AlertDispatcher
from options_agent.obs.killswitch import (
    get_current_state,
    is_flatten,
    is_halted,
    set_state,
)
from options_agent.risk.gates import (
    has_buying_power,
    market_is_open,
    under_position_cap,
    within_blackout_window,
)
from options_agent.risk.sizing import size as _size
from options_agent.risk.structure import (
    StructureMetrics,
    apply_structure_metrics,
    compute_structure_metrics,
)
from options_agent.risk.validator import (
    validate_market_access,
    validate_risk_caps,
    validate_structural,
)
from options_agent.state.crud import (
    get_closing_order,
    insert_order,
    insert_position,
    list_open_positions,
    list_pending_orders,
)
from options_agent.state.db import build_engine, get_connection
from options_agent.state.journal import write_journal_record, write_outcome_record

logger = logging.getLogger(__name__)


def _gated_context_snapshot(config: Config, now: datetime) -> ContextSnapshot:
    """Minimal ContextSnapshot for cycles that short-circuit before assembly."""
    raw: dict = {}
    context_hash = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[
        :16
    ]
    return ContextSnapshot(
        assembled_context=raw,
        context_hash=context_hash,
        model_id=config.model_id,
        prompt_version=config.playbook.playbook_version,
        assembled_at=now,
    )


def _dispatch(dispatcher: AlertDispatcher | None, event: AlertEvent) -> None:
    """Null-safe dispatch: enqueues if dispatcher active, silently skips otherwise."""
    if dispatcher is not None:
        dispatcher.dispatch(event)


def _build_tool_impls(
    config: Config,
    engine: Engine,
    broker: BrokerClient,
) -> dict[str, ToolImpl]:
    """Return the tool implementation map for the current run mode.

    Selection logic (use_real_data_tools is independent of alpaca_paper):
      use_real_data_tools=False + alpaca_paper=True  → MOCK_TOOL_IMPLS (dev/CI)
      use_real_data_tools=False + alpaca_paper=False → hard error (Config validator
          already rejects this; guard here is a belt-and-suspenders check)
      use_real_data_tools=True                       → build_real_tool_impls (paper
          or live — the 90-day paper run uses paper money + real data)

    The live + mocks combination is rejected at Config construction time by
    _live_requires_real_data(), so it cannot reach here in normal operation.
    """
    if config.use_real_data_tools:
        logger.info(
            "Using real WP-3 data tools (use_real_data_tools=True, paper=%s)",
            config.alpaca_paper,
        )
        return build_real_tool_impls(config, engine, broker)

    # mock path: only reachable when use_real_data_tools=False
    if not config.alpaca_paper:
        # Belt-and-suspenders — Config validator should have blocked this already.
        raise RuntimeError(
            "live account (alpaca_paper=False) with use_real_data_tools=False "
            "is forbidden. This should have been caught by Config validation."
        )
    logger.info(
        "Using MOCK_TOOL_IMPLS (use_real_data_tools=False, paper=True — dev/CI mode)"
    )
    return MOCK_TOOL_IMPLS


def _fetch_chain_and_metrics(
    tool_impls: dict[str, ToolImpl],
    proposal: TradeProposal,
) -> tuple[FilteredChain | None, StructureMetrics | None]:
    """Fetch the filtered chain for a proposal and compute deterministic metrics.

    Uses the same tool implementation the agent called during exploration —
    AlpacaDataClient caches chain fetches within the cycle, so this re-fetch
    is served from the cycle cache and sees identical data.

    Returns (None, None) on fetch failure and (chain, None) when a leg is
    absent from the chain. Both cases are rejected downstream by
    validate_market_access's fail-closed liquidity check.
    """
    try:
        chain = tool_impls[TOOL_GET_FILTERED_CHAIN]({"symbol": proposal.underlying})
    except Exception as exc:
        logger.warning(
            "chain fetch for %s failed during validation — %s (failing closed)",
            proposal.underlying,
            exc,
        )
        return None, None
    if not isinstance(chain, FilteredChain):
        return None, None
    return chain, compute_structure_metrics(proposal.legs, chain)


def _enrich_proposal(
    proposal: TradeProposal,
    metrics: StructureMetrics | None,
    log_context: str,
) -> TradeProposal:
    """Override the proposal's self-reported risk metrics with computed values."""
    if metrics is None:
        return proposal
    updates = apply_structure_metrics(
        {},
        metrics,
        agent_est_max_loss=proposal.est_max_loss,
        agent_est_max_profit=proposal.est_max_profit,
        log_context=log_context,
    )
    return proposal.model_copy(update=updates)


def _journal_gated(
    engine: Engine,
    cycle_id: str,
    now: datetime,
    config: Config,
    context_snapshot: ContextSnapshot,
) -> None:
    """Write a NO_ACTION_GATED JournalRecord for a short-circuited cycle."""
    decision = Decision(
        proposal=None,
        validation_result=None,
        sizing_result=None,
        action_taken=ActionTaken.NO_ACTION_GATED,
    )
    journal_record = JournalRecord(
        cycle_id=cycle_id,
        timestamp=now,
        action_taken=ActionTaken.NO_ACTION_GATED,
        decision=decision,
        context_snapshot=context_snapshot,
        limits_version=config.limits.limits_version,
        prompt_version=config.playbook.playbook_version,
        model_id=config.model_id,
    )
    with get_connection(engine) as conn:
        write_journal_record(conn, journal_record)


def _dispose_assignment(
    assigned_positions: list[AssignmentEvent],
    *,
    engine: Engine,
    cycle_id: str,
    now: datetime,
    config: Config,
    dispatcher: AlertDispatcher | None,
    set_by: str = "orchestrator",
) -> None:
    """Policy: HALT + CRITICAL on option assignment.

    An equity position from assignment is categorically outside the system's
    model — no equity order path exists and the exposure cannot be sized or
    managed. HALT immediately and emit a CRITICAL alert.

    For the entry cycle the caller returns immediately after this call.
    For the monitor cycle the caller continues managing the options book —
    HALT stops new entries but the monitor must keep evaluating existing
    options exits under HALT (WP-7.1 HALT semantics).

    A NO_ACTION_GATED JournalRecord is written so WP-7 can cross-reference
    the halt event via cycle_id. The equity Position rows themselves are
    written by the WP-1.5 reconcile path and carry the full assignment detail
    (assigned_qty, assignment_price, closed_option_position_id).
    """
    with get_connection(engine) as _halt_conn:
        set_state(
            _halt_conn,
            KillSwitchState.HALT,
            set_by=set_by,
            reason=(
                f"cycle {cycle_id}: {len(assigned_positions)} option assignment(s) "
                "detected; equity position(s) outside system model — human review "
                "required"
            ),
        )
    detail = (
        f"HALT engaged: {len(assigned_positions)} option assignment(s) created "
        "equity position(s) the system cannot model. Manual liquidation required."
    )
    _dispatch(
        dispatcher,
        AlertEvent(
            event_type=AlertEventType.KILL_SWITCH_CHANGE,
            severity=AlertSeverity.CRITICAL,
            detail=detail,
        ),
    )
    _journal_gated(engine, cycle_id, now, config, _gated_context_snapshot(config, now))
    logger.critical(
        "%s ASSIGNMENT_HALT — %d assignment(s); HALT engaged. %s",
        set_by,
        len(assigned_positions),
        detail,
    )


def run_daily_iv_job(
    config: Config,
    *,
    engine: Engine | None = None,
    dispatcher: AlertDispatcher | None = None,
    _today: date | None = None,
) -> None:
    """Record daily ATM IV for every symbol in the universe.

    Called by CycleScheduler once per trading session, at session_close +
    config.daily_iv_capture_offset_minutes. Not gated by the kill switch —
    IV accumulation must continue during HALT/FLATTEN so history stays intact
    for when trading resumes. Silently stopping accumulation during a halt
    would punch a gap in the 252-day window that degrades rank quality for
    weeks afterward.

    For each universe symbol:
    1. Fetch the options chain via AlpacaDataClient.fetch_option_chain().
    2. Extract ATM IV with get_atm_iv() — identical call (same function, same
       default target_dte=30) as the context assembler's live-IV fetch, so the
       stored history and the live current_iv numerator remain commensurable.
    3. Upsert via record_daily_iv() — idempotent: re-running the job on the
       same day updates the existing row, never inserts a duplicate.

    Missing-data policy: a failed symbol fetch writes NO row for that symbol
    on this date. The rank computation sees the gap as an absent observation,
    not a null value — consistent with WP-3.4's "None means no data" invariant.

    Symbol failures are dispatched as SCHEDULER_SKIP WARN alerts so silent IV
    history degradation surfaces before it degrades trading eligibility. A few
    days of gaps can leave symbols None-ineligible without any visible error.

    _today is a test-only override for the current date (same DI pattern as
    _now in run_entry_cycle). Production callers must not pass it.
    """
    if engine is None:
        engine = build_engine(config.db_url)

    today = _today if _today is not None else date.today()
    today_str = today.isoformat()
    calendar = xcals.get_calendar(config.exchange_calendar)

    if not calendar.is_session(today_str):
        logger.info(
            "run_daily_iv_job: %s is not a trading session — skipping",
            today_str,
        )
        return

    symbols = load_universe(config)
    if not symbols:
        logger.warning("run_daily_iv_job: universe is empty — nothing to record")
        return

    data_provider = AlpacaDataClient()
    data_provider.begin_cycle()

    recorded = 0
    failed: list[str] = []

    for symbol in symbols:
        try:
            contracts = data_provider.fetch_option_chain(symbol)
            price = data_provider.fetch_latest_price(symbol)
            atm_iv = get_atm_iv(contracts, price)
            if atm_iv is None:
                logger.warning(
                    "run_daily_iv_job: %s on %s — no ATM IV extractable from chain "
                    "(no row written for this session)",
                    symbol,
                    today_str,
                )
                failed.append(symbol)
                continue
            with get_connection(engine) as conn:
                record_daily_iv(symbol, atm_iv, today, conn)
            recorded += 1
            logger.debug(
                "run_daily_iv_job: %s on %s — recorded atm_iv=%.4f",
                symbol,
                today_str,
                atm_iv,
            )
        except Exception as exc:
            logger.error(
                "run_daily_iv_job: %s on %s — %s",
                symbol,
                today_str,
                exc,
            )
            failed.append(symbol)

    logger.info(
        "run_daily_iv_job: %s — recorded=%d failed=%d%s",
        today_str,
        recorded,
        len(failed),
        f" failed_symbols={failed[:5]}{'...' if len(failed) > 5 else ''}"
        if failed
        else "",
    )

    if failed:
        _dispatch(
            dispatcher,
            AlertEvent(
                event_type=AlertEventType.SCHEDULER_SKIP,
                severity=AlertSeverity.WARN,
                detail=(
                    f"Daily IV capture: {len(failed)}/{len(symbols)} symbols failed "
                    f"on {today_str}: {failed[:5]}"
                    + (" ..." if len(failed) > 5 else "")
                ),
            ),
        )


def run_entry_cycle(
    config: Config,
    *,
    broker: BrokerClient | None = None,
    engine: Engine | None = None,
    dispatcher: AlertDispatcher | None = None,
    _now: datetime | None = None,
) -> CycleResult:
    """Run the WP-8.2 entry cycle pipeline.

    Full 10-step flow replacing the WP-0.5.2 stub body:
    1.  KILL_SWITCH   — bail on HALT/FLATTEN before any broker calls
    2.  RECONCILE     — broker.reconcile() → StateDiff (broker is source of truth)
    3.  STATE_INTEGRITY — act on StateDiff anomalies before gates:
        3a. WORKING OPEN orders → cancel each at cycle-top;
            fill-race → proceed (real position, reconcile handles it);
            cancel-failure → skip entry this cycle (WORKING_CANCEL_FAILED)
        3b. unmatched_local → HALT + CRITICAL (client_order_id gap; can't resolve)
        3c. orphans → WARN alert + skip entry this cycle (ORPHAN_UNRESOLVED)
        3d. assigned_positions → HALT + CRITICAL (equity not modeled; ASSIGNMENT_HALT)
    4.  TEMPORAL GATES — market_is_open → within_blackout_window (cheap; no portfolio)
    5.  ASSEMBLE      — context/assembler.py with stub tool_impls (WP-3 swap: WP-8.5)
    6.  PORTFOLIO GATES — has_buying_power → under_position_cap (uses bundle.portfolio)
    7.  REASON        — agent/reasoner.py; catch ReasonerError → CycleError(REASON).
                        A validation-feedback closure gives the agent one shot at
                        revising a proposal that would fail deterministic checks.
    8.  ENRICH+VALIDATE — recompute est_max_loss/est_max_profit/net Greeks from
                        chain quotes (risk/structure.py), then validate_structural
                        + validate_market_access; rejection journals + alert
    9.  SIZE          — risk/sizing.py; uses bundle.portfolio
    9b. RISK CAPS     — validate_risk_caps (max-loss cap, Greek bands,
                        concentration) with the sized contract count
    10. EXECUTE       — limit price from chain combo mid ± offset;
                        broker.submit / submit_multi_leg; fill alert; JOURNAL

    Optional DI parameters:
        broker:     BrokerClient — built from config when absent. Required for
                    reconcile (step 2), so it is never lazily constructed.
        engine:     SQLAlchemy Engine — built from config.db_url when absent.
        dispatcher: AlertDispatcher — None silently suppresses all alerts.
                    Inject a NullChannel-backed dispatcher in tests.
        _now:       datetime — test-only clock override (same pattern as
                    run_monitor_cycle). Production callers must not pass it.

    Short-circuit invariant:
        short_circuit_reason is not None implies
        action_taken == ActionTaken.NO_ACTION_GATED.
    """
    if engine is None:
        engine = build_engine(config.db_url)

    now = _now if _now is not None else datetime.now(UTC)
    cycle_id = str(uuid.uuid4())
    _calendar = xcals.get_calendar(config.exchange_calendar)
    _prompt_version = config.playbook.playbook_version
    _model_id = config.model_id

    if dispatcher is None:
        logger.debug("cycle %s: no AlertDispatcher — alerts suppressed", cycle_id)
    else:
        logger.debug("cycle %s: alerting active", cycle_id)

    # ── Step 1: KILL_SWITCH ──────────────────────────────────────────────────
    # Fail-safe: if the DB read fails we cannot confirm the switch is NONE,
    # so we fail closed — treat as HALT and block the entry cycle.
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

    # ── Step 2: RECONCILE ────────────────────────────────────────────────────
    if broker is None:
        broker = BrokerClient(config)

    with get_connection(engine) as _rec_conn:
        state_diff = _reconcile(broker, _rec_conn, _clock=now)

    logger.info(
        "cycle %s: reconcile — %d filled, %d orphans, %d unmatched-local, %d assigned",
        cycle_id,
        len(state_diff.newly_filled),
        len(state_diff.orphans),
        len(state_diff.unmatched_local),
        len(state_diff.assigned_positions),
    )
    # FILL source 1-of-2: prior-cycle orders whose fill was confirmed this pass.
    # Same-cycle confirmed fills come from step 10's inner _reconcile (source 2-of-2).
    for _filled_order in state_diff.newly_filled:
        _dispatch(
            dispatcher,
            AlertEvent(
                event_type=AlertEventType.FILL,
                severity=AlertSeverity.INFO,
                order_id=_filled_order.broker_order_id,
                detail=(
                    "Prior-cycle fill confirmed "
                    f"broker_id={_filled_order.broker_order_id}"
                ),
            ),
        )

    # ── Step 3: STATE_INTEGRITY ──────────────────────────────────────────────
    # 3a. WORKING OPEN orders — stale entries from a previous cycle that didn't
    #     fill. Cancel them before placing a new entry to avoid double exposure.
    #     Fill-race: cancel returns FILLED → a real position exists; reconcile
    #     already applied it; proceed to entry (system now has a position but that
    #     is correct). Cancel-failure: can't determine broker state → skip entry.
    with get_connection(engine) as _wc_conn:
        _pending = list_pending_orders(_wc_conn)
    _working_open = [
        o
        for o in _pending
        if o.role == OrderRole.OPEN and o.status == OrderStatus.WORKING
    ]
    if _working_open:
        _cancel_failed = False
        for _wo in _working_open:
            try:
                _cancelled = broker.cancel(_wo)
                if _cancelled.status == OrderStatus.FILLED:
                    logger.info(
                        "cycle %s: working-order cancel race-filled broker_id=%s "
                        "— fill counted by reconcile; proceeding",
                        cycle_id,
                        _wo.broker_order_id,
                    )
                else:
                    logger.info(
                        "cycle %s: cancelled stale WORKING order broker_id=%s",
                        cycle_id,
                        _wo.broker_order_id,
                    )
            except Exception as _exc:
                logger.error(
                    "cycle %s: cancel failed for WORKING order broker_id=%s — %s; "
                    "skipping entry this cycle (WORKING_CANCEL_FAILED)",
                    cycle_id,
                    _wo.broker_order_id,
                    _exc,
                )
                _cancel_failed = True

        if _cancel_failed:
            _ctx = _gated_context_snapshot(config, now)
            _journal_gated(engine, cycle_id, now, config, _ctx)
            return CycleResult(
                cycle_id=cycle_id,
                action_taken=ActionTaken.NO_ACTION_GATED,
                short_circuit_reason=ShortCircuitReason.WORKING_CANCEL_FAILED,
                journal_record_id=cycle_id,
            )

    # 3b. Unmatched-local: PENDING_SUBMIT orders with no broker_order_id.
    #     Without client_order_id on Order (known gap — future WP-0 amendment),
    #     we cannot determine if the submit reached the broker. HALT to avoid
    #     trading on a false picture of positions.
    if state_diff.unmatched_local:
        logger.critical(
            "cycle %s: %d unmatched-local order(s) — HALT (split-brain risk; "
            "client_order_id gap prevents resolution; WP-0 amendment required)",
            cycle_id,
            len(state_diff.unmatched_local),
        )
        with get_connection(engine) as _halt_conn:
            set_state(
                _halt_conn,
                KillSwitchState.HALT,
                set_by="orchestrator.run_entry_cycle",
                reason=(
                    f"cycle {cycle_id}: {len(state_diff.unmatched_local)} "
                    "unmatched-local PENDING_SUBMIT order(s) — possible split-brain"
                ),
            )
        _dispatch(
            dispatcher,
            AlertEvent(
                event_type=AlertEventType.KILL_SWITCH_CHANGE,
                severity=AlertSeverity.CRITICAL,
                detail=(
                    f"HALT: {len(state_diff.unmatched_local)} unmatched-local"
                    " order(s). Broker state unknown. Human review required."
                ),
            ),
        )
        _ctx = _gated_context_snapshot(config, now)
        _journal_gated(engine, cycle_id, now, config, _ctx)
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.NO_ACTION_GATED,
            short_circuit_reason=ShortCircuitReason.STATE_INTEGRITY,
            journal_record_id=cycle_id,
        )

    # 3c. Orphans: broker has orders with no matching local record.
    #     Unknown exposure — we don't know what they are. Do NOT auto-cancel
    #     (may be legitimate closing orders from a prior run). Skip entry.
    if state_diff.orphans:
        logger.warning(
            "cycle %s: %d orphan order(s) at broker — skipping entry this cycle; "
            "do not auto-cancel (may be legitimate close)",
            cycle_id,
            len(state_diff.orphans),
        )
        _dispatch(
            dispatcher,
            AlertEvent(
                event_type=AlertEventType.STATE_INTEGRITY,
                severity=AlertSeverity.WARN,
                detail=(
                    f"{len(state_diff.orphans)} orphan order(s) at broker with no "
                    "local record; entry skipped this cycle."
                ),
            ),
        )
        _ctx = _gated_context_snapshot(config, now)
        _journal_gated(engine, cycle_id, now, config, _ctx)
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.NO_ACTION_GATED,
            short_circuit_reason=ShortCircuitReason.ORPHAN_UNRESOLVED,
            journal_record_id=cycle_id,
        )

    # 3d. Assignments: option assigned into equity. Equity is outside the
    #     system's model. HALT immediately; monitor continues managing options.
    if state_diff.assigned_positions:
        _dispose_assignment(
            state_diff.assigned_positions,
            engine=engine,
            cycle_id=cycle_id,
            now=now,
            config=config,
            dispatcher=dispatcher,
            set_by="orchestrator.run_entry_cycle",
        )
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.NO_ACTION_GATED,
            short_circuit_reason=ShortCircuitReason.ASSIGNMENT_HALT,
            journal_record_id=cycle_id,
        )

    # ── Step 4: TEMPORAL GATES ───────────────────────────────────────────────
    _market_open, _market_reason = market_is_open(now, _calendar)
    if not _market_open:
        logger.info("cycle %s: MARKET_CLOSED — %s", cycle_id, _market_reason)
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.NO_ACTION_GATED,
            short_circuit_reason=ShortCircuitReason.MARKET_CLOSED,
        )

    _in_window, _blackout_reason = within_blackout_window(
        now,
        _calendar,
        config.session_open_blackout_minutes,
        config.session_close_blackout_minutes,
    )
    if not _in_window:
        logger.info("cycle %s: BLACKOUT_WINDOW — %s", cycle_id, _blackout_reason)
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.NO_ACTION_GATED,
            short_circuit_reason=ShortCircuitReason.BLACKOUT_WINDOW,
        )

    # ── Step 5: ASSEMBLE ─────────────────────────────────────────────────────
    _tool_impls = _build_tool_impls(config, engine, broker)
    bundle = assemble_context(
        _tool_impls,
        model_id=_model_id,
        prompt_version=_prompt_version,
        limits_version=config.limits.limits_version,
    )
    context_snapshot = to_context_snapshot(bundle)
    logger.info(
        "cycle %s: assembled context hash=%s symbols=%d excluded=%d",
        cycle_id,
        bundle.context_hash,
        len(bundle.universe.symbol_snapshots),
        len(bundle.excluded),
    )

    # ── Step 6: PORTFOLIO GATES ──────────────────────────────────────────────
    # Portfolio state from the assembled bundle — avoids a redundant broker call.
    _portfolio = bundle.portfolio

    _has_power, _power_reason = has_buying_power(_portfolio, config.limits)
    if not _has_power:
        logger.info("cycle %s: NO_BUYING_POWER — %s", cycle_id, _power_reason)
        _journal_gated(engine, cycle_id, now, config, context_snapshot)
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.NO_ACTION_GATED,
            short_circuit_reason=ShortCircuitReason.NO_BUYING_POWER,
            journal_record_id=cycle_id,
        )

    _under_cap, _cap_reason = under_position_cap(_portfolio, config.limits)
    if not _under_cap:
        logger.info("cycle %s: MAX_POSITIONS — %s", cycle_id, _cap_reason)
        _journal_gated(engine, cycle_id, now, config, context_snapshot)
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.NO_ACTION_GATED,
            short_circuit_reason=ShortCircuitReason.MAX_POSITIONS,
            journal_record_id=cycle_id,
        )

    if not bundle.universe.symbol_snapshots:
        logger.info("cycle %s: EMPTY_ACTION_SPACE — no symbols in universe", cycle_id)
        _journal_gated(engine, cycle_id, now, config, context_snapshot)
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.NO_ACTION_GATED,
            short_circuit_reason=ShortCircuitReason.EMPTY_ACTION_SPACE,
            journal_record_id=cycle_id,
        )

    # ── Step 7: REASON ───────────────────────────────────────────────────────
    def _validation_feedback(p: TradeProposal) -> str | None:
        """Pre-validate a candidate proposal so the agent can revise once.

        Runs the same structural + market-access checks as step 8 (with
        computed metrics overriding the agent's numbers) and returns the
        rejection reasons as text, or None when the proposal would pass.
        This is advisory — step 8 remains the authoritative gate; kill-switch
        state is re-checked there, not here.
        """
        if p.action != "OPEN":
            return None
        fb_chain, fb_metrics = _fetch_chain_and_metrics(_tool_impls, p)
        checked = _enrich_proposal(p, fb_metrics, f"cycle {cycle_id} feedback")
        result = validate_structural(checked, config.limits)
        reasons = list(result.reasons)
        if result.passed:
            reasons = validate_market_access(
                checked,
                config.limits,
                bundle.universe.symbol_snapshots.get(checked.underlying),
                _portfolio,
                KillSwitchState.NONE,
                fb_chain,
                bundle.events.get(checked.underlying),
            )
        if not reasons:
            return None
        return "; ".join(f"[{r.rule_id.value}] {r.human_message}" for r in reasons)

    try:
        proposal = reason(
            context_snapshot,
            _tool_impls,
            playbook=config.playbook,
            limits=config.limits,
            model_id=_model_id,
            max_schema_retries=config.max_schema_retries,
            max_turns=config.max_reasoning_turns,
            validation_feedback=_validation_feedback,
        )
    except ReasonerError as exc:
        logger.error("cycle %s: REASON failed — %s", cycle_id, exc)
        cycle_error = CycleError(
            stage=CycleStage.REASON,
            message=str(exc),
            recoverable=True,
        )
        decision = Decision(
            proposal=None,
            validation_result=None,
            sizing_result=None,
            action_taken=ActionTaken.NO_ACTION_GATED,
        )
        journal_record = JournalRecord(
            cycle_id=cycle_id,
            timestamp=now,
            action_taken=ActionTaken.NO_ACTION_GATED,
            decision=decision,
            context_snapshot=context_snapshot,
            limits_version=config.limits.limits_version,
            prompt_version=_prompt_version,
            model_id=_model_id,
        )
        with get_connection(engine) as conn:
            write_journal_record(conn, journal_record)
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.NO_ACTION_GATED,
            error=cycle_error,
            journal_record_id=cycle_id,
        )

    logger.info(
        "cycle %s: reasoner returned action=%s strategy=%s underlying=%s",
        cycle_id,
        proposal.action,
        proposal.strategy,
        proposal.underlying,
    )

    if proposal.action == "NO_ACTION":
        decision = Decision(
            proposal=proposal,
            validation_result=None,
            sizing_result=None,
            action_taken=ActionTaken.NO_ACTION_AGENT,
        )
        journal_record = JournalRecord(
            cycle_id=cycle_id,
            timestamp=now,
            action_taken=ActionTaken.NO_ACTION_AGENT,
            decision=decision,
            context_snapshot=context_snapshot,
            limits_version=config.limits.limits_version,
            prompt_version=_prompt_version,
            model_id=_model_id,
        )
        with get_connection(engine) as conn:
            write_journal_record(conn, journal_record)
        logger.info("cycle %s: NO_ACTION_AGENT; journal written", cycle_id)
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.NO_ACTION_AGENT,
            proposal=proposal,
            journal_record_id=cycle_id,
        )

    # ── Step 8: ENRICH + VALIDATE ────────────────────────────────────────────
    # Recompute risk metrics deterministically from the chain, then run the
    # full validation stack: structural (playbook, naked-short), then
    # market-access (liquidity, exit-plan bounds, event blackout, buying
    # power, duplicates/conflicts). Risk caps run after sizing in step 9.
    _chain, _metrics = _fetch_chain_and_metrics(_tool_impls, proposal)
    proposal = _enrich_proposal(proposal, _metrics, f"cycle {cycle_id}")

    validation = validate_structural(proposal, config.limits)
    if validation.passed:
        # Re-read the kill switch — the LLM call takes long enough that the
        # step-1 read may be stale. Fail closed (treat as HALT) on read error.
        try:
            with get_connection(engine) as _ks_conn2:
                _ks_state2 = get_current_state(_ks_conn2)
        except Exception:
            logger.critical(
                "cycle %s: kill-switch re-read failed pre-validation — "
                "failing closed (treating as HALT)",
                cycle_id,
            )
            _ks_state2 = KillSwitchState.HALT
        _ma_reasons = validate_market_access(
            proposal,
            config.limits,
            bundle.universe.symbol_snapshots.get(proposal.underlying),
            _portfolio,
            _ks_state2,
            _chain,
            bundle.events.get(proposal.underlying),
        )
        if _ma_reasons:
            validation = ValidationResult(passed=False, reasons=_ma_reasons)

    # Defensive: execution prices off _metrics.leg_quotes. Market access
    # fails closed when the chain or a leg is missing, so this only fires if
    # that invariant is ever broken.
    if validation.passed and (_chain is None or _metrics is None):
        validation = ValidationResult(
            passed=False,
            reasons=[
                RejectionReason(
                    rule_id=ValidationRuleId.LIQUIDITY_SPREAD,
                    severity=Severity.ERROR,
                    human_message=(
                        "chain quotes unavailable for execution pricing; failing closed"
                    ),
                )
            ],
        )

    logger.info(
        "cycle %s: validation %s",
        cycle_id,
        "passed"
        if validation.passed
        else f"REJECTED {[r.rule_id for r in validation.reasons]}",
    )

    if not validation.passed:
        rejection_ids = [r.rule_id for r in validation.reasons]
        _dispatch(
            dispatcher,
            AlertEvent(
                event_type=AlertEventType.REJECTION,
                severity=AlertSeverity.WARN,
                symbol=proposal.underlying,
                detail=(
                    f"Proposal rejected: {rejection_ids}. "
                    f"strategy={proposal.strategy} underlying={proposal.underlying}"
                ),
            ),
        )
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
            context_snapshot=context_snapshot,
            rejection_rule_ids=rejection_ids,
            limits_version=config.limits.limits_version,
            prompt_version=_prompt_version,
            model_id=_model_id,
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

    # ── Step 9: SIZE ─────────────────────────────────────────────────────────
    sizing = _size(proposal, _portfolio, config.limits)
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
            context_snapshot=context_snapshot,
            limits_version=config.limits.limits_version,
            prompt_version=_prompt_version,
            model_id=_model_id,
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

    # ── Step 9b: RISK CAPS ───────────────────────────────────────────────────
    # Post-sizing portfolio-level checks: max-loss cap, Greek bands,
    # concentration. Steps 8's fail-closed guard guarantees _chain is present.
    assert _chain is not None and _metrics is not None  # guarded in step 8
    risk_validation = validate_risk_caps(
        proposal,
        _portfolio,
        config.limits,
        contracts=sizing.contracts,
        underlying_price=_chain.underlying_price,
    )
    if not risk_validation.passed:
        rejection_ids = [r.rule_id for r in risk_validation.reasons]
        logger.info("cycle %s: risk caps REJECTED %s", cycle_id, rejection_ids)
        _dispatch(
            dispatcher,
            AlertEvent(
                event_type=AlertEventType.REJECTION,
                severity=AlertSeverity.WARN,
                symbol=proposal.underlying,
                detail=(
                    f"Proposal rejected by risk caps: {rejection_ids}. "
                    f"strategy={proposal.strategy} underlying={proposal.underlying}"
                ),
            ),
        )
        decision = Decision(
            proposal=proposal,
            validation_result=risk_validation,
            sizing_result=sizing,
            action_taken=ActionTaken.REJECTED,
        )
        journal_record = JournalRecord(
            cycle_id=cycle_id,
            timestamp=now,
            action_taken=ActionTaken.REJECTED,
            decision=decision,
            context_snapshot=context_snapshot,
            rejection_rule_ids=rejection_ids,
            limits_version=config.limits.limits_version,
            prompt_version=_prompt_version,
            model_id=_model_id,
            strategy=proposal.strategy,
            underlying=proposal.underlying,
            conviction=proposal.conviction,
        )
        with get_connection(engine) as conn:
            write_journal_record(conn, journal_record)
        return CycleResult(
            cycle_id=cycle_id,
            action_taken=ActionTaken.REJECTED,
            proposal=proposal,
            validation=risk_validation,
            sizing=sizing,
            journal_record_id=cycle_id,
        )

    # ── Step 10: EXECUTE + JOURNAL ───────────────────────────────────────────
    # Limit price comes from live chain quotes: combo mid (conservative tick
    # rounding) nudged toward fill by order_limit_offset_from_mid. This
    # replaces the former fixed slice_limit_price, which priced every entry
    # at the same net credit regardless of what the structure was worth.
    position_id = str(uuid.uuid4())
    if len(proposal.legs) == 1:
        _bid, _ask = _metrics.leg_quotes[0]
        limit_price = compute_limit_price(
            _bid, _ask, proposal.legs[0].side, config.order_limit_offset_from_mid
        )
        order = broker.submit(proposal, sizing.contracts, limit_price, position_id)
    else:
        limit_price = compute_multi_leg_limit_price(
            proposal.legs,
            _metrics.leg_quotes,
            offset_toward_fill=config.order_limit_offset_from_mid,
        )
        order = broker.submit_multi_leg(
            proposal, sizing.contracts, limit_price, position_id
        )
    logger.info(
        "cycle %s: limit price %.2f (combo mid %.2f, offset %.2f)",
        cycle_id,
        limit_price,
        _metrics.net_entry_mid,
        config.order_limit_offset_from_mid,
    )
    logger.info(
        "cycle %s: order submitted broker_id=%s status=%s",
        cycle_id,
        order.broker_order_id,
        order.status,
    )

    if order.status == OrderStatus.REJECTED:
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
            context_snapshot=context_snapshot,
            limits_version=config.limits.limits_version,
            prompt_version=_prompt_version,
            model_id=_model_id,
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

    # Insert Position then Order, to satisfy FK constraint.
    # order.id is only known after submit returns; it becomes opening_order_id.
    #
    # If the order filled synchronously (within the poll timeout), apply the
    # PENDING_OPEN → OPEN transition and capture the actual fill price here.
    # reconcile() only processes non-terminal (PENDING) orders, so it would
    # never see this already-FILLED order and the position would be stranded.
    _initial_status = (
        PositionStatus.OPEN
        if order.status == OrderStatus.FILLED
        else PositionStatus.PENDING_OPEN
    )
    _entry_net = (
        order.net_fill_price
        if order.status == OrderStatus.FILLED and order.net_fill_price is not None
        else limit_price
    )
    position = Position(
        id=position_id,
        underlying=proposal.underlying,
        strategy=proposal.strategy,
        legs=[
            PositionLeg(
                leg=leg,
                filled_qty=(
                    order.filled_qty * leg.ratio
                    if order.status == OrderStatus.FILLED
                    else 0
                ),
                avg_fill_price=0.0,
                status=LegStatus.OPEN,
            )
            for leg in proposal.legs
        ],
        quantity=sizing.contracts,
        entry_net_amount=_entry_net,
        current_mark=_entry_net,
        marked_at=now,
        unrealized_pnl=0.0,
        realized_pnl=None,
        exit_plan=proposal.exit_plan,
        status=_initial_status,
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
        context_snapshot=context_snapshot,
        position_ids=[position_id],
        order_ids=[order.id],
        limits_version=config.limits.limits_version,
        prompt_version=_prompt_version,
        model_id=_model_id,
        strategy=proposal.strategy,
        underlying=proposal.underlying,
        net_delta_at_open=proposal.net_delta,
        conviction=proposal.conviction,
    )

    _dispatch(
        dispatcher,
        AlertEvent(
            event_type=AlertEventType.ENTRY_SUBMITTED,
            severity=AlertSeverity.INFO,
            symbol=proposal.underlying,
            order_id=order.broker_order_id,
            detail=(
                f"Entry submitted: {proposal.strategy} on {proposal.underlying} "
                f"broker_id={order.broker_order_id}"
            ),
        ),
    )

    with get_connection(engine) as conn:
        insert_position(conn, position)
        insert_order(conn, order)
        # reconcile() detects fills and transitions Position: PENDING_OPEN → OPEN.
        state_diff_post = _reconcile(broker, conn, _clock=now)
        write_journal_record(conn, journal_record)

    # FILL source 2-of-2: same-cycle order confirmed by the inner _reconcile above.
    # Prior-cycle fills are dispatched from step 2's reconcile (source 1-of-2).
    for _filled_order in state_diff_post.newly_filled:
        _dispatch(
            dispatcher,
            AlertEvent(
                event_type=AlertEventType.FILL,
                severity=AlertSeverity.INFO,
                symbol=proposal.underlying,
                order_id=_filled_order.broker_order_id,
                detail=(
                    f"Fill confirmed: {proposal.strategy} on {proposal.underlying} "
                    f"broker_id={_filled_order.broker_order_id}"
                ),
            ),
        )
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
    dispatcher: AlertDispatcher | None = None,
    _now: datetime | None = None,
) -> MonitorResult:
    """Run one monitor cycle: reconcile state, then evaluate exit rules.

    WP-8.3 — adds alert dispatch (EXIT_SUBMITTED, FILL) and assignment handling
    atop the WP-5.5 core implementation.

    Sequence:
    1. Kill-switch check.
    2. Market open pre-flight — no-op (returns empty MonitorResult) when closed.
    3. Reconcile state via broker to refresh position marks and detect fills.
       3a. Assignments: HALT + CRITICAL; monitor continues managing options book.
    4. Read all open positions from DB (fresh after reconcile).
    5. For each position:
       - FLATTEN mode: submit close immediately, bypassing rule evaluation.
       - Normal mode: evaluate stop-loss → profit-target → DTE in order;
         first rule that triggers submits a close and skips the remainder.
       - On trigger: dispatch EXIT_SUBMITTED alert (early signal; order is WORKING).
    6. Finalize: write OutcomeRecords for positions confirmed CLOSED this cycle;
       dispatch FILL alert with realized_pnl for each confirmed close.
    7. Return MonitorResult.

    Alert semantics:
        EXIT_SUBMITTED — fires at close-order-submit time. The order may still be
            WORKING. This is the early operator signal that an exit was triggered.
        FILL — fires in the finalize step when reconcile confirms the position is
            CLOSED and realized_pnl is computed from the actual fill price. Never
            fire two FILLs for the same close; EXIT_SUBMITTED and FILL are distinct
            events at distinct moments.

    Kill-switch semantics:
        NONE    — normal exit evaluation.
        HALT    — monitor runs normally (unmanaged positions under HALT defeats
                  the purpose of a halt); entries are blocked by entry cycle.
        FLATTEN — all open OPTION_STRATEGY positions closed immediately,
                  bypassing stop/target/DTE checks. EQUITY disposition is
                  WP-8's responsibility.

    Assignment handling:
        If reconcile detects option assignments, HALT + CRITICAL alert via
        _dispose_assignment(). Unlike the entry cycle, the monitor does NOT return
        early — it continues evaluating remaining options positions, because HALT
        stops new entries but the monitor must keep managing the existing book
        (WP-7.1 semantics). The entry cycle will short-circuit on HALT next run.

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

    cycle_id = str(uuid.uuid4())

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
        # DEBUG, not INFO: this fires every monitor interval all night and
        # weekend — thousands of no-op lines per week at INFO.
        logger.debug("run_monitor_cycle: SKIP (market closed) — %s", _market_reason)
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

    # ── Step 3a: ASSIGNMENT HANDLING ─────────────────────────────────────────
    # Assignment detected → HALT + CRITICAL, but monitor continues managing
    # the options book. HALT stops new entries (entry cycle short-circuits on it);
    # the monitor must keep evaluating existing positions under HALT (WP-7.1).
    if state_diff.assigned_positions:
        _dispose_assignment(
            state_diff.assigned_positions,
            engine=engine,
            cycle_id=cycle_id,
            now=now,
            config=config,
            dispatcher=dispatcher,
            set_by="orchestrator.run_monitor_cycle",
        )
        # Do NOT return here — monitor continues with remaining options positions.

    # ── Step 3b: REPRICE STALE CLOSE ORDERS ─────────────────────────────────
    # A close limit that missed its market (gap through a stop) would sit
    # WORKING forever while has_pending_close skips the position. Cancel and
    # replace such orders at a fresh mark, widening toward the market each
    # pass. Runs right after reconcile so current_mark is fresh.
    _race_filled: list[Position] = []
    try:
        with get_connection(engine) as _rp_conn:
            _repriced, _race_filled = reprice_stale_close_orders(
                _rp_conn,
                broker,
                now,
                stale_after=timedelta(minutes=config.exit_reprice_after_minutes),
                offset_step=config.exit_reprice_offset_step,
                max_widenings=config.exit_reprice_max_widenings,
            )
        for _new_order in _repriced:
            _dispatch(
                dispatcher,
                AlertEvent(
                    event_type=AlertEventType.EXIT_SUBMITTED,
                    severity=AlertSeverity.WARN,
                    order_id=_new_order.broker_order_id,
                    detail=(
                        f"Stale exit order repriced: {_new_order.exit_reason} "
                        f"position={_new_order.position_id} new limit="
                        f"{_new_order.limit_price} broker_id="
                        f"{_new_order.broker_order_id}"
                    ),
                ),
            )
    except Exception as exc:
        logger.error(
            "run_monitor_cycle: reprice pass failed — %s (continuing; exits "
            "still evaluated this cycle)",
            exc,
        )

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
                    # EXIT_SUBMITTED fires at close-order-submit time. The order
                    # may still be WORKING; FILL fires later (step 6) once the
                    # fill is confirmed and realized_pnl is known.
                    _dispatch(
                        dispatcher,
                        AlertEvent(
                            event_type=AlertEventType.EXIT_SUBMITTED,
                            severity=AlertSeverity.INFO,
                            symbol=pos.underlying,
                            order_id=order.broker_order_id,
                            detail=(
                                f"Exit close submitted: {order.exit_reason} on "
                                f"{pos.underlying} position={pos.id} "
                                f"broker_id={order.broker_order_id}"
                            ),
                        ),
                    )
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
    # These are positions whose closing orders filled during step 3's reconcile,
    # plus any closed by a fill-race during the step-3b reprice pass. We write
    # the OutcomeRecord NOW (not at trigger time) so realized_pnl uses the
    # actual fill price, not a placeholder. The exit_reason is carried on the
    # closing Order written at trigger time. FILL alert fires here — at the
    # moment the position is confirmed CLOSED with known realized_pnl.
    _finalize_closed_positions(
        engine, now, [*state_diff.closed_positions, *_race_filled], dispatcher
    )

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
    dispatcher: AlertDispatcher | None = None,
) -> None:
    """Write OutcomeRecords and dispatch FILL alerts for confirmed closes.

    Called at the end of run_monitor_cycle with StateDiff.closed_positions —
    the list of positions that transitioned PENDING_CLOSE → CLOSED during this
    cycle's reconcile pass.

    Matches each closed position to its filled closing Order (role=CLOSE,
    status=FILLED) to retrieve the actual fill price and exit_reason. The
    realized_pnl is computed from entry_net_amount and the fill price using
    the sign convention: realized_pnl = (-entry_net_amount - fill_price) *
    quantity * 100.

    FILL alert is dispatched here — not at close-order-submit time — because
    this is the moment the position is confirmed CLOSED and realized_pnl is
    known from the actual fill price. EXIT_SUBMITTED fires at submit time;
    FILL fires here. Never two FILLs for the same close.

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
                _dispatch(
                    dispatcher,
                    AlertEvent(
                        event_type=AlertEventType.FILL,
                        severity=AlertSeverity.INFO,
                        symbol=pos.underlying,
                        order_id=closing_order.broker_order_id,
                        detail=(
                            f"Position closed: {closing_order.exit_reason} on "
                            f"{pos.underlying} position={pos.id} "
                            f"realized_pnl={realized_pnl:.2f} "
                            f"fill_price={fill_price:.4f}"
                        ),
                    ),
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
    "run_daily_iv_job",
    "run_entry_cycle",
    "run_monitor_cycle",
]
