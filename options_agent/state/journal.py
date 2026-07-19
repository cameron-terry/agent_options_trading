"""Journal writer and reader (WP-2.3).

Owns all append-only writes: JournalRecord (one per entry cycle) and
OutcomeRecord (one per terminal position event). No update path is
exposed — these tables are write-once by design.

Immutability is enforced at two layers:
  1. Pydantic: JournalRecord and OutcomeRecord are frozen=True models.
  2. This module: no patch/update function is defined for these tables.

Query API deliberately omits position_id filtering: the JSON position_ids
column on journal_records is unindexed on SQLite. WP-7 attribution should
join via OutcomeRecord.position_id → Position → JournalRecord.cycle_id,
which is the primary key and is always indexed.

Retroactive WP-2.5: query_outcome_records() was added by WP-7.3 (PR #69)
without a WP-2 card. It is a WP-2 interface extension — state/journal.py
owns it going forward; any future signature changes require a WP-2 card.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from options_agent.contracts.journal import (
    JournalRecord,
    OutcomeRecord,
    StrategyOutcomeStats,
    SymbolOutcomeStats,
)
from options_agent.contracts.state import ActionTaken
from options_agent.state.db import (
    journal_records_table,
    outcome_records_table,
    positions_table,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional upstream dependencies — present in WP-3/WP-6 environments.
# Imported conditionally so this module has no hard numpy/pandas dependency.
# ---------------------------------------------------------------------------
try:
    import numpy as _np

    _NUMPY = True
except ImportError:
    _np = None  # type: ignore[assignment]
    _NUMPY = False

try:
    import pandas as _pd

    _PANDAS = True
except ImportError:
    _pd = None  # type: ignore[assignment]
    _PANDAS = False


# ---------------------------------------------------------------------------
# JSON coercion for assembled_context
# ---------------------------------------------------------------------------


def _coerce_for_json(value: Any) -> Any:
    """Recursively coerce a value to a JSON-serializable Python type.

    Handles the known upstream type leaks from WP-3 data pipelines:
    numpy scalars/arrays, Decimal, pandas Timestamp, and Python
    datetime/date objects. Emits a WARNING on every coercion so the
    upstream assembler (WP-6) knows to fix the type leak at its source.

    Raises TypeError for any type not in the known-coercible set.
    Stringifying unknown objects would silently corrupt the immutable
    audit record — fail loudly instead.
    """
    # Fast path: already JSON-native (bool before int — bool subclasses int)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    # Recursive containers
    if isinstance(value, dict):
        return {k: _coerce_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_for_json(v) for v in value]

    # datetime before date — datetime is a subclass of date
    if isinstance(value, datetime):
        _log.warning(
            "assembled_context: coercing datetime → isoformat; fix in WP-6 assembler"
        )
        return value.isoformat()
    if isinstance(value, date):
        _log.warning(
            "assembled_context: coercing date → isoformat; fix in WP-6 assembler"
        )
        return value.isoformat()

    if isinstance(value, Decimal):
        _log.warning(
            "assembled_context: coercing Decimal → float; fix in WP-6 assembler"
        )
        return float(value)

    if _NUMPY:
        import numpy as np

        if isinstance(value, np.floating):
            _log.warning(
                "assembled_context: coercing %s → float; fix in WP-6 assembler",
                type(value).__name__,
            )
            return float(value)
        if isinstance(value, np.integer):
            _log.warning(
                "assembled_context: coercing %s → int; fix in WP-6 assembler",
                type(value).__name__,
            )
            return int(value)
        if isinstance(value, np.ndarray):
            _log.warning(
                "assembled_context: coercing ndarray → list; fix in WP-6 assembler"
            )
            return [_coerce_for_json(v) for v in value.tolist()]

    if _PANDAS:
        import pandas as pd

        if isinstance(value, pd.Timestamp):
            _log.warning(
                "assembled_context: coercing pd.Timestamp → isoformat;"
                " fix in WP-6 assembler"
            )
            return value.isoformat()

    raise TypeError(
        f"assembled_context contains non-JSON-serializable type {type(value)!r}. "
        "This is a bug in the context assembler (WP-6) — fix the type at the source."
    )


# ---------------------------------------------------------------------------
# Shared datetime helper (same convention as crud.py)
# ---------------------------------------------------------------------------


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Re-attach UTC to a naive datetime returned by SQLite's DateTime column."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# JournalRecord serialization
# ---------------------------------------------------------------------------


def _journal_record_to_row(record: JournalRecord) -> dict[str, Any]:
    # Coerce assembled_context on a frozen-copy BEFORE model_dump(mode="json").
    # Pydantic v2's JSON serializer raises PydanticSerializationError on
    # numpy/Decimal types in dict[str, Any] — coercion must happen before the
    # serializer sees the field, not after.
    coerced_snapshot = record.context_snapshot.model_copy(
        update={
            "assembled_context": _coerce_for_json(
                record.context_snapshot.assembled_context
            )
        }
    )
    context_snapshot_dict = coerced_snapshot.model_dump(mode="json")

    return {
        "cycle_id": record.cycle_id,
        "timestamp": record.timestamp,
        "action_taken": record.action_taken.value,
        "decision": json.dumps(record.decision.model_dump(mode="json")),
        "context_snapshot": json.dumps(context_snapshot_dict),
        "position_ids": json.dumps(record.position_ids),
        "order_ids": json.dumps(record.order_ids),
        "strategy": record.strategy,
        "underlying": record.underlying,
        "net_delta_at_open": record.net_delta_at_open,
        "earnings_within_dte": record.earnings_within_dte,
        "conviction": record.conviction,
        "iv_rank_at_open": record.iv_rank_at_open,
        "limits_version": record.limits_version,
        "prompt_version": record.prompt_version,
        "model_id": record.model_id,
        "rejection_rule_ids": json.dumps([r.value for r in record.rejection_rule_ids]),
        "data_quality_flags": json.dumps(record.data_quality_flags),
    }


def _row_to_journal_record(row: Any) -> JournalRecord:
    d = dict(row._mapping)

    for blob_col in (
        "decision",
        "context_snapshot",
        "position_ids",
        "order_ids",
        "rejection_rule_ids",
    ):
        raw = d[blob_col]
        d[blob_col] = json.loads(raw) if isinstance(raw, str) else raw

    # Nullable column: rows written before this flag existed (or never flagged)
    # store NULL, not "[]" — coerce to the model's empty-list default.
    raw_flags = d["data_quality_flags"]
    d["data_quality_flags"] = (
        json.loads(raw_flags) if isinstance(raw_flags, str) else []
    )

    d["timestamp"] = _ensure_utc(d["timestamp"])

    return JournalRecord.model_validate(d)


# ---------------------------------------------------------------------------
# OutcomeRecord serialization
# ---------------------------------------------------------------------------


def _outcome_record_to_row(record: OutcomeRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "position_id": record.position_id,
        "event_type": record.event_type.value,
        "recorded_at": record.recorded_at,
        "contracts_closed": record.contracts_closed,
        "realized_pnl": record.realized_pnl,
        "fill_price": record.fill_price,
        "closing_order_id": record.closing_order_id,
        "exit_reason": record.exit_reason.value
        if record.exit_reason is not None
        else None,
    }


def _row_to_outcome_record(row: Any) -> OutcomeRecord:
    d = dict(row._mapping)
    d["recorded_at"] = _ensure_utc(d["recorded_at"])
    return OutcomeRecord.model_validate(d)


# ---------------------------------------------------------------------------
# Write API — append-only; no update functions are defined for these tables
# ---------------------------------------------------------------------------


def write_journal_record(conn: Connection, record: JournalRecord) -> None:
    """Insert a JournalRecord. Raises IntegrityError if cycle_id already exists."""
    conn.execute(
        journal_records_table.insert().values(**_journal_record_to_row(record))
    )


def write_outcome_record(conn: Connection, record: OutcomeRecord) -> None:
    """Insert an OutcomeRecord. Raises IntegrityError if id already exists.

    Does not require an associated JournalRecord: monitor-driven closes
    (WP-5) produce OutcomeRecords with no entry-cycle. Link by position_id
    only — never look up or require a cycle_id on this write path.
    """
    conn.execute(
        outcome_records_table.insert().values(**_outcome_record_to_row(record))
    )


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


def read_journal_record(conn: Connection, cycle_id: str) -> JournalRecord | None:
    """Return the JournalRecord for the given cycle_id, or None if not found."""
    row = conn.execute(
        sa.select(journal_records_table).where(
            journal_records_table.c.cycle_id == cycle_id
        )
    ).first()
    return _row_to_journal_record(row) if row is not None else None


def query_outcome_records(
    conn: Connection,
    *,
    position_ids: Sequence[str] | None = None,
    since: datetime | None = None,
) -> list[OutcomeRecord]:
    """Query outcome records, ordered by recorded_at ascending.

    Args:
        position_ids: If provided, return only outcomes for these positions.
                      Pass None to return all outcome records.
        since:        If provided, return only outcomes recorded at or after
                      this timestamp.
    """
    stmt = sa.select(outcome_records_table).order_by(
        outcome_records_table.c.recorded_at
    )
    if position_ids is not None:
        stmt = stmt.where(outcome_records_table.c.position_id.in_(position_ids))
    if since is not None:
        stmt = stmt.where(outcome_records_table.c.recorded_at >= since)
    rows = conn.execute(stmt).fetchall()
    return [_row_to_outcome_record(r) for r in rows]


def query_outcome_stats_by_symbol(conn: Connection) -> dict[str, SymbolOutcomeStats]:
    """Aggregate realized outcomes per underlying for context pre-loading.

    Joins outcome_records to positions (position_id → underlying, strategy)
    and reduces to per-symbol win/loss counts, realized P&L totals, a
    per-strategy breakdown, and the most recent exit reasons (newest first,
    capped at 5). Symbols with no outcomes are simply absent.

    Volume note: this scans all outcome records. At this system's trade
    cadence (a handful of positions per week) that stays trivially small for
    the life of the paper run; add a since-filter if it ever grows.
    """
    rows = conn.execute(
        sa.select(
            positions_table.c.underlying,
            positions_table.c.strategy,
            outcome_records_table.c.realized_pnl,
            outcome_records_table.c.exit_reason,
            outcome_records_table.c.recorded_at,
        )
        .select_from(
            outcome_records_table.join(
                positions_table,
                outcome_records_table.c.position_id == positions_table.c.id,
            )
        )
        .order_by(outcome_records_table.c.recorded_at)
    ).fetchall()

    per_symbol: dict[str, list[Any]] = {}
    for row in rows:
        per_symbol.setdefault(row.underlying, []).append(row)

    stats: dict[str, SymbolOutcomeStats] = {}
    for symbol, symbol_rows in per_symbol.items():
        wins = sum(1 for r in symbol_rows if r.realized_pnl > 0)
        total = sum(r.realized_pnl for r in symbol_rows)
        count = len(symbol_rows)

        by_strategy: dict[str, StrategyOutcomeStats] = {}
        for r in symbol_rows:
            existing = by_strategy.get(r.strategy)
            if existing is None:
                by_strategy[r.strategy] = StrategyOutcomeStats(
                    closed_positions=1,
                    wins=1 if r.realized_pnl > 0 else 0,
                    total_realized_pnl=round(r.realized_pnl, 2),
                )
            else:
                by_strategy[r.strategy] = StrategyOutcomeStats(
                    closed_positions=existing.closed_positions + 1,
                    wins=existing.wins + (1 if r.realized_pnl > 0 else 0),
                    total_realized_pnl=round(
                        existing.total_realized_pnl + r.realized_pnl, 2
                    ),
                )

        stats[symbol] = SymbolOutcomeStats(
            symbol=symbol,
            closed_positions=count,
            wins=wins,
            losses=count - wins,
            win_rate=round(wins / count, 3) if count else None,
            total_realized_pnl=round(total, 2),
            avg_realized_pnl=round(total / count, 2) if count else None,
            by_strategy=by_strategy,
            recent_exit_reasons=[
                str(r.exit_reason)
                for r in reversed(symbol_rows)
                if r.exit_reason is not None
            ][:5],
        )

    return stats


def read_outcome_record(conn: Connection, outcome_id: str) -> OutcomeRecord | None:
    """Return the OutcomeRecord with the given id, or None if not found."""
    row = conn.execute(
        sa.select(outcome_records_table).where(outcome_records_table.c.id == outcome_id)
    ).first()
    return _row_to_outcome_record(row) if row is not None else None


# ---------------------------------------------------------------------------
# Query API — indexed filters only (see module docstring for position_id note)
# ---------------------------------------------------------------------------


def query_journal(
    conn: Connection,
    *,
    symbol: str | None = None,
    action_type: ActionTaken | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[JournalRecord]:
    """Query journal records using indexed columns, ordered by timestamp ascending.

    All filters are optional and combinable. Filtering by position_id is
    intentionally absent: the JSON position_ids array column is unindexed
    on SQLite. WP-7 should reach the opening JournalRecord via cycle_id
    (primary key) using the join: OutcomeRecord → Position → cycle_id.

    Args:
        symbol:      Filter by underlying ticker (ix_journal_records_underlying).
        action_type: Filter by ActionTaken value (ix_journal_records_action_taken).
        date_from:   Inclusive lower bound on timestamp (ix_journal_records_timestamp).
        date_to:     Inclusive upper bound on timestamp (ix_journal_records_timestamp).
    """
    stmt = sa.select(journal_records_table).order_by(journal_records_table.c.timestamp)
    if symbol is not None:
        stmt = stmt.where(journal_records_table.c.underlying == symbol)
    if action_type is not None:
        stmt = stmt.where(journal_records_table.c.action_taken == action_type.value)
    if date_from is not None:
        stmt = stmt.where(journal_records_table.c.timestamp >= date_from)
    if date_to is not None:
        stmt = stmt.where(journal_records_table.c.timestamp <= date_to)

    rows = conn.execute(stmt).fetchall()
    return [_row_to_journal_record(r) for r in rows]
