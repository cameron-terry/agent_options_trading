# Data & Signals

**Module:** `options_agent/data/`  
**Credentials required:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`  
**Status:** in progress (WP-3.1–3.3 complete; events/earnings sub-task pending)

Turns raw Alpaca market data into the compact, token-efficient inputs the agent consumes. The provider client handles caching and rate-limit back-off internally — callers see a single blocking call per symbol.

## Sub-modules

| File | Responsibility |
|---|---|
| `providers/alpaca_data.py` | `AlpacaDataClient` — Alpaca options data with per-cycle cache + retry |
| `chains.py` | `get_filtered_chain` — fetch + liquidity/DTE/delta pre-filter → `FilteredChain` |
| `greeks_iv.py` | `enrich_greeks_iv` — validate and normalise Greeks/IV on raw contracts |

## AlpacaDataClient

The client is scoped to one agent cycle. Call `begin_cycle()` at the start to clear the previous cycle's cache.

```python
import os
os.environ["ALPACA_API_KEY"] = "..."
os.environ["ALPACA_SECRET_KEY"] = "..."

from options_agent.data.providers.alpaca_data import AlpacaDataClient

client = AlpacaDataClient()
client.begin_cycle()

price = client.fetch_latest_price("SPY")
print(price)   # e.g. 523.41

contracts = client.fetch_option_chain("SPY")
print(len(contracts))          # raw count before filtering
print(contracts[0])            # RawOptionContract
```

`fetch_option_chain` returns `list[RawOptionContract]` — one entry per OCC symbol with bid/ask/mid, delta, gamma, theta, vega, IV, OI, and volume. Calls within the same cycle are served from an in-memory cache keyed on symbol.

## Filtered chain

Requires the `client` and `config` from the section above.

```python
from pathlib import Path
from options_agent.config import Config
from options_agent.data.chains import get_filtered_chain

config = Config.from_toml(Path("config.toml"))

chain = get_filtered_chain("SPY", client, config.limits.chain_filter)
print(chain.underlying)                      # "SPY"
print(chain.underlying_price)               # current price
print(len(chain.contracts))                 # post-filter count
print(chain.excluded_for_missing_greeks)    # contracts dropped for missing Greeks/IV
print(chain.oi_available)                   # False when Alpaca returns no OI data
print(chain.contracts[0])                   # OptionContract
```

Default filter thresholds (from `config.toml`):

| Threshold | Default |
|---|---|
| `min_open_interest` | 500 |
| `max_spread_pct_of_mid` | 10 % |
| `max_spread_abs_floor` | $0.05 |
| `min_dte` / `max_dte` | 20 – 45 days |
| `min_abs_delta` / `max_abs_delta` | 0.15 – 0.45 |

## Greeks / IV enrichment

Requires `client` from the AlpacaDataClient section above.

```python
from options_agent.data.greeks_iv import enrich_greeks_iv

raw = client.fetch_option_chain("SPY")
enriched = enrich_greeks_iv(raw)

for c in enriched[:3]:
    print(c.symbol, c.delta, c.implied_volatility, c.greek_source)
```

Contracts with missing or implausible Greeks are either corrected (where recoverable) or dropped. Check `len(raw)` vs `len(enriched)` to see the attrition rate for a symbol.

## What's not yet implemented

- `events.py` — earnings-proximity flag and macro calendar (WP-3 remaining sub-task)
- `market.py` — VIX / regime classification
- `news.py` — headline sentiment (phase 2, optional)
