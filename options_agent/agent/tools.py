"""Anthropic SDK tool definitions for the agent's read-only tool set (WP-6.1).

All tools in AGENT_TOOLS are READ-ONLY. No tool in this list can place, modify,
or cancel an order. This invariant is enforced by inspection in tests/test_tools.py.

Architecture (DI pattern):
    These definitions tell the LLM *what* tools exist. The *implementations*
    — callables that run when the LLM calls a tool — are a separate concern.
    Real implementations are provided by WP-3 and injected into reasoner.py
    at call time. Mocks live in agent/tools_mock.py (development/test only).
    See tools_mock.py module docstring for the production guard.

Tool surface (6 read-only tools):
    get_portfolio_state      — full account + position snapshot
    get_universe_snapshot    — per-symbol market state for the configured universe
    get_filtered_chain       — pre-filtered options chain for one underlying
    get_events               — per-symbol earnings and ex-dividend events (batch)
    get_journal_by_symbol    — recent journal records for one underlying
    get_position_history     — full lifecycle of one specific position
"""

from anthropic.types import ToolParam
from pydantic import BaseModel

from options_agent.contracts.journal import JournalRecord, OutcomeRecord

# ──────────────────────────────────────────────────────────────────────────────
# Tool name constants — use these everywhere instead of bare strings so a rename
# stays consistent between the schema and any dispatch logic.
# ──────────────────────────────────────────────────────────────────────────────

TOOL_GET_PORTFOLIO_STATE = "get_portfolio_state"
TOOL_GET_UNIVERSE_SNAPSHOT = "get_universe_snapshot"
TOOL_GET_FILTERED_CHAIN = "get_filtered_chain"
TOOL_GET_EVENTS = "get_events"
TOOL_GET_JOURNAL_BY_SYMBOL = "get_journal_by_symbol"
TOOL_GET_POSITION_HISTORY = "get_position_history"

# Maximum number of JournalRecords returned by get_journal_by_symbol.
# WP-2 (state/journal.py query_journal) must enforce the same limit so the
# two sides stay in sync without relying on prose-only documentation.
JOURNAL_MAX_RECORDS = 20


# ──────────────────────────────────────────────────────────────────────────────
# Agent-facing return type for get_position_history.
# Uses WP-0 contract types internally; defined here (not in contracts/) because
# it is an agent-tool concern, not a system-wide contract.
# ──────────────────────────────────────────────────────────────────────────────


class PositionHistory(BaseModel):
    """Full lifecycle of one position returned by get_position_history().

    opening_record is None only when the opening cycle's JournalRecord cannot
    be located — treat this as a system anomaly and log it; do not trade.
    outcome_records is empty for positions with no exit events yet.
    """

    opening_record: JournalRecord | None
    outcome_records: list[OutcomeRecord]


# ──────────────────────────────────────────────────────────────────────────────
# Description strings — extracted as constants so the ToolParam dicts stay
# readable and each line stays within the 88-character project limit.
# ──────────────────────────────────────────────────────────────────────────────

_DESC_GET_PORTFOLIO_STATE = (
    "Return the current portfolio snapshot: all open positions with their"
    " option legs, account buying power, unrealized P&L, intraday realized"
    " P&L, and net portfolio Greeks."
    "\n\n"
    "NET GREEK UNITS — all net_dollar_* fields are in USD:\n"
    "  net_dollar_delta — $ change in portfolio value per $1 move in"
    " the underlying\n"
    "  net_dollar_gamma — $ change in delta per $1 move in the underlying\n"
    "  net_dollar_theta — $ time decay per calendar day across all positions\n"
    "  net_dollar_vega  — $ change per 1 vol-point (1 pct-point) move in IV\n"
    "\n"
    "These are already aggregated across all open positions by"
    " context/portfolio.py. Do not re-derive them from per-position legs;"
    " use the net_dollar_* fields directly for risk-band reasoning.\n"
    "\n"
    "Other notes:\n"
    "  options_buying_power is Alpaca-specific and may differ from"
    " buying_power on margin accounts. The validator uses"
    " options_buying_power for the buying-power gate.\n"
    "  unrealized_pnl and current_mark on each position are cached from the"
    " last reconcile pass — not real-time quotes. Use them for trend"
    " context, not for precise exit-rule calculations (the monitor owns"
    " those).\n"
    "  realized_pnl_today covers intraday closed positions only; lifetime"
    " P&L attribution is in the journal (use get_journal_by_symbol)."
)

