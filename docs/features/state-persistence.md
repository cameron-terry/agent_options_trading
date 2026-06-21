# State & Persistence

**Module:** `options_agent/state/`  
**Credentials required:** none  
**Status:** complete (WP-2)

Durable storage for positions, orders, fill events, and the per-cycle journal. SQLite by default; Postgres behind one env var.

## Sub-modules

| File | Responsibility |
|---|---|
| `db.py` | `build_engine`, `get_connection` — engine factory + connection context manager |
| `models.py` | SQLAlchemy Core table definitions (positions, orders, fill_events, journal, outcomes) |
| `crud.py` | Position/Order/FillEvent read-write functions |
| `journal.py` | `JournalRecord` and `OutcomeRecord` write/read/query |

## Setup

Run Alembic migrations once before first use:

```bash
uv run alembic upgrade head          # SQLite (creates options_agent.db)

# Postgres:
docker compose up -d
DB_URL=postgresql://postgres:postgres@localhost/options_agent_test \
  uv run alembic upgrade head
```

## Opening a connection

```python
from options_agent.state.db import build_engine, get_connection

engine = build_engine("sqlite:///options_agent.db")

with get_connection(engine) as conn:
    # all CRUD functions take a Connection
    ...
```

For a throwaway in-memory session (no file, no migrations needed):

```python
from options_agent.state.db import build_engine, metadata, get_connection

engine = build_engine("sqlite:///:memory:")
metadata.create_all(engine)      # creates schema in memory

with get_connection(engine) as conn:
    ...
```

## CRUD — positions and orders

The examples below build a `Position` from the stub proposal and an `Order` from scratch, then exercise the CRUD layer against an in-memory DB.

```python
import uuid
from datetime import datetime, UTC
from options_agent.agent.stub_reasoner import stub_reasoner
from options_agent.contracts.state import (
    AssetClass,
    LegStatus,
    OrderRole,
    OrderStatus,
    Position,
    PositionLeg,
    PositionStatus,
)
from options_agent.contracts.state import Order
from options_agent.state.db import build_engine, metadata, get_connection
from options_agent.state.crud import (
    insert_position,
    get_position,
    list_open_positions,
    insert_order,
    get_order,
    list_pending_orders,
    patch_order,
)

engine = build_engine("sqlite:///:memory:")
metadata.create_all(engine)

proposal = stub_reasoner()
position_id = str(uuid.uuid4())
order_id = str(uuid.uuid4())
now = datetime.now(UTC)

position = Position(
    id=position_id,
    underlying=proposal.underlying,
    strategy=proposal.strategy,
    legs=[
        PositionLeg(leg=leg, filled_qty=0, avg_fill_price=0.0, status=LegStatus.OPEN)
        for leg in proposal.legs
    ],
    quantity=1,
    entry_net_amount=-1.50,
    current_mark=-1.50,
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
    opening_order_id=order_id,
    asset_class=AssetClass.OPTION_STRATEGY,
    equity_legs=[],
    assigned_from_position_id=None,
)

order = Order(
    id=order_id,
    broker_order_id="paper-broker-id",
    position_id=position_id,
    role=OrderRole.OPEN,
    status=OrderStatus.WORKING,
    broker_status_raw="accepted",
    submitted_at=now,
    filled_at=None,
    legs_filled=[],
    net_fill_price=None,
    filled_qty=0,
)

with get_connection(engine) as conn:
    insert_position(conn, position)
    insert_order(conn, order)

    pos = get_position(conn, position_id)
    print(pos.underlying, pos.strategy, pos.status)

    open_positions = list_open_positions(conn)
    print(len(open_positions))   # 1 — includes PENDING_OPEN, OPEN, PENDING_CLOSE
    
    pending_orders = list_pending_orders(conn)
    print(len(pending_orders))   # 1

with get_connection(engine) as conn:
    patch_order(conn, order_id, status=OrderStatus.FILLED, filled_qty=1)
    updated = get_order(conn, order_id)
    print(updated.status, updated.filled_qty)   # FILLED 1
```

## Journal

The journal is written by `run_entry_cycle()` as a side-effect of every cycle. The CRUD examples above don't write journal records — use the offline vertical slice to populate one first:

```python
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import MagicMock
import uuid

from options_agent.config import Config
from options_agent.contracts.state import ActionTaken, Order, OrderRole, OrderStatus
from options_agent.orchestrator import run_entry_cycle
from options_agent.state.db import build_engine, metadata, get_connection
from options_agent.state.journal import read_journal_record, query_journal

config = Config.from_toml(Path("config.toml"))
engine = build_engine("sqlite:///:memory:")
metadata.create_all(engine)

def _fake_submit(proposal, contracts, limit_price, position_id):
    return Order(
        id=str(uuid.uuid4()), broker_order_id="paper-test-order",
        position_id=position_id, role=OrderRole.OPEN, status=OrderStatus.WORKING,
        broker_status_raw="accepted", submitted_at=datetime.now(UTC),
        filled_at=None, legs_filled=[], net_fill_price=None, filled_qty=0,
    )

broker = MagicMock()
broker.get_account.return_value = MagicMock(
    equity="50000", buying_power="25000",
    options_buying_power="25000", options_approved_level=2,
)
broker.submit_multi_leg.side_effect = _fake_submit
broker.list_open_orders.return_value = []
broker.get_broker_order.return_value = None

result = run_entry_cycle(config, broker=broker, engine=engine)

with get_connection(engine) as conn:
    # All records today, ordered by timestamp ascending
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    records = query_journal(conn, date_from=today)
    for r in records:
        print(r.cycle_id, r.timestamp, r.action_taken, r.strategy)

    # Filter by action type or ticker
    opened = query_journal(conn, action_type=ActionTaken.OPENED)
    spy_only = query_journal(conn, symbol="SPY")

    # Read back the full record by cycle_id
    record = read_journal_record(conn, result.cycle_id)
    print(record.conviction)
    print(record.decision.validation_result.passed)
```

`query_journal` filters: `symbol` (ticker), `action_type` (`ActionTaken` enum), `date_from` / `date_to` (datetime bounds). Results are ordered by timestamp ascending. No `limit` parameter — use `date_from`/`date_to` to bound the result set.

For a live DB populated by real cycles, swap `build_engine("sqlite:///:memory:")` for `build_engine("sqlite:///options_agent.db")`.

## Outcome records (WP-2.5 interface extension)

`query_outcome_records` is the WP-2 read interface for `OutcomeRecord` rows — added retroactively in WP-7.3 (PR #69) and owned by `state/journal.py` going forward:

```python
from options_agent.state.journal import query_outcome_records

with get_connection(engine) as conn:
    # All outcomes for specific positions
    outcomes = query_outcome_records(conn, position_ids=["pos-id-1", "pos-id-2"])

    # All outcomes since a given date
    from datetime import datetime, UTC
    since = datetime(2025, 1, 1, tzinfo=UTC)
    recent = query_outcome_records(conn, since=since)

    # All outcomes (no filter)
    all_outcomes = query_outcome_records(conn)
```

Results are ordered by `recorded_at` ascending. The `position_id` column is indexed; filtering by `position_ids` is efficient. WP-7 P&L attribution joins `OutcomeRecord.position_id → Position → JournalRecord.cycle_id` to correlate outcomes with entry decisions.

## Inspecting the SQLite file directly

```bash
sqlite3 options_agent.db

.tables
SELECT cycle_id, timestamp, action_taken, strategy, underlying
  FROM journal_records
 ORDER BY timestamp DESC
 LIMIT 10;
```
