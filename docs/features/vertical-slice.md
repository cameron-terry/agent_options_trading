# Vertical Slice — run_entry_cycle

**Module:** `options_agent/orchestrator.py`  
**Credentials required:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (paper account)  
**Status:** complete (WP-0.5)

The thin end-to-end proof that the WP-0 contracts connect. A hardcoded `TradeProposal` runs through the real validator, real sizing, real paper broker, real reconcile, and real journal write. This is the integration target that all other WPs are built against.

**`run_monitor_cycle` is not yet implemented** — it raises `NotImplementedError` (WP-8).

## What the slice does

```
stub_reasoner()
    → validate_structural()
    → size()
    → BrokerClient.submit_multi_leg()
    → reconcile()
    → write_journal_record()
    → CycleResult
```

No kill-switch check, no pre-flight gates, no real context assembly. WP-8 replaces the body with the full 9-step flow.

## Running it

> **Prerequisite:** the stub reasoner hardcodes SPY 450P/445P strikes. Alpaca paper rejects legs whose OCC symbols aren't listed — which happens when those strikes are far OTM (SPY at ~$741 means 450P has no open interest and isn't in Alpaca's system). Before running with real credentials, update `_STUB_EXPIRY`, `_STUB_STRIKE_SELL`, and `_STUB_STRIKE_BUY` in [options_agent/agent/stub_reasoner.py](../../options_agent/agent/stub_reasoner.py) to strikes near the current underlying price. **The offline mock section below is the reliable path for interactive testing without touching those values.**

```python
import logging
logging.basicConfig(level=logging.INFO)

import os
os.environ["ALPACA_API_KEY"] = "..."
os.environ["ALPACA_SECRET_KEY"] = "..."

from pathlib import Path
from options_agent.config import Config
from options_agent.orchestrator import run_entry_cycle

config = Config.from_toml(Path("config.toml"))

result = run_entry_cycle(config)

print(result.action_taken)        # OPENED / REJECTED / SIZED_TO_ZERO
print(result.cycle_id)            # UUID — FK into journal_records
print(result.proposal.strategy)   # "bull_put_spread"
print(result.proposal.underlying) # "SPY"
```

The DB file (`options_agent.db` by default) must exist and have migrations applied before running:

```bash
uv run alembic upgrade head
```

## Injecting dependencies for offline testing

The broker and engine can be passed in directly, which lets you run the validation and sizing steps without live credentials:

```python
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import MagicMock
import uuid

from options_agent.config import Config
from options_agent.contracts.state import Order, OrderRole, OrderStatus
from options_agent.orchestrator import run_entry_cycle
from options_agent.state.db import build_engine, metadata

config = Config.from_toml(Path("config.toml"))

# In-memory DB — no file, no migrations
engine = build_engine("sqlite:///:memory:")
metadata.create_all(engine)

# submit_multi_leg must return a real Order so Pydantic can validate
# Position.opening_order_id. Use side_effect (not return_value) so the
# position_id argument is forwarded — the DB has a FK orders.position_id
# → positions.id, and SQLite enforces it.
def _fake_submit(proposal, contracts, limit_price, position_id):
    return Order(
        id=str(uuid.uuid4()),
        broker_order_id="paper-test-order",
        position_id=position_id,
        role=OrderRole.OPEN,
        status=OrderStatus.WORKING,
        broker_status_raw="accepted",
        submitted_at=datetime.now(UTC),
        filled_at=None,
        legs_filled=[],
        net_fill_price=None,
        filled_qty=0,
    )

broker = MagicMock()
broker.get_account.return_value = MagicMock(
    equity="50000", buying_power="25000",
    options_buying_power="25000", options_approved_level=2,
)
broker.submit_multi_leg.side_effect = _fake_submit
broker.list_open_orders.return_value = []   # reconcile iterates this
broker.get_broker_order.return_value = None  # order not on broker → anomaly logged, no crash

result = run_entry_cycle(config, broker=broker, engine=engine)
print(result.action_taken)
```

Because the broker is constructed lazily (only after validation passes), passing `broker=None` with valid credentials will hit the paper API only when the proposal is valid and sized to a non-zero contract count.

## Reading the result back from the journal

```python
from options_agent.state.db import get_connection
from options_agent.state.journal import read_journal_record

with get_connection(engine) as conn:
    record = read_journal_record(conn, result.cycle_id)

print(record.action_taken)
print(record.strategy)
print(record.conviction)
print(record.decision.validation_result.passed)
```

## Hardcoded proposal details

The stub reasoner emits a SPY bull-put-spread with a September quarterly expiry. It raises `RuntimeError` if the hardcoded expiry is within 30 days of today — bump `_STUB_EXPIRY` in `options_agent/agent/stub_reasoner.py` when that fires.

| Field | Value |
|---|---|
| Underlying | SPY |
| Strategy | bull_put_spread |
| Legs | sell 450P / buy 445P (Sep quarterly) |
| Conviction | 0.65 |
| Limit price | −$1.50 (net credit) |
| Exit plan | 50% profit target, 2× stop, 21-DTE time-stop |