_DESC_GET_UNIVERSE_SNAPSHOT = (
    "Return the full configured trading universe: per-symbol market state"
    " plus the market-wide VIX level, regime classification, and macro"
    " event calendar. Always returns the complete universe — it cannot be"
    " filtered by symbol. The universe is small and curated; all symbols"
    " fit in context.\n"
    "\n"
    "CRITICAL None SEMANTICS — these are trading decisions, not missing"
    " data:\n"
    "  iv_rank: null  → insufficient IV history for this symbol"
    " (warm-up period). Do NOT trade this name this cycle. Treat null as"
    " ineligible, not as 'low IV' or 'zero IV'. The warm-up period where"
    " iv_rank is null for all symbols is a normal operating state early"
    " in system life.\n"
    "  iv_percentile: null → same constraint, same rule as iv_rank null.\n"
    "  days_to_earnings: null → no upcoming earnings confirmed within the"
    " lookahead window (typically ~60 calendar days). Absence of a date is"
    " NOT a guarantee of no upcoming earnings; treat it as 'not confirmed'"
    " and apply normal caution. Use get_events for the confirmed flag and"
    " exact date.\n"
    "  days_to_earnings: N (integer) → earnings confirmed N calendar days"
    " from now. Event blackout rules in the validator will reject trades"
    " when N < event_blackout_days (default 5 days). Do not propose trades"
    " you know will be rejected on this rule.\n"
    "  regime: null → regime classifier lacks sufficient data; treat as"
    " unknown.\n"
    "  historical_vol: null → insufficient history; IV/HV comparison"
    " unavailable.\n"
    "\n"
    "market_regime and vix_level are market-wide — not repeated per symbol."
    " macro_events lists market-wide scheduled events (FOMC, CPI, NFP)"
    " within the lookahead window. Per-symbol events (earnings, ex-div)"
    " live in EventInfo (use get_events for those)."
)

_DESC_GET_FILTERED_CHAIN = (
    "Return the pre-filtered options chain for one underlying. This is NOT"
    " the full chain — a significant fraction of contracts have already"
    " been removed before this data reaches you. Specifically, the"
    " following are excluded:\n"
    "  * Strikes outside the configured DTE window and abs-delta range.\n"
    "  * Contracts with missing or malformed bid/ask quotes.\n"
    "  * Contracts with missing Greeks or IV"
    " (counted in excluded_for_missing_greeks).\n"
    "  * Contracts failing the bid-ask spread width liquidity threshold.\n"
    "  * When oi_available=true, contracts below the min open-interest.\n"
    "\n"
    "Metadata to read:\n"
    "  oi_available=false → open-interest data was unavailable from the"
    " provider; the min-OI threshold was NOT enforced. Liquidity screening"
    " is weaker than usual.\n"
    "  truncated=true → a per-right cap was applied to control token"
    " budget; the most relevant contracts by delta proximity were kept.\n"
    "  excluded_for_missing_greeks → count of contracts dropped for"
    " missing data; a high count signals a provider data-quality issue.\n"
    "\n"
    "DELTA SIGN CONVENTION: call deltas are positive (0 to 1); put deltas"
    " are negative (-1 to 0). Use abs(delta) when comparing to the"
    " configured delta range or reasoning about moneyness. OTM puts near"
    " -0.30 are roughly equivalent in moneyness to OTM calls near +0.30.\n"
    "\n"
    "strategy_hint biases the right-side filter — only pass it when you"
    " already know which strategy you are evaluating:\n"
    "  puts only: 'bull_put_spread', 'bear_put_spread', 'cash_secured_put'\n"
    "  calls only: 'bear_call_spread', 'bull_call_spread', 'covered_call'\n"
    "  both rights: 'iron_condor', 'iron_butterfly'\n"
    "  omit strategy_hint to receive both rights with the full delta window."
)

