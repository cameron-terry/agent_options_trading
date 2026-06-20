# Data & Signals

**Module:** `options_agent/data/`  
**Credentials required:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`  
**Status:** in progress (WP-3.1–3.3, 3.6, 3.8 complete; IV-rank WP-3.4 pending)

Turns raw Alpaca market data into the compact, token-efficient inputs the agent consumes. The provider client handles caching and rate-limit back-off internally — callers see a single blocking call per symbol.

## Sub-modules

| File | Responsibility |
|---|---|
| `providers/alpaca_data.py` | `AlpacaDataClient` — Alpaca options data with per-cycle cache + retry |
| `chains.py` | `get_filtered_chain` — fetch + liquidity/DTE/delta pre-filter → `FilteredChain`; `get_held_leg_greeks` — unfiltered Greek fetch for open positions |
| `greeks_iv.py` | `enrich_greeks_iv` — validate and normalise Greeks/IV on raw contracts |
| `market.py` | `get_universe_snapshot` — VIX fetch, regime classification, per-symbol prices |

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

## Regime classification (WP-3.6)

Requires `provider`, `vol_provider`, and `config` from the sections above.

```python
from pathlib import Path
from options_agent.config import Config
from options_agent.data.market import get_universe_snapshot
from options_agent.data.providers.alpaca_data import AlpacaDataClient
from options_agent.data.providers.yfinance_volatility_provider import YfinanceVolatilityProvider

config = Config.from_toml(Path("config.toml"))
provider = AlpacaDataClient()
provider.begin_cycle()
vol_provider = YfinanceVolatilityProvider()

snapshot = get_universe_snapshot(
    symbols=["SPY", "QQQ"],
    provider=provider,
    vol_provider=vol_provider,
    playbook=config.playbook,
)

print(snapshot.vix_level)       # e.g. 18.3
print(snapshot.market_regime)   # MarketRegime.NORMAL / LOW_VOL / HIGH_VOL / UNKNOWN
print(snapshot.as_of)           # UTC timestamp

for sym, s in snapshot.symbol_snapshots.items():
    print(sym, s.price, s.regime)   # e.g. SPY 523.41 MarketRegime.NORMAL
```

`MarketRegime` values: `low_vol`, `normal`, `high_vol`, `unknown`. Thresholds are set by `vix_low_vol_threshold` and `vix_high_vol_threshold` in `[playbook]` in `config.toml`. If VIX is unavailable, `market_regime` and all per-symbol `regime` values are set to `UNKNOWN` and the snapshot is still returned (degraded-context operation). `vix_level` is `0.0` as a sentinel when unavailable — check `market_regime != UNKNOWN` rather than the numeric value.

Symbols that fail price fetch are excluded from `symbol_snapshots` with a WARNING. The caller should handle absent symbols as not-tradeable this cycle.

**v1 per-symbol regime note:** `SymbolSnapshot.regime` echoes `market_regime` for every symbol. It is not an independent per-symbol classification — do not treat it as such in the WP-6 playbook.

## Held-leg Greeks (WP-3.8)

Requires `provider` from the AlpacaDataClient section above and a list of open `Position` objects.

```python
from options_agent.data.chains import get_held_leg_greeks
from options_agent.state.db import build_engine, get_connection
from options_agent.state.crud import list_open_positions

engine = build_engine("sqlite:///options_agent.db")

with get_connection(engine) as conn:
    positions = list_open_positions(conn)

greek_map = get_held_leg_greeks(positions, provider)

# Key: (underlying, right, strike, expiration_isoformat)
# Value: (delta, vega, theta)
for key, (delta, vega, theta) in greek_map.items():
    print(key, delta, vega, theta)
```

Unlike `get_filtered_chain`, no DTE window or delta range is applied — the full chain is fetched for each underlying. This closes the gap where a held leg that has aged below `dte_min` would be absent from the entry-chain and silently contribute `0.0` to portfolio Greek aggregation. Contracts absent from the provider snapshot (expired options no longer quoted) are simply omitted; callers should fall back to `0.0` and log a warning.

## What's not yet implemented

- IV rank / percentile / historical vol on `SymbolSnapshot` — WP-3.4 (currently `None`)
- `news.py` — headline sentiment (phase 2, optional)
