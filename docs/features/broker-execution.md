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

## Rate limits and retries

`BrokerClient` wraps every Alpaca call with exponential back-off on 429 / 5xx responses. `order_poll_interval_secs` and `order_poll_timeout_secs` in `config.toml` control fill-status polling behaviour. The client re-initialises its internal `alpaca-py` session automatically on auth expiry.
