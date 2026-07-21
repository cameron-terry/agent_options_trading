# Broker & Execution

**Module:** `options_agent/execution/`  
**Credentials required:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (paper account)  
**Status:** complete (WP-1)

Wraps `alpaca-py` for order placement and account/position reconciliation. The broker is always the source of truth for fills; the local DB is the source of truth for intent.

## Sub-modules

| File | Responsibility |
|---|---|
| `broker.py` | `BrokerClient` — order submit/cancel, account/position queries, session retry |
| `orders.py` | OCC symbol construction, limit price computation, Alpaca request builders |
| `reconcile.py` | Pull live broker state, diff against DB, detect fills/expirations/assignments |

## Connecting

`BrokerClient` reads credentials from the environment and `config.alpaca_paper` to select the endpoint.

```python
import os
os.environ["ALPACA_API_KEY"] = "..."
os.environ["ALPACA_SECRET_KEY"] = "..."

from pathlib import Path
from options_agent.config import Config
from options_agent.execution.broker import BrokerClient

config = Config.from_toml(Path("config.toml"))   # alpaca_paper = true by default
broker = BrokerClient(config)
```

## Account state

```python
account = broker.get_account()
print(account.equity)
print(account.options_buying_power)
print(account.options_approved_level)   # must be >= 2 for spreads
print(broker.is_paper)                  # True when using paper endpoint
```

## Inspecting live positions and orders

```python
positions = broker.get_all_positions()
for p in positions:
    print(p.symbol, p.qty, p.unrealized_pl)

open_orders = broker.list_open_orders()
for o in open_orders:
    print(o.id, o.status, o.symbol)
```

## Submitting an order (paper only)

`submit_multi_leg` takes a validated `TradeProposal`, contract count, and a pre-computed limit price. It blocks until filled or `order_poll_timeout_secs` elapses, then returns the current `Order` (which may still be `WORKING`).

> **Note:** The stub reasoner hardcodes SPY strikes (~450/445) from when the system was built. Alpaca paper rejects legs whose OCC symbols don't exist on the current chain. Build legs from `get_filtered_chain` instead so strikes reflect what's actually tradeable today.

```python
import uuid
from options_agent.contracts.data import PortfolioState
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.data.chains import get_filtered_chain
from options_agent.data.providers.alpaca_data import AlpacaDataClient
from options_agent.risk.validator import validate_structural
from options_agent.risk.sizing import size

# Pull a real chain so leg strikes/expiries exist on the paper endpoint
data_client = AlpacaDataClient()
data_client.begin_cycle()
chain = get_filtered_chain("SPY", data_client, config.limits.chain_filter)

# Both legs must share the same expiration — Alpaca treats cross-expiry legs
# as unrelated positions and charges naked-put margin on the short leg.
puts = [c for c in chain.contracts if c.right == "put"]
target_expiry = puts[0].expiration
same_exp_puts = [c for c in puts if c.expiration == target_expiry]
sell_leg_data, buy_leg_data = same_exp_puts[0], same_exp_puts[1]

proposal = TradeProposal(
    action="OPEN",
    underlying="SPY",
    strategy="bull_put_spread",
    legs=[
        Leg(right="put", side="sell", strike=sell_leg_data.strike, expiration=sell_leg_data.expiration),
        Leg(right="put", side="buy",  strike=buy_leg_data.strike,  expiration=buy_leg_data.expiration),
    ],
    thesis="Test submission against live paper chain.",
    iv_rationale="N/A — manual test.",
    catalyst_check="N/A — manual test.",
    conviction=0.65,
    est_max_loss=abs(sell_leg_data.strike - buy_leg_data.strike) * 100,
    est_max_profit=150.0,
    breakevens=[sell_leg_data.strike - 1.50],
    net_delta=sell_leg_data.delta + buy_leg_data.delta,
    net_theta=sell_leg_data.theta + buy_leg_data.theta,
    net_vega=sell_leg_data.vega + buy_leg_data.vega,
    exit_plan=ExitPlan(profit_target_pct=0.50, stop_loss_mult=2.0, time_stop_dte=21),
    informed_by=[],
)
assert validate_structural(proposal, config.limits).passed

account = broker.get_account()
portfolio_state = PortfolioState(
    positions=[],
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

sizing = size(proposal, portfolio_state, config.limits)
position_id = str(uuid.uuid4())
limit_price = -1.50   # negative = net credit

order = broker.submit_multi_leg(proposal, sizing.contracts, limit_price, position_id)
print(order.broker_order_id, order.status)
```

Limit price sign convention: **negative = net credit received** (consistent with `Position.entry_net_amount`). For debit spreads pass a positive value.

## Cancelling an order

```python
cancelled = broker.cancel(order)
print(cancelled.status)   # CANCELLED
```

## Reconcile

`reconcile` pulls the live broker account and diffs it against the local DB in a single call. It detects fills, expirations, and assignments and writes the results back through the `Connection`.

