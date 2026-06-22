"""Real tool implementations for the agent's read-only tool set (WP-8.5).

build_real_tool_impls() returns a tool implementation map matching the DI
interface from WP-6.1 (agent/tools.py), backed by live data: AlpacaDataClient
for chain/price data, YFinance for VIX and events, and the WP-2 state layer
for portfolio and journal reads.

This module is the production counterpart to agent/tools_mock.py. The same
tool name constants key both maps so callers cannot tell which backing is
active from the dispatch interface — that is the whole point of the DI pattern.

Live-money + mock-data guard: enforcement lives in orchestrator._build_tool_impls,
which hard-errors when use_real_data_tools=False and alpaca_paper=False. This
module is never imported on mock paths, so no per-function guard is needed here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.engine import Engine

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
from options_agent.config import Config
from options_agent.contracts.data import (
    EventInfo,
    FilteredChain,
    PortfolioState,
    UniverseSnapshot,
)
from options_agent.contracts.journal import JournalRecord
from options_agent.data.chains import get_filtered_chain as _chain_impl
from options_agent.data.events import get_events as _events_impl
from options_agent.data.greeks_iv import get_atm_iv
from options_agent.data.iv_rank import compute_iv_percentile, compute_iv_rank
from options_agent.data.market import get_universe_snapshot as _universe_impl
from options_agent.data.providers.alpaca_data import AlpacaDataClient
from options_agent.data.providers.yfinance_provider import YFinanceProvider
from options_agent.data.providers.yfinance_volatility_provider import (
    YFinanceVolatilityProvider,
)
from options_agent.execution.broker import BrokerClient
from options_agent.state.crud import get_position, list_open_positions
from options_agent.state.db import get_connection
from options_agent.state.journal import query_journal, query_outcome_records

logger = logging.getLogger(__name__)

ToolImpl = Callable[[dict[str, Any]], Any]

_EVENTS_LOOKAHEAD_DAYS = 60
_MACRO_LOOKAHEAD_DAYS = 60


def load_universe(config: Config) -> list[str]:
    """Return the ordered symbol list from config.universe_file.

    Lines starting with '#' and blank lines are ignored. Returns [] with a
    WARNING when the file is missing or empty so the cycle short-circuits at
    the EMPTY_ACTION_SPACE gate rather than crashing.

    Used by both build_real_tool_impls() (entry cycle) and run_daily_iv_job()
    (daily IV capture). One source so both paths trade the same universe.
    """
    path = config.universe_file
    if not path.exists():
        logger.warning(
            "Universe file %s not found — universe will be empty this cycle", path
        )
        return []
    symbols = [
        line.strip().upper()
        for line in path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not symbols:
        logger.warning("Universe file %s contains no symbols", path)
    return symbols


def build_real_tool_impls(
    config: Config,
    engine: Engine,
    broker: BrokerClient,
) -> dict[str, ToolImpl]:
    """Return a tool implementation map backed by live data sources.

    Creates AlpacaDataClient, YFinanceVolatilityProvider, and YFinanceProvider
    for this cycle and calls begin_cycle() on the data client so all chain/price
    fetches within the cycle see a coherent point-in-time snapshot.

    Args:
        config: Operational config — supplies limits, playbook, universe file.
        engine: SQLAlchemy engine for WP-2 state and journal reads.
        broker: BrokerClient for live account equity and buying power.
    """
    universe_symbols = load_universe(config)
    data_provider = AlpacaDataClient()
    vol_provider = YFinanceVolatilityProvider()
    event_provider = YFinanceProvider()
    data_provider.begin_cycle()

    def _portfolio(_tool_input: dict[str, Any]) -> PortfolioState:
        with get_connection(engine) as conn:
            positions = list_open_positions(conn)
        account = broker.get_account()
        equity = float(account.equity or 0)
        buying_power = float(account.buying_power or 0)
        options_bp = float(account.options_buying_power or 0)
        approval = int(account.options_approved_level or 0)
        unrealized = sum(p.unrealized_pnl for p in positions)
        return PortfolioState(
            positions=positions,
            account_equity=equity,
            buying_power=buying_power,
            options_buying_power=options_bp,
            unrealized_pnl=unrealized,
            realized_pnl_today=0.0,
            approval_level=approval,
            # Net Greeks are overwritten by context/assembler.py after chain
            # fetches; 0.0 here signals "not yet aggregated."
            net_dollar_delta=0.0,
            net_dollar_gamma=0.0,
            net_dollar_theta=0.0,
            net_dollar_vega=0.0,
        )

    def _universe(_tool_input: dict[str, Any]) -> UniverseSnapshot:
        snapshot = _universe_impl(
            symbols=universe_symbols,
            provider=data_provider,
            vol_provider=vol_provider,
            playbook=config.playbook,
            macro_lookahead_days=_MACRO_LOOKAHEAD_DAYS,
        )
        # Enrich each SymbolSnapshot with iv_rank and iv_percentile.
        # fetch_option_chain() is cached within the cycle (keyed by
        # ("fetch_option_chain", symbol)), so this call and the agent's later
        # get_filtered_chain() calls share the same cached chain object.
        # get_atm_iv() uses identical parameters (default target_dte=30) as
        # the daily IV job, keeping the live current_iv numerator commensurable
        # with the stored history — same ATM definition, same tenor.
        for symbol, ss in list(snapshot.symbol_snapshots.items()):
            try:
                contracts = data_provider.fetch_option_chain(symbol)
                atm_iv = get_atm_iv(contracts, ss.price)
                if atm_iv is None:
                    logger.debug(
                        "IV rank: %s — no ATM IV available; "
                        "iv_rank/iv_percentile remain None (symbol ineligible)",
                        symbol,
                    )
                    continue
                with get_connection(engine) as conn:
                    iv_rank = compute_iv_rank(symbol, atm_iv, conn)
                    iv_pct = compute_iv_percentile(symbol, atm_iv, conn)
                snapshot.symbol_snapshots[symbol] = ss.model_copy(
                    update={"iv_rank": iv_rank, "iv_percentile": iv_pct}
                )
            except Exception as exc:
                logger.warning(
                    "IV rank enrichment failed for %s — %s "
                    "(iv_rank/iv_percentile remain None; symbol ineligible this cycle)",
                    symbol,
                    exc,
                )
        return snapshot

    def _filtered_chain(tool_input: dict[str, Any]) -> FilteredChain:
        return _chain_impl(
            symbol=tool_input["symbol"],
            provider=data_provider,
            limits=config.limits.chain_filter,
            strategy_hint=tool_input.get("strategy_hint"),
        )

    def _events(tool_input: dict[str, Any]) -> dict[str, EventInfo]:
        return _events_impl(
            symbols=tool_input["symbols"],
            lookahead_days=_EVENTS_LOOKAHEAD_DAYS,
            provider=event_provider,
        )

    def _journal_by_symbol(tool_input: dict[str, Any]) -> list[JournalRecord]:
        symbol: str = tool_input["symbol"]
        with get_connection(engine) as conn:
            records = query_journal(conn, symbol=symbol)
        return records[-JOURNAL_MAX_RECORDS:]

    def _position_history(tool_input: dict[str, Any]) -> PositionHistory | None:
        position_id: str = tool_input["position_id"]
        with get_connection(engine) as conn:
            position = get_position(conn, position_id)
            if position is None:
                return None
            all_records = query_journal(conn, symbol=position.underlying)
            outcomes = query_outcome_records(conn, position_ids=[position_id])
        # JournalRecord.position_ids is a list[str]; scan for this position's opener.
        opening: JournalRecord | None = None
        for record in all_records:
            if position_id in record.position_ids:
                opening = record
                break
        return PositionHistory(opening_record=opening, outcome_records=outcomes)

    return {
        TOOL_GET_PORTFOLIO_STATE: _portfolio,
        TOOL_GET_UNIVERSE_SNAPSHOT: _universe,
        TOOL_GET_FILTERED_CHAIN: _filtered_chain,
        TOOL_GET_EVENTS: _events,
        TOOL_GET_JOURNAL_BY_SYMBOL: _journal_by_symbol,
        TOOL_GET_POSITION_HISTORY: _position_history,
    }
