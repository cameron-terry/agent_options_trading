# Data & Signals

**Module:** `options_agent/data/`  
**Credentials required:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`  
**Status:** in progress (WP-3.1–3.4, 3.4b, 3.6, 3.8, 8.10 complete)

Turns raw Alpaca market data into the compact, token-efficient inputs the agent consumes. The provider client handles caching and rate-limit back-off internally — callers see a single blocking call per symbol.

## Sub-modules

| File | Responsibility |
|---|---|
| `providers/alpaca_data.py` | `AlpacaDataClient` — Alpaca options data with per-cycle cache + retry |
| `chains.py` | `get_filtered_chain` — fetch + liquidity/DTE/delta pre-filter → `FilteredChain`; `get_held_leg_greeks` — unfiltered Greek fetch for open positions |
| `greeks_iv.py` | `get_atm_iv` — ATM IV extraction from a chain; `enrich_greeks_iv` — validate and normalise Greeks/IV on raw contracts |
| `iv_rank.py` | `record_daily_iv` — idempotent upsert into `iv_history`; `compute_iv_rank` / `compute_iv_percentile` — 252-day lookback rank/percentile |
| `market.py` | `get_universe_snapshot` — VIX fetch, regime classification, per-symbol prices |
| `tools.py` | `load_universe` — shared universe reader; `build_real_tool_impls` — wires all live tool implementations |

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

## IV rank and percentile (WP-3.4 + WP-8.10)

`iv_rank.py` maintains a rolling 252-trading-day history of ATM IV and computes rank/percentile on demand. `run_daily_iv_job()` (in `orchestrator.py`) records one IV observation per symbol per session, scheduled at `session_close + daily_iv_capture_offset_minutes`. The entry cycle's `get_universe_snapshot` tool enriches each `SymbolSnapshot` with live rank/percentile from the same history table.

**Correctness invariant:** both the daily capture job and the live enrichment at assembly time call `get_atm_iv()` with identical default parameters (`target_dte=30`). Using the same function at both sites keeps the stored history and the live `current_iv` numerator commensurable.

**Missing data policy:** if fewer than `min_days=30` historical observations exist, `compute_iv_rank` / `compute_iv_percentile` return `None`. Symbols with `iv_rank=None` are labelled `ineligible (iv_rank unknown)` in the assembler context and excluded from entry candidates by WP-4 gates. This is the expected state during the first ~30 sessions of the paper run.

**Bounds:** `compute_iv_rank` is clamped to `[0.0, 1.0]` — a `current_iv` beyond the trailing window (a new 52-week high/low) is clamped to the nearest bound rather than left unbounded, matching external references (Barchart, Market Chameleon) that fold today's observation into the window before ranking. `compute_iv_percentile` is already naturally bounded to `[0.0, 1.0]` by its `count_below / n` formula and needs no clamping.

```python
from datetime import date
from options_agent.data.greeks_iv import get_atm_iv
from options_agent.data.iv_rank import record_daily_iv, compute_iv_rank, compute_iv_percentile
from options_agent.state.db import build_engine, get_connection

engine = build_engine("sqlite:///options_agent.db")

# Manually record today's IV for a symbol:
client = AlpacaDataClient()
client.begin_cycle()
contracts = client.fetch_option_chain("SPY")
price = client.fetch_latest_price("SPY")
atm_iv = get_atm_iv(contracts, price)   # float | None

if atm_iv is not None:
    with get_connection(engine) as conn:
        record_daily_iv("SPY", atm_iv, date.today(), conn)

# Query rank and percentile:
with get_connection(engine) as conn:
    rank = compute_iv_rank("SPY", atm_iv, conn)        # float | None
    pct  = compute_iv_percentile("SPY", atm_iv, conn)  # float | None
```

## Bootstrapping iv_history for a new paper run (WP-3.4b)

The daily IV capture job accumulates one observation per trading session going forward.
`compute_iv_rank` / `compute_iv_percentile` require `min_days=30` observations before
returning non-null values, and ideally 252 for a full 52-week window. Without seeding,
every entry cycle returns `iv_rank=None` for all symbols and the agent NO_ACTIONs
indefinitely.

Run the one-shot backfill script before starting the paper run:

```bash
uv run python scripts/backfill_iv_history.py
```

The script fetches ~15 months of daily closing prices from yfinance for every symbol in
`universe.txt`, computes annualized 30-day trailing realized volatility (HV30) as an ATM
IV proxy, and loads up to 252 sessions into the production `iv_history` table via
`record_daily_iv()`. It prints a per-symbol summary with session count, HV range, and
resulting IV rank/percentile so the operator can spot obvious anomalies before the first
entry cycle.

**Known trade-off:** the bootstrapped rows use realized vol (HV30) while the live daily
job accumulates real ATM IV from options chains. The two metrics correlate but differ,
especially around earnings events. The approximation degrades gracefully: over a live
252-trading-day window, real IV observations replace bootstrapped rows one-by-one until
the window is fully live-sourced. `record_daily_iv()` is idempotent on `(symbol, date)`,
so re-running the script after real data exists for a date is safe — the live observation
takes precedence.

The script exits with code 1 and an error message if fewer than 3 symbols end up with
non-null IV rank (indicating an insufficient history load or data-source failure).

## What's not yet implemented

- `news.py` — headline sentiment (phase 2, optional)
