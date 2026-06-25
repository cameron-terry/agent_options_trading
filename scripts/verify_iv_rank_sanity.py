"""WP-3.4 DoD sanity check — Option C (formula-level spot check).

Compares computed ATM IV against Barchart / Market Chameleon for ≥ 3 tickers.

Usage:
    uv run python scripts/verify_iv_rank_sanity.py

Requires ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment (or .env).

After running, open Barchart for each ticker and verify:
  1. "Current IV" column matches the ATM IV printed here (± a few % is normal
     due to quote timing; > 5% warrants investigation).
  2. Use Barchart's displayed "IV High" and "IV Low" (52-week) to manually
     compute rank = (current_iv - iv_low) / (iv_high - iv_low) and confirm
     it matches the formula output printed here.

Barchart links (open after running):
  SPY  — https://www.barchart.com/stocks/quotes/SPY/volatility-greeks
  QQQ  — https://www.barchart.com/stocks/quotes/QQQ/volatility-greeks
  AAPL — https://www.barchart.com/stocks/quotes/AAPL/volatility-greeks

Market Chameleon links (alternative):
  SPY  — https://marketchameleon.com/Overview/SPY/IV/
  QQQ  — https://marketchameleon.com/Overview/QQQ/IV/
  AAPL — https://marketchameleon.com/Overview/AAPL/IV/
"""

from __future__ import annotations

import sys
from datetime import date

from options_agent.data.greeks_iv import enrich_greeks_iv, get_atm_iv
from options_agent.data.providers.alpaca_data import AlpacaDataClient

TICKERS = ["SPY", "QQQ", "AAPL"]
TARGET_DTE = 30


def main() -> None:
    try:
        client = AlpacaDataClient()
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"WP-3.4 ATM IV spot check — {date.today()}")
    print(
        f"ATM definition: nearest-to-{TARGET_DTE}-DTE expiration,"
        " call with delta closest to 0.5"
    )
    print()
    print(
        f"{'Ticker':<8} {'Spot price':>12} {'ATM IV':>10}"
        f" {'Selected expiry':>18} {'Contracts scanned':>18}"
    )
    print("-" * 72)

    results: list[dict] = []
    for ticker in TICKERS:
        try:
            raw_chain = client.fetch_option_chain(ticker)
            spot = client.fetch_latest_price(ticker)
            enriched = enrich_greeks_iv(raw_chain)
            atm_iv = get_atm_iv(enriched, spot_price=spot, target_dte=TARGET_DTE)

            # Find the expiry that get_atm_iv selected (nearest to target_dte).
            today = date.today()
            future_calls = [
                c
                for c in enriched
                if c.right == "call"
                and c.implied_volatility is not None
                and (c.expiration - today).days > 0
            ]
            if future_calls:
                selected_exp = min(
                    {c.expiration for c in future_calls},
                    key=lambda exp: abs((exp - today).days - TARGET_DTE),
                )
            else:
                selected_exp = None

            iv_str = (
                f"{atm_iv:.4f} ({atm_iv * 100:.1f}%)" if atm_iv is not None else "None"
            )
            exp_str = str(selected_exp) if selected_exp else "N/A"
            print(
                f"{ticker:<8} {spot:>12.2f} {iv_str:>10}"
                f" {exp_str:>18} {len(enriched):>18}"
            )
            results.append(
                {"ticker": ticker, "spot": spot, "atm_iv": atm_iv, "exp": selected_exp}
            )
        except Exception as exc:  # noqa: BLE001
            print(f"{ticker:<8} ERROR: {exc}")

    print()
    print("Manual check steps:")
    print("  1. Open each Barchart link (see script header).")
    print("  2. Record Barchart's 'Current IV', '52w IV High', '52w IV Low'.")
    print("  3. Confirm 'Current IV' is within ~5% of ATM IV above.")
    print("  4. Compute: rank = (current_iv - iv_low) / (iv_high - iv_low)")
    print(
        "     and confirm it matches Barchart's displayed 'IV Rank' or 'IV Percentile'."
    )
    print()
    pr_url = "https://github.com/cameron-terry/agent_options_trading/pull/44"
    print(f"Record results in the PR comment on {pr_url}")
    print()
    print("Expected format for PR comment:")
    header = (
        "  | Ticker | Alpaca ATM IV | Barchart Current IV"
        " | Delta | Barchart IV Rank | Formula Rank | Match? |"
    )
    print(header)
    for r in results:
        iv = f"{r['atm_iv']:.4f}" if r["atm_iv"] is not None else "None"
        row = (
            f"  | {r['ticker']} | {iv}"
            " | _fill in_ | _fill in_ | _fill in_ | _fill in_ | _fill in_ |"
        )
        print(row)


if __name__ == "__main__":
    main()