```python
from options_agent.state.db import build_engine, get_connection
from options_agent.execution.reconcile import reconcile

engine = build_engine(config.db_url)

with get_connection(engine) as conn:
    stat_diff = reconcile(broker, conn)

print(stat_diff.newly_filled)             # list[Order] — fills detected this pass
print(stat_diff.newly_partial)            # list[Order] — incremental partial fills
print(stat_diff.newly_cancelled)          # list[Order]
print(stat_diff.expired_option_positions) # list[Position] — expired worthless
print(stat_diff.assigned_positions)       # list[AssignmentEvent] — option assignments
print(stat_diff.anomalies)               # list[ReconcileAnomaly] — needs human review
print(stat_diff.reconciled_at)           # UTC timestamp of this pass
```

Reconcile is idempotent — running it twice produces the same DB state.

A `PENDING_OPEN` position whose only order ends `CANCELLED` or `REJECTED` with zero fill is closed out (`status=CLOSED`, `realized_pnl=0.0`) rather than left stranded — `PositionStatus` has no dedicated "failed to open" value, and `risk/validator.py`'s duplicate/conflict check treats `PENDING_OPEN` as an active position, so an unclosed one would silently and permanently affect future proposals for that underlying. An order that partially filled before the remainder was cancelled is left alone — that position has real, unclosed exposure.

## Fill-time risk correction (WP-1 follow-up)

`Position.est_max_loss`/`est_max_profit` are set at position creation from the pre-trade chain-mid estimate (`risk/structure.py::compute_structure_metrics`, applied by the orchestrator's ENRICH+VALIDATE step). Once the order actually fills, `risk/structure.py::apply_fill_metrics` recomputes both values from the real `net_fill_price` — the same expiration-payoff analysis, just anchored to the confirmed price instead of the pre-trade mid. This runs automatically on both fill paths:

- **Synchronous** — the order fills within `submit_multi_leg`'s poll window at cycle-top (`orchestrator.py`).
- **Asynchronous** — the order is still `WORKING` at cycle-top and fills later; the next `reconcile()` pass detects it (`execution/reconcile.py::_apply_fill_to_position`).

Both values remain **per-contract** (matching `TradeProposal`, `risk/sizing.py`, and `monitor/exits.py` — multiply by `Position.quantity` for whole-position dollars). A deviation >20% between the pre-fill estimate and the fill-corrected value is logged as a warning (`risk/structure.py::_log_deviation`); mixed-expiration structures (where payoff analysis doesn't apply) fall back to the pre-fill estimate unchanged.

Positions opened before this fix landed carry a stale, mid-based `est_max_loss`/`est_max_profit`. `scripts/backfill_position_fill_metrics.py` corrects existing open positions from their opening order's `net_fill_price` — dry-run by default, `--apply` to write.

### Per-leg fill audit trail

Every broker fill is recorded as one or more immutable `fill_events` rows (`options_agent.state.crud.list_fill_events_for_order`), and `Order.legs_filled` is kept as a full per-leg breakdown (`LegFill`: leg spec, filled qty, fill price). For a multi-leg (mleg) order, Alpaca returns real, distinct per-leg fill data on a plain `get_order_by_id` fetch — no `nested=True` filter needed — so one `FillEvent` is written per leg, matched to the position's legs by OCC symbol. Single-leg orders get one order-granularity `FillEvent`.

`reconcile()` records fills two ways:

- **Incrementally**, inside the main per-order loop, for orders still non-terminal in the DB (`WORKING` / `PARTIALLY_FILLED`) when a broker fill is observed.
- **Via a backfill pass** (`_backfill_missing_fill_events`, via `state.crud.list_orders_with_unrecorded_fills`), for orders that reached the DB *already* at a terminal `FILLED` status — e.g. an order that filled inside `broker.submit()`/`submit_multi_leg()`'s synchronous poll window. Such orders are invisible to the main loop (which reads `list_pending_orders()`, excluding terminal orders by design), so the backfill pass is the only thing that ever records their fills. It runs on every `reconcile()` call and is self-limiting: an order drops out of the candidate query as soon as its recorded `fill_events` qty catches up, so it costs nothing once caught up.

A consistency check compares the signed sum of per-leg fill prices against the order's combo `net_fill_price` (tolerance 0.02) and appends a `ReconcileAnomaly` on mismatch — surfaced via `stat_diff.anomalies`, not a separate alert channel.

```python
from options_agent.state.crud import list_fill_events_for_order

with get_connection(engine) as conn:
    events = list_fill_events_for_order(conn, order.id)
for fe in events:
    print(fe.leg_symbol, fe.filled_qty, fe.fill_price, fe.occurred_at)
```

## Rate limits and retries

`BrokerClient` wraps every Alpaca call with exponential back-off on 429 / 5xx responses. `order_poll_interval_secs` and `order_poll_timeout_secs` in `config.toml` control fill-status polling behaviour. The client re-initialises its internal `alpaca-py` session automatically on auth expiry.
