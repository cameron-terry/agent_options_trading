"""Context assembler (WP-6.2).

Collects the broad, always-needed context the agent requires at the start of
each entry cycle and packages it into a typed ContextBundle.

What is pre-loaded (low volume, agent always needs it):
  - Portfolio state + net Greeks (via portfolio.py)
  - Universe snapshot (all symbols: price, IV rank, regime, events flags)
  - Per-symbol events (earnings, ex-dividend, within lookahead window)
  - Recent journal summary per symbol (capped at journal_max_per_symbol)

What is NOT pre-loaded (high volume, drill-down via live tool calls):
  - Full filtered chains — the agent requests these via get_filtered_chain()
    for whichever symbols it is actively evaluating.
  - Full position history — available via get_position_history().

For portfolio Greek aggregation the assembler fetches chains for the underlyings
of all OPEN positions. These chains may not cover all held legs (entry filter
bounds DTE/delta; a leg entered at 45 DTE may now be at 10 DTE and fall outside
the filter). Missing legs emit a warning in greek_warnings and contribute 0.0 to
the net Greek totals. See context/portfolio.py module docstring for details on
the WP-3 dependency that would close this gap.

ContextBundle serves two consumers:
  1. render_overview(bundle) -> str — compact text injected as the leading user
     message before the agent's tool-use loop (priority order per design doc §10).
  2. to_context_snapshot(bundle) -> ContextSnapshot — the structured bundle dict
     stored in JournalRecord.context_snapshot for WP-7 reproducibility analysis.

The assembler does NOT write to the journal or move state. It is pure data
collection + packaging. WP-8 (orchestrator) owns the full cycle sequencing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from options_agent.agent.tools import (
    TOOL_GET_EVENTS,
    TOOL_GET_FILTERED_CHAIN,
    TOOL_GET_JOURNAL_BY_SYMBOL,
    TOOL_GET_PORTFOLIO_STATE,
    TOOL_GET_UNIVERSE_SNAPSHOT,
)
from options_agent.context.portfolio import aggregate_portfolio_greeks
from options_agent.contracts.data import (
    EventInfo,
    FilteredChain,
    PortfolioState,
    UniverseSnapshot,
)
from options_agent.contracts.journal import JournalRecord
from options_agent.contracts.state import ContextSnapshot

ToolImpl = Callable[[dict[str, Any]], Any]


class ContextBundle(BaseModel):
    """The structured pre-loaded context the agent starts from each entry cycle.

    All typed — consumers use portfolio.net_dollar_* directly, not by parsing
    the rendered overview text.  render_overview() renders to text for the agent
    prompt; to_context_snapshot() serialises to the journal.

    greek_warnings lists legs from open positions that could not be matched in
    the filtered chain.  A non-empty list means portfolio Greek totals are
    understated; the agent and WP-7 should treat them as lower bounds.

    excluded records symbols where data collection was incomplete or absent this
    cycle.  The reason string is human-readable (e.g. "chain_unavailable").
    Symbols still appear in universe.symbol_snapshots — excluded only signals
    a data-quality gap, not a trading eligibility decision (the validator owns
    eligibility).
    """

    portfolio: PortfolioState
    universe: UniverseSnapshot
    events: dict[str, EventInfo]
    journal: dict[str, list[JournalRecord]]
    excluded: dict[str, str]
    greek_warnings: list[str]
    assembled_at: datetime
    model_id: str
    prompt_version: str
    limits_version: str
    context_hash: str


def _compute_context_hash(
    portfolio: PortfolioState,
    universe: UniverseSnapshot,
    events: dict[str, EventInfo],
    journal: dict[str, list[JournalRecord]],
    excluded: dict[str, str],
    model_id: str,
    prompt_version: str,
) -> str:
    """SHA-256 (truncated to 16 hex chars) over the agent-visible data fields.

    Covers portfolio, universe, events, journal, excluded, model_id, and
    prompt_version. Excludes limits_version (recorded separately on
    JournalRecord), assembled_at (timestamp variance is not meaningful), and
    greek_warnings (derived metadata, not input data).

    Supports WP-7 reproducibility query: "did identical context produce
    different proposals?" Two bundles with the same hash saw the same data
    under the same model/prompt configuration.
    """
    payload: dict[str, Any] = {
        "portfolio": portfolio.model_dump(mode="json"),
        "universe": universe.model_dump(mode="json"),
        "events": {sym: ei.model_dump(mode="json") for sym, ei in events.items()},
        "journal": {
            sym: [r.model_dump(mode="json") for r in records]
            for sym, records in journal.items()
        },
        "excluded": excluded,
        "model_id": model_id,
        "prompt_version": prompt_version,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def assemble_context(
    tool_impls: dict[str, ToolImpl],
    model_id: str,
    prompt_version: str,
    limits_version: str,
    journal_max_per_symbol: int = 5,
) -> ContextBundle:
    """Collect and package the broad per-cycle context.

    Calls tool implementations directly (not through the LLM) to gather
    portfolio state, universe snapshot, events, and journal history. Then
    fetches filtered chains for the underlyings of all open positions to
    compute accurate net portfolio Greeks via portfolio.aggregate_portfolio_greeks.

    Args:
        tool_impls: Map of tool name → callable, matching the DI pattern from
            WP-6.1 (MOCK_TOOL_IMPLS for development; real WP-3 impls in
            production). Must contain at minimum:
            TOOL_GET_PORTFOLIO_STATE, TOOL_GET_UNIVERSE_SNAPSHOT,
            TOOL_GET_EVENTS, TOOL_GET_JOURNAL_BY_SYMBOL,
            TOOL_GET_FILTERED_CHAIN.
        model_id: Anthropic model ID to stamp on the bundle (for journaling).
        prompt_version: Prompt version string (for journaling).
        limits_version: Risk limits version string (for journaling).
        journal_max_per_symbol: Max journal records to include per symbol;
            oldest records are dropped first if the history exceeds this cap.
    """
    now = datetime.now(UTC)
    excluded: dict[str, str] = {}

    # ── 1. Portfolio state (account + positions; net Greeks will be overwritten) ──
    portfolio_raw: PortfolioState = tool_impls[TOOL_GET_PORTFOLIO_STATE]({})

    # ── 2. Universe snapshot ──────────────────────────────────────────────────────
    universe: UniverseSnapshot = tool_impls[TOOL_GET_UNIVERSE_SNAPSHOT]({})
    all_symbols = list(universe.symbol_snapshots.keys())

    # ── 3. Per-symbol events (batch) ──────────────────────────────────────────────
    events: dict[str, EventInfo] = {}
    if all_symbols:
        raw_events = tool_impls[TOOL_GET_EVENTS]({"symbols": all_symbols})
        if isinstance(raw_events, dict):
            events = raw_events

    # ── 4. Journal summary per symbol (capped) ────────────────────────────────────
    journal: dict[str, list[JournalRecord]] = {}
    for sym in all_symbols:
        records: list[JournalRecord] = tool_impls[TOOL_GET_JOURNAL_BY_SYMBOL](
            {"symbol": sym}
        )
        if records:
            journal[sym] = records[-journal_max_per_symbol:]

    # ── 5. Chains for held-position Greek aggregation ─────────────────────────────
    # Fetch only the underlyings of open positions — NOT for all universe symbols
    # (chains are high-volume; universe candidates get full chains via live agent
    # tool calls during the reasoning phase).
    portfolio_underlyings = {pos.underlying for pos in portfolio_raw.positions}
    chains_for_greeks: dict[str, FilteredChain] = {}
    for sym in portfolio_underlyings:
        chain = tool_impls[TOOL_GET_FILTERED_CHAIN]({"symbol": sym})
        if chain is not None:
            chains_for_greeks[sym] = chain
        else:
            excluded[sym] = "chain_unavailable"

    # ── 6. Portfolio Greek aggregation ───────────────────────────────────────────
    portfolio, greek_warnings = aggregate_portfolio_greeks(
        portfolio_raw, chains_for_greeks
    )

    # ── 7. Hash + bundle ─────────────────────────────────────────────────────────
    context_hash = _compute_context_hash(
        portfolio, universe, events, journal, excluded, model_id, prompt_version
    )

    return ContextBundle(
        portfolio=portfolio,
        universe=universe,
        events=events,
        journal=journal,
        excluded=excluded,
        greek_warnings=greek_warnings,
        assembled_at=now,
        model_id=model_id,
        prompt_version=prompt_version,
        limits_version=limits_version,
        context_hash=context_hash,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Overview renderer (produces the leading user message text)
# ──────────────────────────────────────────────────────────────────────────────


def _fmt_float(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{decimals}f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.0f}%"


def _fmt_usd(value: float) -> str:
    return f"${value:,.0f}"


def render_overview(bundle: ContextBundle) -> str:
    """Render a compact plain-text overview of *bundle* for the agent prompt.

    This text is injected as the leading user message before the agent's
    tool-use loop. It covers the broad context the agent always needs; detailed
    chain and position-history data remain available via live tool calls.

    Rendering order: portfolio state (positions + Greeks) first — the agent needs
    to know its existing exposure before evaluating new candidates — then universe
    snapshot (IV rank / vol context per design doc §10), events, journal summary,
    and data-gap notices. Full chains and position history remain as live tool
    calls; they are not rendered here.
    """
    lines: list[str] = []
    p = bundle.portfolio
    u = bundle.universe

    # ── Portfolio state ───────────────────────────────────────────────────────
    lines.append("=== PORTFOLIO STATE ===")
    lines.append(
        f"Account equity: {_fmt_usd(p.account_equity)}"
        f"  |  Options BP: {_fmt_usd(p.options_buying_power)}"
        f"  |  Approval: L{p.approval_level}"
    )
    lines.append(
        f"Net delta: {_fmt_usd(p.net_dollar_delta)}"
        f"  |  Net vega: {_fmt_usd(p.net_dollar_vega)}"
        f"  |  Net theta: {_fmt_usd(p.net_dollar_theta)}"
        f"  |  Net gamma: {_fmt_usd(p.net_dollar_gamma)}"
    )
    lines.append(
        f"P&L: unrealized {_fmt_usd(p.unrealized_pnl)}"
        f"  |  realized today {_fmt_usd(p.realized_pnl_today)}"
    )
    if p.positions:
        lines.append(f"Open positions ({len(p.positions)}):")
        for pos in p.positions:
            expiry = pos.nearest_expiration.isoformat()
            pnl_sign = "+" if pos.unrealized_pnl >= 0 else ""
            legs_summary = ", ".join(
                f"{pl.leg.side[0].upper()}{pl.leg.right[0].upper()}@{pl.leg.strike:.0f}"
                for pl in pos.legs
            )
            lines.append(
                f"  [{pos.id}] {pos.underlying} {pos.strategy} ({legs_summary})"
                f"  exp {expiry}  qty={pos.quantity}"
                f"  P&L: {pnl_sign}{_fmt_usd(pos.unrealized_pnl)}"
            )
    else:
        lines.append("No open positions.")

    if bundle.greek_warnings:
        lines.append(
            f"Greek warnings ({len(bundle.greek_warnings)}):"
            " some legs missing from chain; Greek totals understated."
        )

    # ── Universe snapshot ─────────────────────────────────────────────────────
    lines.append("")
    lines.append(
        f"=== UNIVERSE SNAPSHOT"
        f"  (VIX {_fmt_float(u.vix_level)}  |  regime: {u.market_regime}) ==="
    )

    if u.macro_events:
        macro_strs = [f"{e.name} {e.event_date.isoformat()}" for e in u.macro_events]
        lines.append(f"Macro events: {', '.join(macro_strs)}")

    # Header
    lines.append(
        f"{'Symbol':<6}  {'Price':>8}  {'IV Rank':>7}  {'IV%ile':>6}"
        f"  {'→Earn':>5}  {'Regime':<10}  Notes"
    )
    lines.append("-" * 72)
    for sym, ss in sorted(u.symbol_snapshots.items()):
        iv_rank_str = _fmt_pct(ss.iv_rank)
        iv_pct_str = _fmt_pct(ss.iv_percentile)
        earn_str = f"{ss.days_to_earnings}d" if ss.days_to_earnings is not None else "—"
        regime_str = ss.regime or "—"
        notes: list[str] = []
        if sym in bundle.excluded:
            notes.append(f"DATA: {bundle.excluded[sym]}")
        if ss.iv_rank is None:
            notes.append("ineligible (iv_rank unknown)")
        elif ss.days_to_earnings is not None and ss.days_to_earnings <= 5:
            notes.append(f"⚠ earnings {ss.days_to_earnings}d")
        lines.append(
            f"{sym:<6}  {_fmt_float(ss.price, 2):>8}"
            f"  {iv_rank_str:>7}  {iv_pct_str:>6}"
            f"  {earn_str:>5}  {regime_str:<10}  {', '.join(notes)}"
        )

    # ── Events (per-symbol detail) ────────────────────────────────────────────
    event_lines: list[str] = []
    for sym, ei in sorted(bundle.events.items()):
        parts: list[str] = []
        if ei.earnings:
            conf = "confirmed" if ei.earnings.confirmed else "estimated"
            parts.append(f"earnings {ei.earnings.event_date.isoformat()} ({conf})")
        if ei.ex_dividend:
            parts.append(
                f"ex-div {ei.ex_dividend.event_date.isoformat()}"
                f" ${ei.ex_dividend.amount:.2f}"
            )
        if parts:
            event_lines.append(f"  {sym}: {'; '.join(parts)}")
    if event_lines:
        lines.append("")
        lines.append("=== EVENTS ===")
        lines.extend(event_lines)

    # ── Journal summary ───────────────────────────────────────────────────────
    if bundle.journal:
        lines.append("")
        lines.append("=== RECENT JOURNAL ===")
        for sym, records in sorted(bundle.journal.items()):
            if not records:
                continue
            last = records[-1]
            summary_parts = [f"last: {last.action_taken.value}"]
            if last.strategy:
                summary_parts.append(last.strategy)
            if last.conviction is not None:
                summary_parts.append(f"conviction={last.conviction:.2f}")
            summary_parts.append(last.timestamp.strftime("%Y-%m-%d"))
            lines.append(
                f"  {sym}: {len(records)} cycle(s) — {', '.join(summary_parts)}"
            )

    # ── Excluded ─────────────────────────────────────────────────────────────
    if bundle.excluded:
        lines.append("")
        lines.append("=== DATA GAPS ===")
        for sym, reason in sorted(bundle.excluded.items()):
            lines.append(f"  {sym}: {reason}")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Journal helper
# ──────────────────────────────────────────────────────────────────────────────


def _journal_record_summary(record: JournalRecord) -> dict[str, Any]:
    """Compact, non-recursive summary of a historical JournalRecord.

    Deliberately omits `decision` and `context_snapshot` — JournalRecord.
    context_snapshot.assembled_context embeds this same `journal` field
    (each historical record carries its own prior N records), so dumping it
    in full here would re-embed that record's entire ancestry. Because every
    cycle's context_snapshot is persisted and then re-fetched as one of the
    *next* cycle's journal entries, embedding it in full compounds
    generation over generation — prompt size grows geometrically with cycle
    count until requests blow past the model's context window. The
    denormalized fields on JournalRecord (strategy, underlying, conviction,
    etc.) exist precisely to index this history without the nested payload;
    use those instead of model_dump()'ing the whole record.
    """
    return {
        "cycle_id": record.cycle_id,
        "timestamp": record.timestamp.isoformat(),
        "action_taken": record.action_taken.value,
        "strategy": record.strategy,
        "underlying": record.underlying,
        "net_delta_at_open": record.net_delta_at_open,
        "earnings_within_dte": record.earnings_within_dte,
        "conviction": record.conviction,
        "iv_rank_at_open": record.iv_rank_at_open,
        "rejection_rule_ids": [r.value for r in record.rejection_rule_ids],
    }


def to_context_snapshot(bundle: ContextBundle) -> ContextSnapshot:
    """Serialise *bundle* into the ContextSnapshot stored in the JournalRecord.

    The assembled_context dict contains the full bundle data payload so WP-7
    can reconstruct what the agent started from without relying on tool-call
    transcripts.  model_id and prompt_version are duplicated at the top level
    of ContextSnapshot per the WP-0.3 contract.

    Journal history is summarized via _journal_record_summary() rather than
    dumped in full — see that function's docstring for why: full dumps
    recurse through each record's own context_snapshot and compound across
    cycles.
    """
    assembled: dict[str, Any] = {
        "portfolio": bundle.portfolio.model_dump(mode="json"),
        "universe": bundle.universe.model_dump(mode="json"),
        "events": {
            sym: ei.model_dump(mode="json") for sym, ei in bundle.events.items()
        },
        "journal": {
            sym: [_journal_record_summary(r) for r in records]
            for sym, records in bundle.journal.items()
        },
        "excluded": bundle.excluded,
        "greek_warnings": bundle.greek_warnings,
        "limits_version": bundle.limits_version,
    }
    return ContextSnapshot(
        assembled_context=assembled,
        context_hash=bundle.context_hash,
        model_id=bundle.model_id,
        prompt_version=bundle.prompt_version,
        assembled_at=bundle.assembled_at,
    )
