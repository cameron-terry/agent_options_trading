# State & Persistence

**Module:** `options_agent/state/`
**Credentials required:** none
**Status:** complete

Durable storage for positions, orders, fill events, and the per-cycle journal. SQLite by default; Postgres behind one env var (`DB_URL`). Setup and migration commands are in the [README](../../README.md#database).

## Sub-modules

| File | Responsibility |
|---|---|
| `db.py` | `build_engine` (incl. `read_only=True` mode), `get_connection` — engine factory + connection context manager |
| `models.py` | SQLAlchemy Core table definitions (positions, orders, fill_events, journal, outcomes) |
| `crud.py` | Position/Order/FillEvent read-write functions |
| `journal.py` | `JournalRecord` and `OutcomeRecord` write/read/query |

## Opening a connection

```python
from options_agent.state.db import build_engine, get_connection, metadata

engine = build_engine("sqlite:///options_agent.db")   # or "sqlite:///:memory:" + metadata.create_all(engine)

with get_connection(engine) as conn:
    ...   # all CRUD and journal functions take a Connection
```

## Invariants

- **JSON-typed columns take native Python objects.** `legs`, `exit_plan`, `equity_legs`, `legs_filled`, and the journal's `decision`/`context_snapshot`/id-list/flag columns use SQLAlchemy's `JSON` type, which serializes on bind. Writers must never hand these columns a pre-`json.dumps`-ed string, or the value gets double-encoded. Migration `009_fix_double_json_encoded_columns.py` repaired historical rows (idempotent — safe against a clean DB).
- **The journal is write-once.** Rows are never UPDATEd in normal operation; the only sanctioned exceptions are documented backfill scripts (see `state/journal.py`'s module docstring).
- **Backend swap needs no call-site changes.** SQLite↔Postgres is selected entirely by the engine URL.

## Read interfaces

- `crud.py`: `get_position` / `list_open_positions` / `insert_position`, `get_order` / `list_pending_orders` / `insert_order` / `patch_order`, fill-event queries. `options_agent/tests/test_crud.py` demonstrates full construction of `Position`/`Order` objects.
- `journal.py`:
  - `query_journal(conn, symbol=..., action_type=..., date_from=..., date_to=...)` — ordered by timestamp ascending; no `limit` parameter, bound the result with dates.
  - `read_journal_record(conn, cycle_id)` — full record round-trip.
  - `query_outcome_records(conn, position_ids=..., since=...)` — `OutcomeRecord` rows, ordered by `recorded_at`; `position_id` is indexed and is the join key back to `Position` → `JournalRecord`.

The journal is written by `run_entry_cycle()` / `run_monitor_cycle()` as a side-effect of every cycle — see [orchestrator.md](orchestrator.md) for how to run a cycle (including offline with a mocked broker) to populate records.

## Inspecting the SQLite file directly

```bash
sqlite3 options_agent.db
.tables
SELECT cycle_id, timestamp, action_taken, strategy, underlying
  FROM journal_records ORDER BY timestamp DESC LIMIT 10;
```