_DESC_GET_EVENTS = (
    "Return upcoming earnings and ex-dividend events for one or more"
    " symbols, within the configured lookahead window (typically ~60"
    " calendar days). Accepts a batch of symbols; returns one EventInfo per"
    " symbol as a dict keyed by ticker. Use this when you need the"
    " confirmed flag or the exact ex-dividend amount — for a quick"
    " days-to-earnings check, SymbolSnapshot.days_to_earnings in"
    " get_universe_snapshot is already populated from the same source.\n"
    "\n"
    "FIELD SEMANTICS:\n"
    "  earnings: null → no earnings confirmed within the lookahead window."
    " This is NOT a guarantee of absence — treat it as 'not confirmed' and"
    " remain cautious. The validator's EVENT_BLACKOUT rule uses this field;"
    " do not assume null means a trade will pass the event gate.\n"
    "  earnings.confirmed=true → earnings date is firm (company-issued)."
    " earnings.confirmed=false → estimated date; less reliable.\n"
    "  earnings.event_date → calendar date of the announcement. If today is"
    " 2026-06-14 and event_date is 2026-06-19, days_to_earnings is 5.\n"
    "  ex_dividend: null → no ex-dividend date within the window. A"
    " non-null ex-dividend within the DTE of a short call creates"
    " early-assignment risk — factor this into strategy selection.\n"
    "\n"
    "Note: FOMC, CPI, NFP, and other market-wide macro events are on"
    " UniverseSnapshot.macro_events (get_universe_snapshot), not here."
    " This tool covers per-symbol corporate events only.\n"
    "\n"
    "WP-3.5 alignment note: this tool accepts a batch of symbols and"
    " returns dict[str, EventInfo]. WP-3.5 must expose a batch"
    " implementation matching this signature. If WP-3.5 only implements"
    " a single-symbol get_events(symbol), the dispatch layer in"
    " reasoner.py must fan out and reassemble into the expected dict."
)

_DESC_GET_JOURNAL_BY_SYMBOL = (
    "Return recent journal records for one underlying, ordered"
    " oldest-first. Use this to review past decisions, rationales, IV"
    " conditions, and outcomes for a ticker before proposing a new trade"
    " on it. Returns at most 20 records. Returns an empty list when the"
    " journal is empty (expected when the system is new or this symbol"
    " has no history yet).\n"
    "\n"
    "KEY FIELDS PER RECORD:\n"
    "  action_taken — what happened: OPENED, REJECTED, NO_ACTION_AGENT,"
    " NO_ACTION_GATED, SIZED_TO_ZERO, EXECUTION_FAILED\n"
    "  decision.proposal — the full TradeProposal evaluated"
    " (null for NO_ACTION_GATED)\n"
    "  decision.validation_result — why a proposal was rejected, if it"
    " was; check reasons[].rule_id for the specific rule that fired\n"
    "  iv_rank_at_open — IV rank at the time of the opening cycle (null"
    " for non-OPENED cycles); useful for comparing current IV rank to"
    " entry IV\n"
    "  earnings_within_dte — whether confirmed earnings fell within the"
    " proposed trade's DTE window; correlate against outcomes to calibrate"
    " event-gate policy\n"
    "  conviction — the agent's stated conviction at proposal time;\n"
    " compare against actual P&L to calibrate conviction over time\n"
    "\n"
    "The cycle_id field is the stable identifier for a record. Reference"
    " cycle_ids in TradeProposal.informed_by to document which past"
    " decisions shaped your current proposal."
)

_DESC_GET_POSITION_HISTORY = (
    "Return the full lifecycle of one specific position: the opening"
    " journal record plus all outcome events (partial closes, full close,"
    " expiry, assignment). Use this when you need to understand how a"
    " specific open position has evolved — for example, to assess whether"
    " to roll or close based on its original thesis and how much of the"
    " planned profit target has been realised.\n"
    "\n"
    "RETURN STRUCTURE:\n"
    "  opening_record — the JournalRecord from the cycle that opened this"
    " position. Contains the original proposal (strategy, legs, thesis,"
    " iv_rationale, catalyst_check), the IV rank at entry, and the sizing"
    " decision. Null only if the opening record cannot be located — treat"
    " as a system anomaly.\n"
    "  outcome_records — all OutcomeRecord events for this position,"
    " ordered by recorded_at. Empty if no exit events have occurred yet."
    " Each OutcomeRecord carries: event_type (PARTIAL_CLOSE, FULL_CLOSE,"
    " ROLL, EXPIRED, ASSIGNED), realized_pnl, contracts_closed,"
    " and fill_price.\n"
    "\n"
    "Returns null if position_id is not found. Find position IDs via"
    " get_portfolio_state — each open position has an 'id' field."
)

