"""Mock implementations of the agent's read-only tools (WP-6.1).

IMPORTABLE BUT NOT FOR PRODUCTION USE.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
These mocks are importable by non-test code so that WP-6.4 can run
reasoner.py end-to-end without live WP-3 data. They must never reach a
production code path.

Production guard: reasoner.py receives tool implementations by dependency
injection — callers must pass the impl map explicitly. There is no default
that falls back to mocks. Never import MOCK_TOOL_IMPLS in production code;
it will be caught in code review and blocked by this module's guard.

If you are adding mocks as a "temporary fallback until WP-3 is wired up,"
stop: that is precisely the silent-production-data-fabrication failure mode
this guard exists to prevent. Wire in real implementations or run explicitly
against this harness; do not sneak mocks in via a default argument.

Mock universe (three representative data states):
    SPY  — clean, tradeable: good iv_rank, no upcoming earnings, moderate IV
    AAPL — upcoming earnings in 5 calendar days (within typical blackout)
    NVDA — warm-up period: iv_rank=None, iv_percentile=None (ineligible)

These three states exercise the distribution the real agent will see: a
normal tradeable name, an event-proximity rejection, and the warm-up period
where IV history is insufficient. WP-6.4 must develop and test against all
three, not only the happy-path SPY scenario.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from options_agent.agent.tools import (
    JOURNAL_MAX_RECORDS,
    TOOL_GET_EVENTS,
    TOOL_GET_FILTERED_CHAIN,
    TOOL_GET_JOURNAL_BY_SYMBOL,
    TOOL_GET_PORTFOLIO_STATE,
    TOOL_GET_POSITION_HISTORY,
    TOOL_GET_UNIVERSE_SNAPSHOT,
    PositionHistory,
)
from options_agent.contracts.data import (
    ChainFilterParams,
    EarningsEvent,
    EventInfo,
    FilteredChain,
    MacroEvent,
    OptionContract,
    PortfolioState,
    SymbolSnapshot,
    UniverseSnapshot,
)
from options_agent.contracts.journal import JournalRecord
from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.results import (
    SizingConstraint,
    SizingResult,
    ValidationResult,
)
from options_agent.contracts.state import (
    ActionTaken,
    AssetClass,
    ContextSnapshot,
    Decision,
    LegStatus,
    Position,
    PositionLeg,
    PositionStatus,
)

# ──────────────────────────────────────────────────────────────────────────────
# Reference timestamps — fixed so all mock data is deterministic
# ──────────────────────────────────────────────────────────────────────────────

_AS_OF = datetime(2026, 6, 14, 14, 30, 0, tzinfo=UTC)
_EXPIRY_NEAR = date(2026, 7, 18)  # ~34 DTE from _AS_OF
_EXPIRY_FAR = date(2026, 8, 21)  # ~68 DTE from _AS_OF

# ──────────────────────────────────────────────────────────────────────────────
# Mock portfolio state
# ──────────────────────────────────────────────────────────────────────────────

_MOCK_POSITION = Position(
    id="pos-001",
    underlying="SPY",
    strategy="bull_put_spread",
    legs=[
        PositionLeg(
            leg=Leg(right="put", side="sell", strike=530.0, expiration=_EXPIRY_NEAR),
            filled_qty=2,
            avg_fill_price=2.45,
            status=LegStatus.OPEN,
        ),
        PositionLeg(
            leg=Leg(right="put", side="buy", strike=525.0, expiration=_EXPIRY_NEAR),
            filled_qty=2,
            avg_fill_price=1.10,
            status=LegStatus.OPEN,
        ),
    ],
    quantity=2,
    # P&L arithmetic (all in "option price × contracts" units, pre-×100):
    #   entry_net_amount = -(2.45 - 1.10) × 2 contracts = -2.70 (credit)
    #   current_mark     = -1.35 for 2 contracts (halfway to worthless)
    #   unrealized_pnl   = (|entry| - |current|) × 100
    #                    = (2.70 - 1.35) × 100 = $135.0
    # This places the position exactly at the 50% profit target
    # (135 / 270 = 50%), which is a useful WP-6.4 test state.
    entry_net_amount=-2.70,
    current_mark=-1.35,
    marked_at=_AS_OF,
    unrealized_pnl=135.0,
    realized_pnl=None,
    exit_plan=ExitPlan(profit_target_pct=0.50, stop_loss_mult=2.0, time_stop_dte=21),
    status=PositionStatus.OPEN,
    opened_at=datetime(2026, 6, 7, 15, 0, 0, tzinfo=UTC),
    closed_at=None,
    nearest_expiration=_EXPIRY_NEAR,
    est_max_loss=500.0,
    est_max_profit=270.0,
    opening_order_id="ord-001",
    asset_class=AssetClass.OPTION_STRATEGY,
)

_MOCK_PORTFOLIO_STATE = PortfolioState(
    positions=[_MOCK_POSITION],
    account_equity=50000.0,
    buying_power=38000.0,
    options_buying_power=35000.0,
    unrealized_pnl=270.0,
    realized_pnl_today=0.0,
    approval_level=3,
    net_dollar_delta=42.0,
    net_dollar_gamma=12.0,
    net_dollar_theta=18.0,
    net_dollar_vega=-85.0,
)


def _get_portfolio_state(_tool_input: dict[str, Any]) -> PortfolioState:
    return _MOCK_PORTFOLIO_STATE


# ──────────────────────────────────────────────────────────────────────────────
# Mock universe snapshot — three representative data states
# ──────────────────────────────────────────────────────────────────────────────

_MOCK_UNIVERSE_SNAPSHOT = UniverseSnapshot(
    symbol_snapshots={
        # Clean, tradeable name — good IV rank, no upcoming earnings.
        "SPY": SymbolSnapshot(
            symbol="SPY",
            price=545.20,
            iv_rank=62.0,
            iv_percentile=65.0,
            historical_vol=0.14,
            regime="neutral",
            days_to_earnings=None,
        ),
        # Earnings approaching — 5 days out, within the typical blackout
        # window. The validator will reject an OPEN proposal on AAPL with
        # EVENT_BLACKOUT. This state exercises the agent's ability to
        # identify and skip event-adjacent names.
        "AAPL": SymbolSnapshot(
            symbol="AAPL",
            price=212.80,
            iv_rank=71.0,
            iv_percentile=74.0,
            historical_vol=0.22,
            regime="bullish",
            days_to_earnings=5,
        ),
        # Warm-up period: iv_rank and iv_percentile are None — insufficient
        # IV history. The agent must treat NVDA as ineligible this cycle.
        # This is NOT low IV; it is an explicit signal that IV-rank-based
        # strategy selection is impossible for this name right now.
        "NVDA": SymbolSnapshot(
            symbol="NVDA",
            price=131.50,
            iv_rank=None,
            iv_percentile=None,
            historical_vol=None,
            regime=None,
            days_to_earnings=None,
        ),
    },
    vix_level=16.8,
    market_regime="neutral",
    macro_events=[
        MacroEvent(
            name="FOMC Rate Decision",
            event_date=date(2026, 6, 18),
            event_type="FOMC",
        )
    ],
    as_of=_AS_OF,
)


def _get_universe_snapshot(_tool_input: dict[str, Any]) -> UniverseSnapshot:
    return _MOCK_UNIVERSE_SNAPSHOT


# ──────────────────────────────────────────────────────────────────────────────
# Mock filtered chain
# ──────────────────────────────────────────────────────────────────────────────

_SPY_CHAIN_CONTRACTS = [
    OptionContract(
        symbol="SPY260718P00535000",
        strike=535.0,
        expiration=_EXPIRY_NEAR,
        right="put",
        bid=3.40,
        ask=3.60,
        mid=3.50,
        volume=2150,
        open_interest=12400,
        delta=-0.28,
        theta=-0.22,
        vega=0.45,
        iv=0.182,
        spread_width=0.20,
        dte=34,
        greek_source="alpaca",
    ),
    OptionContract(
        symbol="SPY260718P00530000",
        strike=530.0,
        expiration=_EXPIRY_NEAR,
        right="put",
        bid=2.35,
        ask=2.55,
        mid=2.45,
        volume=3200,
        open_interest=18700,
        delta=-0.21,
        theta=-0.18,
        vega=0.38,
        iv=0.175,
        spread_width=0.20,
        dte=34,
        greek_source="alpaca",
    ),
    OptionContract(
        symbol="SPY260718P00525000",
        strike=525.0,
        expiration=_EXPIRY_NEAR,
        right="put",
        bid=1.05,
        ask=1.20,
        mid=1.125,
        volume=4100,
        open_interest=22300,
        delta=-0.15,
        theta=-0.13,
        vega=0.28,
        iv=0.168,
        spread_width=0.15,
        dte=34,
        greek_source="alpaca",
    ),
    OptionContract(
        symbol="SPY260718C00555000",
        strike=555.0,
        expiration=_EXPIRY_NEAR,
        right="call",
        bid=2.80,
        ask=3.00,
        mid=2.90,
        volume=1850,
        open_interest=9800,
        delta=0.25,
        theta=-0.20,
        vega=0.42,
        iv=0.172,
        spread_width=0.20,
        dte=34,
        greek_source="alpaca",
    ),
    OptionContract(
        symbol="SPY260718C00560000",
        strike=560.0,
        expiration=_EXPIRY_NEAR,
        right="call",
        bid=1.55,
        ask=1.70,
        mid=1.625,
        volume=2400,
        open_interest=14100,
        delta=0.18,
        theta=-0.15,
        vega=0.33,
        iv=0.166,
        spread_width=0.15,
        dte=34,
        greek_source="alpaca",
    ),
]

_SPY_FILTER_PARAMS = ChainFilterParams(
    dte_min=21,
    dte_max=60,
    delta_min=0.15,
    delta_max=0.40,
    min_open_interest=100,
    max_spread_pct_of_mid=0.15,
    max_spread_abs_floor=0.30,
)

_PUT_ONLY_HINTS = {"bull_put_spread", "bear_put_spread", "cash_secured_put"}
_CALL_ONLY_HINTS = {"bear_call_spread", "bull_call_spread", "covered_call"}


def _get_filtered_chain(tool_input: dict[str, Any]) -> FilteredChain:
    symbol: str = tool_input["symbol"]
    strategy_hint: str | None = tool_input.get("strategy_hint")

    if strategy_hint in _PUT_ONLY_HINTS:
        contracts = [c for c in _SPY_CHAIN_CONTRACTS if c.right == "put"]
    elif strategy_hint in _CALL_ONLY_HINTS:
        contracts = [c for c in _SPY_CHAIN_CONTRACTS if c.right == "call"]
    else:
        contracts = list(_SPY_CHAIN_CONTRACTS)

    # Returns SPY-shaped data regardless of symbol; the mock represents any
    # underlying. WP-3 wires in real per-symbol fetches when integrated.
    return FilteredChain(
        underlying=symbol,
        underlying_price=545.20,
        as_of=_AS_OF,
        filter_params=_SPY_FILTER_PARAMS,
        contracts=contracts,
        strategy_hint=strategy_hint,
        oi_available=True,
        excluded_for_missing_greeks=0,
        truncated=False,
        total_before_cap=len(contracts),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Mock events
# ──────────────────────────────────────────────────────────────────────────────

_MOCK_EVENTS: dict[str, EventInfo] = {
    "SPY": EventInfo(symbol="SPY", earnings=None, ex_dividend=None),
    "AAPL": EventInfo(
        symbol="AAPL",
        earnings=EarningsEvent(event_date=date(2026, 6, 19), confirmed=True),
        ex_dividend=None,
    ),
    "NVDA": EventInfo(symbol="NVDA", earnings=None, ex_dividend=None),
}


def _get_events(tool_input: dict[str, Any]) -> dict[str, EventInfo]:
    symbols: list[str] = tool_input["symbols"]
    return {
        sym: _MOCK_EVENTS.get(
            sym, EventInfo(symbol=sym, earnings=None, ex_dividend=None)
        )
        for sym in symbols
    }


# ──────────────────────────────────────────────────────────────────────────────
# Mock journal records
# ──────────────────────────────────────────────────────────────────────────────


def _make_context_snapshot(assembled: dict[str, Any]) -> ContextSnapshot:
    blob = json.dumps(assembled, sort_keys=True)
    context_hash = hashlib.sha256(blob.encode()).hexdigest()[:16]
    return ContextSnapshot(
        assembled_context=assembled,
        context_hash=context_hash,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        assembled_at=_AS_OF,
    )


_STUB_PROPOSAL = TradeProposal(
    action="OPEN",
    underlying="SPY",
    strategy="bull_put_spread",
    legs=[
        Leg(right="put", side="sell", strike=530.0, expiration=_EXPIRY_NEAR),
        Leg(right="put", side="buy", strike=525.0, expiration=_EXPIRY_NEAR),
    ],
    thesis=(
        "SPY near 545 with neutral market regime and VIX at 16.8. "
        "The 530/525 put spread collects credit below a well-supported level."
    ),
    iv_rationale=(
        "IV rank at 62 — in the upper half of the trailing year range."
        " Selling premium at elevated-but-not-extreme IV captures the vol"
        " risk premium without chasing IV at peak. A defined-risk credit"
        " spread suits this regime."
    ),
    catalyst_check=(
        "SPY tracks the S&P 500 index; no single-company earnings catalyst."
        " FOMC rate decision on 2026-06-18 falls within the DTE window —"
        " sized conservatively. No other macro events within 5 calendar days."
    ),
    conviction=0.65,
    est_max_loss=500.0,
    est_max_profit=270.0,
    breakevens=[527.30],
    net_delta=0.13,
    net_theta=9.0,
    net_vega=-0.38,
    exit_plan=ExitPlan(profit_target_pct=0.50, stop_loss_mult=2.0, time_stop_dte=21),
    informed_by=[],
)

_OPENED_JOURNAL_RECORD = JournalRecord(
    cycle_id="cycle-20260607-001",
    timestamp=datetime(2026, 6, 7, 15, 0, 0, tzinfo=UTC),
    action_taken=ActionTaken.OPENED,
    decision=Decision(
        proposal=_STUB_PROPOSAL,
        validation_result=ValidationResult(passed=True),
        sizing_result=SizingResult(
            contracts=2,
            sized_max_loss=500.0,
            sized_max_profit=270.0,
            risk_budget_used=0.01,
            binding_constraint=SizingConstraint.RISK_BUDGET,
        ),
        action_taken=ActionTaken.OPENED,
    ),
    context_snapshot=_make_context_snapshot(
        {"underlying": "SPY", "cycle_id": "cycle-20260607-001"}
    ),
    position_ids=["pos-001"],
    order_ids=["ord-001"],
    strategy=_STUB_PROPOSAL.strategy,
    underlying="SPY",
    net_delta_at_open=_STUB_PROPOSAL.net_delta,
    earnings_within_dte=False,
    conviction=_STUB_PROPOSAL.conviction,
    iv_rank_at_open=62.0,
    limits_version="1.0",
    prompt_version="0.1",
    model_id="claude-sonnet-4-6",
)

_MOCK_JOURNAL: dict[str, list[JournalRecord]] = {
    "SPY": [_OPENED_JOURNAL_RECORD],
    "AAPL": [],
    "NVDA": [],
}


def _get_journal_by_symbol(tool_input: dict[str, Any]) -> list[JournalRecord]:
    symbol: str = tool_input["symbol"]
    records = _MOCK_JOURNAL.get(symbol, [])
    return records[-JOURNAL_MAX_RECORDS:]


# ──────────────────────────────────────────────────────────────────────────────
# Mock position history
# ──────────────────────────────────────────────────────────────────────────────

_MOCK_POSITION_HISTORIES: dict[str, PositionHistory] = {
    "pos-001": PositionHistory(
        opening_record=_OPENED_JOURNAL_RECORD,
        outcome_records=[],  # position still open; no exit events yet
    ),
}


def _get_position_history(tool_input: dict[str, Any]) -> PositionHistory | None:
    position_id: str = tool_input["position_id"]
    return _MOCK_POSITION_HISTORIES.get(position_id)


# ──────────────────────────────────────────────────────────────────────────────
# Exported mock tool implementation map
# ──────────────────────────────────────────────────────────────────────────────

# Type alias for a tool implementation callable.
ToolImpl = Callable[[dict[str, Any]], Any]

# Maps each tool name to its mock callable. Pass this map to the reasoner
# harness via dependency injection — never import it from production code.
# See module docstring for the production guard.
MOCK_TOOL_IMPLS: dict[str, ToolImpl] = {
    TOOL_GET_PORTFOLIO_STATE: _get_portfolio_state,
    TOOL_GET_UNIVERSE_SNAPSHOT: _get_universe_snapshot,
    TOOL_GET_FILTERED_CHAIN: _get_filtered_chain,
    TOOL_GET_EVENTS: _get_events,
    TOOL_GET_JOURNAL_BY_SYMBOL: _get_journal_by_symbol,
    TOOL_GET_POSITION_HISTORY: _get_position_history,
}
