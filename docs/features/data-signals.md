# Data & Signals

**Module:** `options_agent/data/`
**Credentials required:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`
**Status:** in progress (`news.py` sentiment remains — phase 2, optional)

Turns raw Alpaca market data into the compact, token-efficient inputs the agent consumes. The provider client handles caching and rate-limit back-off internally — callers see a single blocking call per symbol.

## Sub-modules

| File | Responsibility |
|---|---|
| `providers/alpaca_data.py` | `AlpacaDataClient` — Alpaca options data with per-cycle cache + retry |
| `chains.py` | `get_filtered_chain` — fetch + liquidity/DTE/delta pre-filter → `FilteredChain`; `get_held_leg_greeks` — unfiltered Greek fetch for open positions |
| `greeks_iv.py` | `get_atm_iv` — ATM IV extraction; `enrich_greeks_iv` — validate/normalise Greeks and IV on raw contracts |
| `iv_rank.py` | `record_daily_iv` — idempotent upsert into `iv_history`; `compute_iv_rank` / `compute_iv_percentile` — 252-day lookback |
| `market.py` | `get_universe_snapshot` — VIX fetch, regime classification, per-symbol prices |
| `tools.py` | `load_universe`; `build_real_tool_impls` — wires all live tool implementations (see [orchestrator.md](orchestrator.md)) |

## AlpacaDataClient

Scoped to one agent cycle — call `begin_cycle()` at the start to clear the previous cycle's cache. `fetch_option_chain(symbol)` returns `list[RawOptionContract]` (bid/ask/mid, Greeks, IV, OI, volume), served from an in-memory per-cycle cache; `fetch_latest_price(symbol)` returns the underlying price.

## Filtered chain

`get_filtered_chain(symbol, client, config.limits.chain_filter)` returns a `FilteredChain` with post-filter contracts plus data-quality signals (`excluded_for_missing_greeks`, `oi_available`). Default thresholds from `config.toml`:

| Threshold | Default |
|---|---|
| `min_open_interest` | 500 |
| `max_spread_pct_of_mid` | 10 % (absolute floor $0.05) |
| `min_dte` / `max_dte` | 20 – 45 days |
| `min_abs_delta` / `max_abs_delta` | 0.15 – 0.45 |

`enrich_greeks_iv(raw)` corrects or drops contracts with missing/implausible Greeks; compare input/output lengths to see per-symbol attrition.

## Regime classification

`get_universe_snapshot(symbols, provider, vol_provider, playbook)` returns VIX level, a `MarketRegime` (`low_vol` / `normal` / `high_vol` / `unknown`, thresholds from `[playbook]` config), and per-symbol prices.

- **Degraded-context operation:** if VIX is unavailable, regimes are `UNKNOWN` and the snapshot is still returned. `vix_level` is `0.0` as a sentinel — check `market_regime != UNKNOWN`, not the number.
- Symbols that fail price fetch are excluded from `symbol_snapshots` with a warning; callers treat absent symbols as not-tradeable this cycle.
- **v1 note:** `SymbolSnapshot.regime` echoes the market-wide regime — it is not an independent per-symbol classification.

## Held-leg Greeks

`get_held_leg_greeks(positions, provider)` fetches the **full** chain (no DTE/delta filter) for each held underlying and returns `(underlying, right, strike, expiration) → (delta, vega, theta)`. This exists because a held leg that has aged below the entry filter's `dte_min` would otherwise be absent from the entry chain and silently contribute `0.0` to portfolio Greek aggregation. Contracts no longer quoted (expired) are omitted; callers fall back to `0.0` with a warning.

## IV rank and percentile

`iv_rank.py` maintains a rolling 252-trading-day history of ATM IV and computes rank/percentile on demand. `run_daily_iv_job()` (scheduler) records one observation per symbol per session; the entry cycle enriches each `SymbolSnapshot` with live rank/percentile from the same table.

- **Commensurability invariant:** the daily capture job and the live enrichment both call `get_atm_iv()` with identical defaults (`target_dte=30`), keeping stored history and the live numerator comparable.
- **Missing-data policy / warm-up:** with fewer than `min_days=30` observations, rank/percentile return `None`. Symbols with `iv_rank=None` are labelled ineligible in the assembler context and excluded from entry candidates — so during the first ~30 sessions of a fresh paper run the expected outcome is `NO_ACTION` every entry cycle, lifting symbol by symbol as history accumulates.
- **Bounds:** rank is clamped to `[0, 1]` — a new 52-week extreme clamps to the bound, matching external references that fold today's observation into the window. Percentile is naturally bounded by its formula.

### Bootstrapping `iv_history` for a new paper run

Without seeding, every entry cycle NO_ACTIONs for ~30 sessions. `uv run python scripts/backfill_iv_history.py` loads up to 252 sessions per universe symbol using 30-day trailing realized volatility (HV30, from yfinance closes) as an ATM-IV proxy, and prints a per-symbol summary for anomaly-spotting. Known trade-off: HV30 correlates with but differs from real IV (especially around earnings); the approximation degrades gracefully as live observations replace bootstrapped rows one date at a time (`record_daily_iv` is idempotent on `(symbol, date)` — the live observation wins). Exits non-zero if fewer than 3 symbols end up with a non-null rank.

## Not yet implemented

- `news.py` — headline sentiment (phase 2, optional)
