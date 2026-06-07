from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from options_agent.contracts.state import Position


class ChainFilterParams(BaseModel):
    """Thresholds applied when building a FilteredChain.

    Carried on the chain so the journal can answer 'why did the agent only see
    these strikes?' without re-fetching. Confirm field values match Limits defaults
    in risk/limits.py before freeze.
    """

    dte_min: int
    dte_max: int
    delta_min: float
    delta_max: float
    min_open_interest: int
    max_spread_width: float


class OptionContract(BaseModel):
    """One row in a filtered options chain.

    symbol is the OCC option symbol — the key WP-1 uses to map TradeProposal legs
    to tradeable instruments. Reconstructing it from strike/expiration/right at
    order-submission time is fragile; carry it here instead.

    dte is precomputed from (expiration - as_of) to prevent the agent and other
    consumers from doing date math independently (and getting it wrong).

    gamma is intentionally omitted — not a meaningful per-row entry signal at the
    chain level; net_dollar_gamma lives on PortfolioState for portfolio risk.
    """

    symbol: str
    strike: float
    expiration: date
    right: Literal["call", "put"]
    bid: float
    ask: float
    mid: float
    volume: int
    open_interest: int
    delta: float
    theta: float
    vega: float
    iv: float
    spread_width: float
    dte: int


class FilteredChain(BaseModel):
    """Pre-filtered options chain returned by get_filtered_chain().

    Covers one underlying for one agent cycle. Header fields (underlying_price,
    as_of, filter_params) are per-chain — not repeated on every row.

    filter_params embeds the thresholds used so the chain is self-describing;
    the assembler (WP-6) renders contracts to a compact tabular string for the
    prompt while this typed object flows through code (WP-1 mapping, validation,
    journal).
    """

    underlying: str
    underlying_price: float
    as_of: datetime
    filter_params: ChainFilterParams
    contracts: list[OptionContract]


class PortfolioState(BaseModel):
    """Full account + position snapshot consumed by the agent and validator each cycle.

    Net Greeks are computed once by context/portfolio.py and embedded here so
    WP-4 (validator) and WP-6 (agent) always see identical values — never
    recomputed independently to prevent divergence between the guardrail check and
    what the agent reasoned over.

    Unit note — all net_dollar_* fields are in USD:
      net_dollar_delta — $ change per $1 move in the underlying (across all positions)
      net_dollar_gamma — $ change in delta per $1 move in the underlying
      net_dollar_theta — $ time decay per calendar day
      net_dollar_vega  — $ change per 1 vol-point (1%) move in IV

    IMPORTANT: Confirm these units with the WP-4 owner before freeze. They must
    match the Limits bands in risk/limits.py to avoid silent mis-comparisons.

    options_buying_power is Alpaca-specific; may differ from buying_power on
    margin accounts. approval_level is broker-reported (not from Config) so it
    reflects the live account state if it changes between cycles.

    realized_pnl_today covers intraday closed positions; lifetime P&L analytics
    are WP-7's responsibility, derived from the journal.
    """

    positions: list[Position]
    account_equity: float
    buying_power: float
    options_buying_power: float
    unrealized_pnl: float
    realized_pnl_today: float
    approval_level: int
    net_dollar_delta: float
    net_dollar_gamma: float
    net_dollar_theta: float
    net_dollar_vega: float


class MacroEvent(BaseModel):
    """A market-wide scheduled event.

    Lives on UniverseSnapshot, not per-symbol EventInfo — FOMC/CPI/NFP are not
    per-ticker facts.
    """

    name: str
    event_date: date
    event_type: Literal["FOMC", "CPI", "NFP", "OTHER"]


class SymbolSnapshot(BaseModel):
    """Per-symbol market state for one ticker in the trading universe.

    price is required — a symbol with no price is excluded from the snapshot
    entirely rather than emitted with a null. An absent symbol means 'not
    tradeable this cycle'; a symbol present here is asserting its price is valid.

    iv_rank / iv_percentile are Optional because historical IV may be unavailable
    for newer names. None ≠ 0 — a None iv_rank means 'exclude from entry
    candidates today' (WP-4 entry gate must enforce this, not treat it as low IV).

    days_to_earnings replaces a boolean proximity flag. The validator's event-
    blackout gate needs the exact count, not near/not-near. None = no known
    upcoming earnings within the lookahead window.

    regime and historical_vol are Optional (derived / history-dependent; tolerate
    data gaps without excluding the symbol).
    """

    symbol: str
    price: float
    iv_rank: float | None
    iv_percentile: float | None
    historical_vol: float | None
    regime: str | None
    days_to_earnings: int | None


class UniverseSnapshot(BaseModel):
    """Full universe state returned by get_universe_snapshot().

    symbol_snapshots is keyed by ticker for O(1) lookup by the agent and
    validator (access pattern: look up one symbol, not iterate all).

    vix_level and market_regime are market-wide — lifting them here avoids
    repeating one value across every SymbolSnapshot row.

    macro_events lists market-wide scheduled events (FOMC, CPI, NFP) within the
    lookahead window. Per-symbol events (earnings, ex-div) live in EventInfo.

    Derive SymbolSnapshot.days_to_earnings from the same EventInfo fetch that
    populates the EventInfo dict — do not recompute from a separate source to
    prevent drift between the two representations.
    """

    symbol_snapshots: dict[str, SymbolSnapshot]
    vix_level: float
    market_regime: str
    macro_events: list[MacroEvent]
    as_of: datetime


class EarningsEvent(BaseModel):
    """Upcoming earnings date for one symbol."""

    event_date: date
    confirmed: bool


class ExDividendEvent(BaseModel):
    """Upcoming ex-dividend date for one symbol."""

    event_date: date
    amount: float


class EventInfo(BaseModel):
    """Upcoming events for one symbol.

    Returned as values in the dict[str, EventInfo] from get_events(). The tool
    accepts a list of symbols and returns one entry per symbol; the lookahead
    window is a tool parameter (default ~60 days from Config), not hardcoded here.

    earnings and ex_dividend are None when no event falls within the window.

    SymbolSnapshot.days_to_earnings is a denormalized convenience field derived
    from earnings.event_date; populate both from the same fetch in a single place
    to prevent drift. Do not recompute days_to_earnings independently.

    Macro events (FOMC/CPI/NFP) are market-wide and live on UniverseSnapshot.
    """

    symbol: str
    earnings: EarningsEvent | None
    ex_dividend: ExDividendEvent | None
