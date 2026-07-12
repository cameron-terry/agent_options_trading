"""Decision explorer API — WP-9.3.

Renders JournalRecord history: a filtered, slim cycle list and a full-trace
detail view per cycle_id. Purely a renderer over data the journal already
stores (decision, context_snapshot, tool_calls_transcript) plus the
position/order/outcome join WP-7 uses — no new computation, no broker or
market-data call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy.engine import Connection

from options_agent.contracts.journal import OutcomeRecord
from options_agent.contracts.proposal import TradeProposal
from options_agent.contracts.results import (
    SizingResult,
    ValidationResult,
    ValidationRuleId,
)
from options_agent.contracts.state import ActionTaken, Order, Position, ToolCallRecord
from options_agent.state.crud import get_order, get_position
from options_agent.state.journal import (
    query_journal,
    query_outcome_records,
    read_journal_record,
)

# Applied to GET /api/cycles when the caller passes no date_from — keeps the
# list payload bounded as the journal grows without adding LIMIT/OFFSET
# support to state/journal.py's query API. Passing date_from explicitly
# overrides this default.
_DEFAULT_LOOKBACK_DAYS = 30


class CycleListItem(BaseModel):
    """One row in the cycle list — slim projection, not the full record.

    The full JournalRecord carries the entire assembled_context blob and
    tool-call transcript inline; embedding that in every list row would bloat
    the payload for no reason the list view needs. Full detail lives behind
    GET /api/cycles/{cycle_id}.
    """

    cycle_id: str
    timestamp: datetime
    action_taken: ActionTaken
    underlying: str | None
    strategy: str | None
    conviction: float | None


class PositionLink(BaseModel):
    """A JournalRecord.position_ids entry, resolved (or not).

    anomaly=True when the id doesn't resolve to a stored Position — surfaced
    rather than hidden, so the explorer shows broken history faithfully (same
    precedent as agent/tools.py's PositionHistory: a missing opening record
    is a system anomaly to report, not paper over).
    """

    id: str
    position: Position | None
    outcomes: list[OutcomeRecord]
    anomaly: bool


class OrderLink(BaseModel):
    """A JournalRecord.order_ids entry, resolved (or not). See PositionLink."""

    id: str
    order: Order | None
    anomaly: bool


class CycleDetail(BaseModel):
    """Full trace for one cycle — GET /api/cycles/{cycle_id}."""

    cycle_id: str
    timestamp: datetime
    action_taken: ActionTaken
    underlying: str | None
    strategy: str | None
    conviction: float | None

    model_id: str
    prompt_version: str
    limits_version: str
    context_hash: str

    proposal: TradeProposal | None
    tool_calls_transcript: list[ToolCallRecord]
    validation_result: ValidationResult | None
    rejection_rule_ids: list[ValidationRuleId]
    sizing_result: SizingResult | None

    positions: list[PositionLink]
    orders: list[OrderLink]


def get_cycles(
    conn: Connection,
    *,
    symbol: str | None = None,
    action_type: ActionTaken | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    now: datetime | None = None,
) -> list[CycleListItem]:
    """Filtered cycle list, newest first — GET /api/cycles.

    Filters map 1:1 to query_journal's params. date_from defaults to
    _DEFAULT_LOOKBACK_DAYS ago when the caller doesn't supply one.
    """
    now = now or datetime.now(UTC)
    if date_from is None:
        date_from = now - timedelta(days=_DEFAULT_LOOKBACK_DAYS)

    records = query_journal(
        conn,
        symbol=symbol,
        action_type=action_type,
        date_from=date_from,
        date_to=date_to,
    )
    return [
        CycleListItem(
            cycle_id=r.cycle_id,
            timestamp=r.timestamp,
            action_taken=r.action_taken,
            underlying=r.underlying,
            strategy=r.strategy,
            conviction=r.conviction,
        )
        for r in reversed(records)
    ]


def _position_link(conn: Connection, position_id: str) -> PositionLink:
    pos = get_position(conn, position_id)
    if pos is None:
        return PositionLink(id=position_id, position=None, outcomes=[], anomaly=True)
    outcomes = query_outcome_records(conn, position_ids=[position_id])
    return PositionLink(id=position_id, position=pos, outcomes=outcomes, anomaly=False)


def _order_link(conn: Connection, order_id: str) -> OrderLink:
    order = get_order(conn, order_id)
    if order is None:
        return OrderLink(id=order_id, order=None, anomaly=True)
    return OrderLink(id=order_id, order=order, anomaly=False)


def get_cycle_detail(conn: Connection, cycle_id: str) -> CycleDetail | None:
    """Full trace for one cycle — GET /api/cycles/{cycle_id}. None if not found."""
    record = read_journal_record(conn, cycle_id)
    if record is None:
        return None

    decision = record.decision
    return CycleDetail(
        cycle_id=record.cycle_id,
        timestamp=record.timestamp,
        action_taken=record.action_taken,
        underlying=record.underlying,
        strategy=record.strategy,
        conviction=record.conviction,
        model_id=record.model_id,
        prompt_version=record.prompt_version,
        limits_version=record.limits_version,
        context_hash=record.context_snapshot.context_hash,
        proposal=decision.proposal,
        tool_calls_transcript=record.context_snapshot.tool_calls_transcript,
        validation_result=decision.validation_result,
        rejection_rule_ids=record.rejection_rule_ids,
        sizing_result=decision.sizing_result,
        positions=[_position_link(conn, pid) for pid in record.position_ids],
        orders=[_order_link(conn, oid) for oid in record.order_ids],
    )
