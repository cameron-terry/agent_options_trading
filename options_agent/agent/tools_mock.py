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
    TOOL_GET_HELD_LEG_GREEKS,
    TOOL_GET_JOURNAL_BY_SYMBOL,
    TOOL_GET_OUTCOME_STATS,
    TOOL_GET_PORTFOLIO_STATE,
    TOOL_GET_POSITION_HISTORY,
    TOOL_GET_PRICE_HISTORY,
    TOOL_GET_UNIVERSE_SNAPSHOT,
    PositionHistory,
)
from options_agent.contracts.data import (
    ChainFilterParams,
    EarningsEvent,
    EventInfo,
    FilteredChain,
    MacroEvent,
    MarketRegime,
    OptionContract,
    PortfolioState,
    PriceHistorySummary,
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
    exit_plan=ExitPlan(
        profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
    ),
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
    unrealized_pnl=135.0,
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
            regime=MarketRegime.NORMAL,
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
            regime=MarketRegime.NORMAL,
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
    market_regime=MarketRegime.NORMAL,
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
    # September quarterly expiry — matches the stub_reasoner proposal
    # (sell 560P / buy 555P exp 2026-09-18) so the enrich+validate entry-cycle
    # path can price and liquidity-check the stub's legs against this chain.
    # Mids are chosen so the recomputed metrics equal the stub's stated
    # est_max_loss=350 / est_max_profit=150 (credit = 12.00 − 10.50 = 1.50).
    OptionContract(
        symbol="SPY260918P00560000",
        strike=560.0,
        expiration=date(2026, 9, 18),
        right="put",
        bid=11.95,
        ask=12.05,
        mid=12.00,
        volume=1800,
        open_interest=8600,
        # −0.60 keeps the stub structure's net delta at +0.05 so the happy
        # path stays inside the 20%-of-equity delta band on top of the mock
        # portfolio's existing exposure.
        delta=-0.60,
        theta=-0.09,
        vega=0.52,
        iv=0.19,
        spread_width=0.10,
        dte=96,
        greek_source="alpaca",
    ),
    OptionContract(
        symbol="SPY260918P00555000",
        strike=555.0,
        expiration=date(2026, 9, 18),
        right="put",
        bid=10.45,
        ask=10.55,
        mid=10.50,
        volume=1500,
        open_interest=7200,
        delta=-0.55,
        theta=-0.085,
        vega=0.50,
        iv=0.188,
        spread_width=0.10,
        dte=96,
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
    exit_plan=ExitPlan(
        profit_target_pct=0.50, stop_loss_max_loss_fraction=0.5, time_stop_dte=21
    ),
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


_MOCK_PRICE_HISTORIES: dict[str, PriceHistorySummary] = {
    # SPY: steady uptrend near the highs — consistent with the bullish stub.
    "SPY": PriceHistorySummary(
        symbol="SPY",
        as_of=_AS_OF,
        price=545.20,
        sma_20=538.40,
        sma_50=530.10,
        price_vs_sma_20_pct=1.26,
        price_vs_sma_50_pct=2.85,
        high_52w=548.90,
        low_52w=442.30,
        pct_from_52w_high=-0.67,
        pct_from_52w_low=23.26,
        atr_14=4.85,
        atr_14_pct=0.89,
        return_5d_pct=0.8,
        return_21d_pct=2.4,
        return_63d_pct=6.1,
        recent_closes=[
            538.1,
            539.4,
            541.2,
            540.8,
            542.5,
            543.1,
            542.9,
            544.0,
            544.7,
            545.2,
        ],
        bars_available=252,
    ),
    # AAPL: pullback below the 20-day into earnings — trend caution state.
    "AAPL": PriceHistorySummary(
        symbol="AAPL",
        as_of=_AS_OF,
        price=212.80,
        sma_20=216.50,
        sma_50=210.20,
        price_vs_sma_20_pct=-1.71,
        price_vs_sma_50_pct=1.24,
        high_52w=237.50,
        low_52w=164.10,
        pct_from_52w_high=-10.4,
        pct_from_52w_low=29.68,
        atr_14=4.10,
        atr_14_pct=1.93,
        return_5d_pct=-2.1,
        return_21d_pct=-0.6,
        return_63d_pct=8.3,
        recent_closes=[
            218.2,
            217.5,
            216.9,
            215.3,
            214.8,
            215.6,
            214.2,
            213.5,
            213.1,
            212.8,
        ],
        bars_available=252,
    ),
    # NVDA: short listing history — most indicators unavailable, mirroring the
    # warm-up state its IV rank is in.
    "NVDA": PriceHistorySummary(
        symbol="NVDA",
        as_of=_AS_OF,
        price=131.50,
        sma_20=128.70,
        sma_50=None,
        price_vs_sma_20_pct=2.18,
        price_vs_sma_50_pct=None,
        high_52w=135.20,
        low_52w=118.60,
        pct_from_52w_high=-2.74,
        pct_from_52w_low=10.88,
        atr_14=3.95,
        atr_14_pct=3.0,
        return_5d_pct=1.9,
        return_21d_pct=None,
        return_63d_pct=None,
        recent_closes=[
            127.4,
            128.1,
            129.6,
            128.8,
            130.2,
            129.5,
            130.8,
            131.1,
            130.6,
            131.5,
        ],
        bars_available=28,
    ),
}


def _get_price_history(tool_input: dict[str, Any]) -> PriceHistorySummary | None:
    symbol: str = tool_input["symbol"]
    return _MOCK_PRICE_HISTORIES.get(symbol)


def _get_outcome_stats(_tool_input: dict[str, Any]) -> dict[str, Any]:
    # Internal assembler key: the mock system has no closed positions yet, so
    # the track record is empty — matching a fresh paper account.
    return {}


def _get_held_leg_greeks(_tool_input: dict[str, Any]) -> dict[Any, Any]:
    # Internal assembler key: the mock chain covers all mock position legs,
    # so no held-leg fallback data is needed — an empty dict means "no extra
    # coverage" and aggregation falls back to the chain lookup.
    return {}


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
    TOOL_GET_PRICE_HISTORY: _get_price_history,
    TOOL_GET_HELD_LEG_GREEKS: _get_held_leg_greeks,
    TOOL_GET_OUTCOME_STATS: _get_outcome_stats,
}


# ══════════════════════════════════════════════════════════════════════════════
# WP-6.5 EVAL SCENARIO EXTENSIONS
#
# Scenario-specific tool implementation maps for the prompt eval harness.
# These share the same data vocabulary as MOCK_TOOL_IMPLS (same types,
# reference timestamps, filter params) but represent distinct market states.
# eval_scenarios.py composes these into EvalScenario objects.
#
# Naming: make_<scenario>_tool_impls() → dict[str, ToolImpl]
# ══════════════════════════════════════════════════════════════════════════════

# ──────────────────────────────────────────────────────────────────────────────
# Shared flat portfolio (no open positions) used by scenarios B, C, D
# ──────────────────────────────────────────────────────────────────────────────

_FLAT_PORTFOLIO_STATE = PortfolioState(
    positions=[],
    account_equity=50000.0,
    buying_power=50000.0,
    options_buying_power=47500.0,
    unrealized_pnl=0.0,
    realized_pnl_today=0.0,
    approval_level=3,
    net_dollar_delta=0.0,
    net_dollar_gamma=0.0,
    net_dollar_theta=0.0,
    net_dollar_vega=0.0,
)


def _get_flat_portfolio(_tool_input: dict[str, Any]) -> PortfolioState:
    return _FLAT_PORTFOLIO_STATE


# ──────────────────────────────────────────────────────────────────────────────
# Scenario B — QQQ low-IV bullish
# Universe: QQQ only, iv_rank=12 (12th percentile → low band < 25th threshold)
# Regime: bullish; VIX=12.5 (low_vol < 15 threshold); no events
# Portfolio: flat
# Expected: strategy in low_iv_strategies (bull_call_spread or bear_put_spread)
# ──────────────────────────────────────────────────────────────────────────────

_QQQ_EXPIRY = date(2026, 7, 18)  # same ~34 DTE window as the base mock

_QQQ_CHAIN_CONTRACTS = [
    # Call side — primary for bull_call_spread in a bullish + low-IV regime
    OptionContract(
        symbol="QQQ260718C00482500",
        strike=482.50,
        expiration=_QQQ_EXPIRY,
        right="call",
        bid=4.20,
        ask=4.40,
        mid=4.30,
        volume=1800,
        open_interest=9200,
        delta=0.38,
        theta=-0.18,
        vega=0.52,
        iv=0.108,
        spread_width=0.20,
        dte=34,
        greek_source="alpaca",
    ),
    OptionContract(
        symbol="QQQ260718C00487500",
        strike=487.50,
        expiration=_QQQ_EXPIRY,
        right="call",
        bid=2.10,
        ask=2.25,
        mid=2.175,
        volume=2400,
        open_interest=11000,
        delta=0.28,
        theta=-0.14,
        vega=0.43,
        iv=0.102,
        spread_width=0.15,
        dte=34,
        greek_source="alpaca",
    ),
    OptionContract(
        symbol="QQQ260718C00492500",
        strike=492.50,
        expiration=_QQQ_EXPIRY,
        right="call",
        bid=0.85,
        ask=0.95,
        mid=0.90,
        volume=3100,
        open_interest=13400,
        delta=0.18,
        theta=-0.09,
        vega=0.31,
        iv=0.096,
        spread_width=0.10,
        dte=34,
        greek_source="alpaca",
    ),
    # Put side — available for bear_put_spread (also in low_iv_strategies)
    OptionContract(
        symbol="QQQ260718P00475000",
        strike=475.00,
        expiration=_QQQ_EXPIRY,
        right="put",
        bid=2.05,
        ask=2.20,
        mid=2.125,
        volume=1600,
        open_interest=8100,
        delta=-0.25,
        theta=-0.12,
        vega=0.38,
        iv=0.104,
        spread_width=0.15,
        dte=34,
        greek_source="alpaca",
    ),
    OptionContract(
        symbol="QQQ260718P00470000",
        strike=470.00,
        expiration=_QQQ_EXPIRY,
        right="put",
        bid=0.80,
        ask=0.90,
        mid=0.85,
        volume=2200,
        open_interest=10500,
        delta=-0.15,
        theta=-0.08,
        vega=0.25,
        iv=0.097,
        spread_width=0.10,
        dte=34,
        greek_source="alpaca",
    ),
]

_LOW_IV_BULLISH_UNIVERSE = UniverseSnapshot(
    symbol_snapshots={
        "QQQ": SymbolSnapshot(
            symbol="QQQ",
            price=480.50,
            iv_rank=12.0,  # 12th percentile → low band (< 25th threshold)
            iv_percentile=10.0,
            historical_vol=0.11,
            regime=MarketRegime.LOW_VOL,
            days_to_earnings=None,
        ),
    },
    vix_level=12.5,  # low_vol regime (< 15.0 threshold)
    market_regime=MarketRegime.LOW_VOL,
    macro_events=[],
    as_of=_AS_OF,
)


def _get_low_iv_universe(_tool_input: dict[str, Any]) -> UniverseSnapshot:
    return _LOW_IV_BULLISH_UNIVERSE


def _get_qqq_filtered_chain(tool_input: dict[str, Any]) -> FilteredChain:
    strategy_hint: str | None = tool_input.get("strategy_hint")
    if strategy_hint in _CALL_ONLY_HINTS:
        contracts = [c for c in _QQQ_CHAIN_CONTRACTS if c.right == "call"]
    elif strategy_hint in _PUT_ONLY_HINTS:
        contracts = [c for c in _QQQ_CHAIN_CONTRACTS if c.right == "put"]
    else:
        contracts = list(_QQQ_CHAIN_CONTRACTS)
    return FilteredChain(
        underlying=tool_input.get("symbol", "QQQ"),
        underlying_price=480.50,
        as_of=_AS_OF,
        filter_params=_SPY_FILTER_PARAMS,
        contracts=contracts,
        strategy_hint=strategy_hint,
        oi_available=True,
        excluded_for_missing_greeks=0,
        truncated=False,
        total_before_cap=len(contracts),
    )


def _get_qqq_events(tool_input: dict[str, Any]) -> dict[str, EventInfo]:
    return {
        sym: EventInfo(symbol=sym, earnings=None, ex_dividend=None)
        for sym in tool_input["symbols"]
    }


def _get_empty_journal(_tool_input: dict[str, Any]) -> list[JournalRecord]:
    return []


def _get_no_position_history(_tool_input: dict[str, Any]) -> PositionHistory | None:
    return None


def make_low_iv_bullish_tool_impls() -> dict[str, ToolImpl]:
    """Scenario B: QQQ-only low-IV bullish universe, flat portfolio."""
    return {
        TOOL_GET_PORTFOLIO_STATE: _get_flat_portfolio,
        TOOL_GET_UNIVERSE_SNAPSHOT: _get_low_iv_universe,
        TOOL_GET_FILTERED_CHAIN: _get_qqq_filtered_chain,
        TOOL_GET_EVENTS: _get_qqq_events,
        TOOL_GET_JOURNAL_BY_SYMBOL: _get_empty_journal,
        TOOL_GET_POSITION_HISTORY: _get_no_position_history,
        TOOL_GET_PRICE_HISTORY: _get_price_history,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Scenario C — AAPL earnings blackout
# Universe: AAPL only, iv_rank=71 (high band), confirmed earnings in 5 days
# blackout_days default=5 → within blackout; agent must decline
# Portfolio: flat
# Expected: action=NO_ACTION (proactive decline before validator rejects)
# ──────────────────────────────────────────────────────────────────────────────

_AAPL_ONLY_UNIVERSE = UniverseSnapshot(
    symbol_snapshots={
        "AAPL": _MOCK_UNIVERSE_SNAPSHOT.symbol_snapshots["AAPL"],
    },
    vix_level=_MOCK_UNIVERSE_SNAPSHOT.vix_level,
    market_regime=_MOCK_UNIVERSE_SNAPSHOT.market_regime,
    macro_events=[],
    as_of=_AS_OF,
)


def _get_aapl_only_universe(_tool_input: dict[str, Any]) -> UniverseSnapshot:
    return _AAPL_ONLY_UNIVERSE


def _get_aapl_events(tool_input: dict[str, Any]) -> dict[str, EventInfo]:
    return {
        sym: _MOCK_EVENTS.get(
            sym, EventInfo(symbol=sym, earnings=None, ex_dividend=None)
        )
        for sym in tool_input["symbols"]
    }


def make_earnings_blackout_tool_impls() -> dict[str, ToolImpl]:
    """Scenario C: AAPL-only universe with earnings 5 days out, flat portfolio."""
    return {
        TOOL_GET_PORTFOLIO_STATE: _get_flat_portfolio,
        TOOL_GET_UNIVERSE_SNAPSHOT: _get_aapl_only_universe,
        TOOL_GET_FILTERED_CHAIN: _get_filtered_chain,  # reuse SPY-shaped chain
        TOOL_GET_EVENTS: _get_aapl_events,
        TOOL_GET_JOURNAL_BY_SYMBOL: _get_empty_journal,
        TOOL_GET_POSITION_HISTORY: _get_no_position_history,
        TOOL_GET_PRICE_HISTORY: _get_price_history,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Scenario D — NVDA no IV history (warm-up period)
# Universe: NVDA only, iv_rank=None, iv_percentile=None
# Portfolio: flat
# Expected: action=NO_ACTION — system prompt mandates NO_ACTION when iv_rank is None
# ──────────────────────────────────────────────────────────────────────────────

_NVDA_ONLY_UNIVERSE = UniverseSnapshot(
    symbol_snapshots={
        "NVDA": _MOCK_UNIVERSE_SNAPSHOT.symbol_snapshots["NVDA"],
    },
    vix_level=_MOCK_UNIVERSE_SNAPSHOT.vix_level,
    market_regime=_MOCK_UNIVERSE_SNAPSHOT.market_regime,
    macro_events=[],
    as_of=_AS_OF,
)


def _get_nvda_only_universe(_tool_input: dict[str, Any]) -> UniverseSnapshot:
    return _NVDA_ONLY_UNIVERSE


def _get_nvda_events(tool_input: dict[str, Any]) -> dict[str, EventInfo]:
    return {
        sym: EventInfo(symbol=sym, earnings=None, ex_dividend=None)
        for sym in tool_input["symbols"]
    }


def make_no_iv_history_tool_impls() -> dict[str, ToolImpl]:
    """Scenario D: NVDA-only universe with iv_rank=None, flat portfolio."""
    return {
        TOOL_GET_PORTFOLIO_STATE: _get_flat_portfolio,
        TOOL_GET_UNIVERSE_SNAPSHOT: _get_nvda_only_universe,
        TOOL_GET_FILTERED_CHAIN: _get_filtered_chain,
        TOOL_GET_EVENTS: _get_nvda_events,
        TOOL_GET_JOURNAL_BY_SYMBOL: _get_empty_journal,
        TOOL_GET_POSITION_HISTORY: _get_no_position_history,
        TOOL_GET_PRICE_HISTORY: _get_price_history,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Scenario E — portfolio-aware (existing SPY position at 50% profit target)
# Universe: SPY only (high IV, neutral), portfolio has live SPY position
# Portfolio: existing SPY bull_put_spread at 50% profit (entry_net_amount=-2.70,
#   current_mark=-1.35) — the monitor's profit-target rule would normally fire.
#   The agent should account for this in its reasoning, ideally consulting
#   get_journal_by_symbol and populating informed_by.
# Expected: get_portfolio_state called; informed_by non-empty (preference)
# ──────────────────────────────────────────────────────────────────────────────

_SPY_ONLY_UNIVERSE = UniverseSnapshot(
    symbol_snapshots={
        "SPY": _MOCK_UNIVERSE_SNAPSHOT.symbol_snapshots["SPY"],
    },
    vix_level=_MOCK_UNIVERSE_SNAPSHOT.vix_level,
    market_regime=_MOCK_UNIVERSE_SNAPSHOT.market_regime,
    macro_events=_MOCK_UNIVERSE_SNAPSHOT.macro_events,
    as_of=_AS_OF,
)


def _get_spy_only_universe(_tool_input: dict[str, Any]) -> UniverseSnapshot:
    return _SPY_ONLY_UNIVERSE


def _get_spy_events(tool_input: dict[str, Any]) -> dict[str, EventInfo]:
    return {
        sym: _MOCK_EVENTS.get(
            sym, EventInfo(symbol=sym, earnings=None, ex_dividend=None)
        )
        for sym in tool_input["symbols"]
    }


def make_portfolio_aware_tool_impls() -> dict[str, ToolImpl]:
    """Scenario E: SPY-only universe with an existing open position."""
    return {
        TOOL_GET_PORTFOLIO_STATE: _get_portfolio_state,  # open position at 50% profit
        TOOL_GET_UNIVERSE_SNAPSHOT: _get_spy_only_universe,
        TOOL_GET_FILTERED_CHAIN: _get_filtered_chain,
        TOOL_GET_EVENTS: _get_spy_events,
        TOOL_GET_JOURNAL_BY_SYMBOL: _get_journal_by_symbol,  # has SPY opening record
        TOOL_GET_POSITION_HISTORY: _get_position_history,  # has pos-001 history
        TOOL_GET_PRICE_HISTORY: _get_price_history,
    }