# ──────────────────────────────────────────────────────────────────────────────
# Tool definitions
# ──────────────────────────────────────────────────────────────────────────────

GET_PORTFOLIO_STATE: ToolParam = {
    "name": TOOL_GET_PORTFOLIO_STATE,
    "description": _DESC_GET_PORTFOLIO_STATE,
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

GET_UNIVERSE_SNAPSHOT: ToolParam = {
    "name": TOOL_GET_UNIVERSE_SNAPSHOT,
    "description": _DESC_GET_UNIVERSE_SNAPSHOT,
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

GET_FILTERED_CHAIN: ToolParam = {
    "name": TOOL_GET_FILTERED_CHAIN,
    "description": _DESC_GET_FILTERED_CHAIN,
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": ("Underlying ticker symbol (e.g. 'SPY', 'AAPL')."),
            },
            "strategy_hint": {
                "type": "string",
                "description": (
                    "Optional. Strategy name from the allowed playbook —"
                    " biases which option rights (calls, puts, or both) are"
                    " included in the returned chain. Only provide when you"
                    " have already chosen a strategy direction. Unknown"
                    " values fall back to both rights with a logged warning."
                ),
                "enum": [
                    "bull_put_spread",
                    "bear_put_spread",
                    "cash_secured_put",
                    "bear_call_spread",
                    "bull_call_spread",
                    "covered_call",
                    "iron_condor",
                    "iron_butterfly",
                ],
            },
        },
        "required": ["symbol"],
    },
}

GET_EVENTS: ToolParam = {
    "name": TOOL_GET_EVENTS,
    "description": _DESC_GET_EVENTS,
    "input_schema": {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of underlying ticker symbols to look up"
                    " (e.g. ['SPY', 'AAPL', 'TSLA']). Returns one EventInfo"
                    " per symbol, keyed by ticker."
                ),
                "minItems": 1,
            },
        },
        "required": ["symbols"],
    },
}

GET_JOURNAL_BY_SYMBOL: ToolParam = {
    "name": TOOL_GET_JOURNAL_BY_SYMBOL,
    "description": _DESC_GET_JOURNAL_BY_SYMBOL,
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": (
                    "Underlying ticker symbol to retrieve journal history"
                    " for (e.g. 'SPY')."
                ),
            },
        },
        "required": ["symbol"],
    },
}

GET_POSITION_HISTORY: ToolParam = {
    "name": TOOL_GET_POSITION_HISTORY,
    "description": _DESC_GET_POSITION_HISTORY,
    "input_schema": {
        "type": "object",
        "properties": {
            "position_id": {
                "type": "string",
                "description": (
                    "The position ID to retrieve history for. Find position"
                    " IDs via get_portfolio_state under each position's"
                    " 'id' field."
                ),
            },
        },
        "required": ["position_id"],
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# Exported tool list
# ──────────────────────────────────────────────────────────────────────────────

# The complete read-only tool set passed to the Anthropic SDK at each agent
# call. Order is deliberate: stable, no-argument tools first so the tool-list
# prefix is cache-friendly (least-likely-to-change portion at the front).
AGENT_TOOLS: list[ToolParam] = [
    GET_PORTFOLIO_STATE,
    GET_UNIVERSE_SNAPSHOT,
    GET_FILTERED_CHAIN,
    GET_EVENTS,
    GET_JOURNAL_BY_SYMBOL,
    GET_POSITION_HISTORY,
]

# Frozen set of all tool names — used by tests and by reasoner.py to validate
# that a model-returned tool call refers to a known, read-only tool.
AGENT_TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in AGENT_TOOLS)
