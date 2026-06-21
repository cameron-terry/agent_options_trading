# Entry Cycle — run_entry_cycle

**Module:** `options_agent/orchestrator.py`  
**Credentials required:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (paper account)  
**Status:** complete (WP-8.2)

The full production entry pipeline. A real LLM call (via `reason()`) proposes a trade, which runs through the real validator, real sizing, real paper broker, real reconcile, and real journal write. `run_monitor_cycle` (WP-5.5) is also implemented — see [monitor.md](monitor.md).

## 10-step pipeline

```
1.  KILL_SWITCH       — bail on HALT/FLATTEN before any broker calls
2.  RECONCILE         — broker → StateDiff (fills, orphans, assignments)
3.  STATE_INTEGRITY   — act on StateDiff anomalies before gates:
      3a. WORKING OPEN orders → cancel; fill-race=proceed, cancel-fail=skip entry
      3b. unmatched_local     → HALT + CRITICAL (split-brain; WP-0 gap)
      3c. orphans             → WARN alert + skip entry this cycle
      3d. assigned_positions  → HALT + CRITICAL (equity not modeled)
4.  TEMPORAL GATES    — market_is_open → within_blackout_window
5.  ASSEMBLE          — context/assembler.py with MOCK_TOOL_IMPLS (WP-8.5: real WP-3)
6.  PORTFOLIO GATES   — has_buying_power → under_position_cap
7.  REASON            — agent/reasoner.py LLM call; ReasonerError → CycleError
8.  VALIDATE          — risk/validator.py; REJECTION alert
9.  SIZE              — risk/sizing.py; uses bundle.portfolio
10. EXECUTE + JOURNAL — broker.submit_multi_leg(); fill alert; write_journal_record()
```

## Short-circuit reasons

| `ShortCircuitReason` | Trigger |
|---|---|
| `KILL_SWITCH_HALT` | DB read failed or state=HALT |
| `KILL_SWITCH_FLATTEN` | state=FLATTEN |
| `WORKING_CANCEL_FAILED` | Stale entry order cancel raised exception |
| `STATE_INTEGRITY` | unmatched-local PENDING_SUBMIT orders (split-brain) |
| `ORPHAN_UNRESOLVED` | Broker has open orders with no local record |
| `ASSIGNMENT_HALT` | Option assignment detected; equity not modeled |
| `MARKET_CLOSED` | Exchange not open |
| `BLACKOUT_WINDOW` | Within open/close blackout windows |
| `NO_BUYING_POWER` | options_buying_power below configured floor |
| `MAX_POSITIONS` | Open position count ≥ max_open_positions |
| `EMPTY_ACTION_SPACE` | Universe has no symbols after assembly |

## DI parameters

```python
def run_entry_cycle(
    config: Config,
    *,
    broker: BrokerClient | None = None,    # built from config if absent
    engine: Engine | None = None,          # built from config.db_url if absent
    dispatcher: AlertDispatcher | None = None,  # None → alerts suppressed
    _now: datetime | None = None,          # test-only clock override
) -> CycleResult
```

**dispatcher**: inject an `AlertDispatcher` backed by `NullChannel` in tests to assert on `AlertEvent` production without coupling to real async delivery.

**_now**: production callers must not pass this. Inject in tests to control market-hours gate without mocking the calendar.

## Alert events emitted

| Stage | Event type | Severity |
|---|---|---|
| Unmatched-local (HALT) | `KILL_SWITCH_CHANGE` | CRITICAL |
| Assignment (HALT) | `KILL_SWITCH_CHANGE` | CRITICAL |
| Orphan (skip entry) | `STATE_INTEGRITY` | WARN |
| Validation rejection | `REJECTION` | WARN |
| Successful execute | `FILL` | INFO |

## WP-3 tool implementations (WP-8.5 pending)

`_build_tool_impls(config)` returns `MOCK_TOOL_IMPLS` when `config.alpaca_paper=True`. The mock map (`agent/tools_mock.py`) has realistic data: SPY tradeable, AAPL in earnings blackout, NVDA warming up. Real WP-3 implementations (live market data) are wired in WP-8.5.

## Running it

```python
import logging
logging.basicConfig(level=logging.INFO)

import os
os.environ["ALPACA_API_KEY"] = "..."
os.environ["ALPACA_SECRET_KEY"] = "..."

from datetime import datetime, UTC
from pathlib import Path
from options_agent.config import Config
from options_agent.orchestrator import run_entry_cycle

config = Config.from_toml(Path("config.toml"))
result = run_entry_cycle(config, _now=datetime.now(UTC))

print(result.action_taken)        # OPENED / REJECTED / NO_ACTION_GATED / …
print(result.cycle_id)            # UUID — FK into journal_records
print(result.short_circuit_reason)  # None when the full flow ran
```

The DB must have migrations applied:

```bash
uv run alembic upgrade head
```

## Offline testing (no credentials)

```python
from datetime import datetime, UTC
from unittest.mock import MagicMock, patch
import uuid

from options_agent.agent.stub_reasoner import stub_reasoner
from options_agent.config import Config
from options_agent.contracts.state import Order, OrderRole, OrderStatus
from options_agent.orchestrator import run_entry_cycle
from options_agent.state.db import build_engine, metadata

config = Config()
engine = build_engine("sqlite:///:memory:")
metadata.create_all(engine)

broker = MagicMock()
broker.list_open_orders.return_value = []
broker.get_all_positions.return_value = []
broker.get_account_activities.return_value = []

def _fake_submit(proposal, contracts, limit_price, position_id, **kw):
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

broker.submit_multi_leg.side_effect = _fake_submit
broker.get_broker_order.return_value = None

# NYSE open, outside blackout: Tuesday 2026-06-16 10:30 AM ET
_market_hours = datetime(2026, 6, 16, 14, 30, tzinfo=UTC)

with patch("options_agent.orchestrator.reason", return_value=stub_reasoner()):
    result = run_entry_cycle(config, broker=broker, engine=engine, _now=_market_hours)

print(result.action_taken)
```

## Known gaps

**client_order_id gap (WP-0 amendment required):** `broker.py` generates a `client_order_id` at submit time but it is NOT persisted on the `Order` model. This makes unmatched-local recovery impossible — any PENDING_SUBMIT order without a `broker_order_id` triggers HALT. A future WP-0 amendment will add `client_order_id` to `Order` and enable resolution.
