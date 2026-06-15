"""Tests for WP-6.2: context assembler + portfolio Greek aggregation.

Verifies:
  1. portfolio.aggregate_portfolio_greeks correctly computes net dollar-Greeks
     from chain data using the WP-4.4 unit convention.
  2. Missing-leg and missing-chain cases emit warnings and contribute 0.0.
  3. assemble_context() returns a well-formed ContextBundle against mock tools.
  4. context_hash is deterministic and matches the round-trip ContextSnapshot.
  5. render_overview() returns a non-empty string covering key sections.
  6. to_context_snapshot() produces a valid ContextSnapshot with the right fields.
"""

from datetime import UTC, date, datetime

import pytest

from options_agent.agent.tools_mock import MOCK_TOOL_IMPLS
from options_agent.context.assembler import (
    ContextBundle,
    assemble_context,
    render_overview,
    to_context_snapshot,
)
from options_agent.context.portfolio import aggregate_portfolio_greeks
from options_agent.contracts.data import (
    ChainFilterParams,
    FilteredChain,
    OptionContract,
    PortfolioState,
    UniverseSnapshot,
)
from options_agent.contracts.proposal import ExitPlan, Leg
from options_agent.contracts.state import (
    AssetClass,
    ContextSnapshot,
    LegStatus,
    Position,
    PositionLeg,
    PositionStatus,
)

# ──────────────────────────────────────────────────────────────────────────────
# Shared test data
# ──────────────────────────────────────────────────────────────────────────────

_AS_OF = datetime(2026, 6, 14, 14, 30, 0, tzinfo=UTC)

_FILTER_PARAMS = ChainFilterParams(
    dte_min=21,
    dte_max=60,
    delta_min=0.15,
    delta_max=0.40,
    min_open_interest=100,
    max_spread_pct_of_mid=0.15,
    max_spread_abs_floor=0.30,
)

_EXPIRY = date(2026, 7, 18)  # 34 DTE from _AS_OF


def _make_position(
    pos_id: str = "test-pos-001",
    underlying: str = "SPY",
    sell_strike: float = 530.0,
    buy_strike: float = 525.0,
    qty: int = 2,
) -> Position:
    return Position(
        id=pos_id,
        underlying=underlying,
        strategy="bull_put_spread",
        legs=[
            PositionLeg(
                leg=Leg(
                    right="put", side="sell", strike=sell_strike, expiration=_EXPIRY
                ),
                filled_qty=qty,
                avg_fill_price=2.45,
                status=LegStatus.OPEN,
            ),
            PositionLeg(
                leg=Leg(right="put", side="buy", strike=buy_strike, expiration=_EXPIRY),
                filled_qty=qty,
                avg_fill_price=1.10,
                status=LegStatus.OPEN,
            ),
        ],
        quantity=qty,
        entry_net_amount=-2.70,
        current_mark=-1.35,
        marked_at=_AS_OF,
        unrealized_pnl=135.0,
        realized_pnl=None,
        exit_plan=ExitPlan(
            profit_target_pct=0.50, stop_loss_mult=2.0, time_stop_dte=21
        ),
        status=PositionStatus.OPEN,
        opened_at=datetime(2026, 6, 7, 15, 0, 0, tzinfo=UTC),
        closed_at=None,
        nearest_expiration=_EXPIRY,
        est_max_loss=500.0,
        est_max_profit=270.0,
        opening_order_id="ord-001",
        asset_class=AssetClass.OPTION_STRATEGY,
    )


def _make_spy_chain(
    sell_strike: float = 530.0,
    buy_strike: float = 525.0,
    underlying_price: float = 545.20,
    sell_delta: float = -0.21,
    sell_vega: float = 0.38,
    sell_theta: float = -0.18,
    buy_delta: float = -0.15,
    buy_vega: float = 0.28,
    buy_theta: float = -0.13,
) -> FilteredChain:
    return FilteredChain(
        underlying="SPY",
        underlying_price=underlying_price,
        as_of=_AS_OF,
        filter_params=_FILTER_PARAMS,
        contracts=[
            OptionContract(
                symbol=f"SPY260718P{int(sell_strike * 1000):08d}",
                strike=sell_strike,
                expiration=_EXPIRY,
                right="put",
                bid=2.35,
                ask=2.55,
                mid=2.45,
                volume=3200,
                open_interest=18700,
                delta=sell_delta,
                theta=sell_theta,
                vega=sell_vega,
                iv=0.175,
                spread_width=0.20,
                dte=34,
                greek_source="alpaca",
            ),
            OptionContract(
                symbol=f"SPY260718P{int(buy_strike * 1000):08d}",
                strike=buy_strike,
                expiration=_EXPIRY,
                right="put",
                bid=1.05,
                ask=1.20,
                mid=1.125,
                volume=4100,
                open_interest=22300,
                delta=buy_delta,
                theta=buy_theta,
                vega=buy_vega,
                iv=0.168,
                spread_width=0.15,
                dte=34,
                greek_source="alpaca",
            ),
        ],
    )


def _make_empty_portfolio(position: Position | None = None) -> PortfolioState:
    positions = [position] if position is not None else []
    return PortfolioState(
        positions=positions,
        account_equity=50000.0,
        buying_power=38000.0,
        options_buying_power=35000.0,
        unrealized_pnl=135.0 if position else 0.0,
        realized_pnl_today=0.0,
        approval_level=3,
        net_dollar_delta=0.0,
        net_dollar_gamma=0.0,
        net_dollar_theta=0.0,
        net_dollar_vega=0.0,
    )


# ──────────────────────────────────────────────────────────────────────────────
# portfolio.aggregate_portfolio_greeks — happy path
# ──────────────────────────────────────────────────────────────────────────────

# Expected values for a 2-lot 530/525 bull put spread on SPY at $545.20:
#   Short 530P: delta=-0.21, vega=0.38, theta=-0.18, side_sign=-1, qty=2, ratio=1
#   Long  525P: delta=-0.15, vega=0.28, theta=-0.13, side_sign=+1, qty=2, ratio=1
#
# dollar_delta = short: -0.21×-1×2×545.20×100 = +22898.4
#              + long:  -0.15×+1×2×545.20×100 = -16356.0
#              = 6542.4
# dollar_vega  = short: 0.38×-1×2×100 = -76; long: 0.28×+1×2×100 = +56 → -20.0
# dollar_theta = short: -0.18×-1×2×100 = +36; long: -0.13×+1×2×100 = -26 → 10.0

_EXPECTED_DELTA = 6542.4
_EXPECTED_VEGA = -20.0
_EXPECTED_THETA = 10.0


def test_greek_aggregation_delta() -> None:
    position = _make_position()
    portfolio_raw = _make_empty_portfolio(position)
    chain = _make_spy_chain()
    result, warnings = aggregate_portfolio_greeks(portfolio_raw, {"SPY": chain})
    assert abs(result.net_dollar_delta - _EXPECTED_DELTA) < 0.01
    assert not warnings


def test_greek_aggregation_vega() -> None:
    position = _make_position()
    portfolio_raw = _make_empty_portfolio(position)
    chain = _make_spy_chain()
    result, warnings = aggregate_portfolio_greeks(portfolio_raw, {"SPY": chain})
    assert abs(result.net_dollar_vega - _EXPECTED_VEGA) < 0.01


def test_greek_aggregation_theta() -> None:
    position = _make_position()
    portfolio_raw = _make_empty_portfolio(position)
    chain = _make_spy_chain()
    result, warnings = aggregate_portfolio_greeks(portfolio_raw, {"SPY": chain})
    assert abs(result.net_dollar_theta - _EXPECTED_THETA) < 0.01


def test_greek_aggregation_gamma_is_zero() -> None:
    """net_dollar_gamma is always 0.0 — OptionContract intentionally omits gamma."""
    position = _make_position()
    portfolio_raw = _make_empty_portfolio(position)
    chain = _make_spy_chain()
    result, _ = aggregate_portfolio_greeks(portfolio_raw, {"SPY": chain})
    assert result.net_dollar_gamma == 0.0


def test_greek_aggregation_no_warnings_on_hit() -> None:
    position = _make_position()
    portfolio_raw = _make_empty_portfolio(position)
    chain = _make_spy_chain()
    _, warnings = aggregate_portfolio_greeks(portfolio_raw, {"SPY": chain})
    assert warnings == []


def test_greek_aggregation_preserves_account_fields() -> None:
    position = _make_position()
    portfolio_raw = _make_empty_portfolio(position)
    chain = _make_spy_chain()
    result, _ = aggregate_portfolio_greeks(portfolio_raw, {"SPY": chain})
    assert result.account_equity == portfolio_raw.account_equity
    assert result.buying_power == portfolio_raw.buying_power
    assert result.options_buying_power == portfolio_raw.options_buying_power
    assert result.positions == portfolio_raw.positions


def test_greek_aggregation_buy_side_direction() -> None:
    """A single long put contributes negative dollar delta (loses when stock falls)."""
    leg = PositionLeg(
        leg=Leg(right="put", side="buy", strike=530.0, expiration=_EXPIRY),
        filled_qty=1,
        avg_fill_price=2.45,
        status=LegStatus.OPEN,
    )
    position = Position(
        id="p-buy",
        underlying="SPY",
        strategy="long_put",
        legs=[leg],
        quantity=1,
        entry_net_amount=2.45,
        current_mark=2.45,
        marked_at=_AS_OF,
        unrealized_pnl=0.0,
        realized_pnl=None,
        exit_plan=None,
        status=PositionStatus.OPEN,
        opened_at=_AS_OF,
        closed_at=None,
        nearest_expiration=_EXPIRY,
        est_max_loss=245.0,
        est_max_profit=52755.0,
        opening_order_id="ord-buy",
    )
    portfolio_raw = _make_empty_portfolio(position)
    chain = _make_spy_chain()
    result, _ = aggregate_portfolio_greeks(portfolio_raw, {"SPY": chain})
    # Long put: delta=-0.21, side=buy, side_sign=+1
    # contribution = -0.21 × 545.20 × 100 < 0
    assert result.net_dollar_delta < 0


def test_greek_aggregation_no_positions_all_zero() -> None:
    portfolio_raw = _make_empty_portfolio()
    result, warnings = aggregate_portfolio_greeks(portfolio_raw, {})
    assert result.net_dollar_delta == 0.0
    assert result.net_dollar_vega == 0.0
    assert result.net_dollar_theta == 0.0
    assert result.net_dollar_gamma == 0.0
    assert warnings == []


# ──────────────────────────────────────────────────────────────────────────────
# Missing-chain and missing-leg handling
# ──────────────────────────────────────────────────────────────────────────────


def test_missing_chain_emits_warning() -> None:
    position = _make_position(underlying="AAPL")
    portfolio_raw = _make_empty_portfolio(position)
    # No AAPL chain supplied
    result, warnings = aggregate_portfolio_greeks(portfolio_raw, {})
    assert len(warnings) == 1
    assert "AAPL" in warnings[0]


def test_missing_chain_contributes_zero() -> None:
    position = _make_position(underlying="AAPL")
    portfolio_raw = _make_empty_portfolio(position)
    result, _ = aggregate_portfolio_greeks(portfolio_raw, {})
    assert result.net_dollar_delta == 0.0
    assert result.net_dollar_vega == 0.0
    assert result.net_dollar_theta == 0.0


def test_missing_leg_in_chain_emits_warning() -> None:
    """When a held leg's strike is not in the chain, a per-leg warning is emitted."""
    position = _make_position(sell_strike=530.0, buy_strike=525.0)
    portfolio_raw = _make_empty_portfolio(position)
    # Chain contains only the 530 put, not the 525 put
    chain = FilteredChain(
        underlying="SPY",
        underlying_price=545.20,
        as_of=_AS_OF,
        filter_params=_FILTER_PARAMS,
        contracts=[
            OptionContract(
                symbol="SPY260718P00530000",
                strike=530.0,
                expiration=_EXPIRY,
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
            ),
        ],
    )
    _, warnings = aggregate_portfolio_greeks(portfolio_raw, {"SPY": chain})
    assert len(warnings) == 1
    assert "525" in warnings[0]
    assert "filter window" in warnings[0]


def test_missing_leg_partial_greek_counted() -> None:
    """The found leg's Greeks are still counted even when the other leg is missing."""
    position = _make_position(sell_strike=530.0, buy_strike=525.0, qty=1)
    portfolio_raw = _make_empty_portfolio(position)
    chain = FilteredChain(
        underlying="SPY",
        underlying_price=545.20,
        as_of=_AS_OF,
        filter_params=_FILTER_PARAMS,
        contracts=[
            OptionContract(
                symbol="SPY260718P00530000",
                strike=530.0,
                expiration=_EXPIRY,
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
            ),
        ],
    )
    result, _ = aggregate_portfolio_greeks(portfolio_raw, {"SPY": chain})
    # Short 530P only: delta=-0.21, side_sign=-1, qty=1, price=545.20
    expected_delta = -0.21 * -1 * 1 * 1 * 545.20 * 100
    assert abs(result.net_dollar_delta - expected_delta) < 0.01


# ──────────────────────────────────────────────────────────────────────────────
# assemble_context — full integration against mock tool impls
# ──────────────────────────────────────────────────────────────────────────────


def test_assemble_context_returns_context_bundle() -> None:
    bundle = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    assert isinstance(bundle, ContextBundle)


def test_assemble_context_portfolio_present() -> None:
    bundle = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    assert isinstance(bundle.portfolio, PortfolioState)


def test_assemble_context_universe_present() -> None:
    bundle = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    assert isinstance(bundle.universe, UniverseSnapshot)
    assert "SPY" in bundle.universe.symbol_snapshots


def test_assemble_context_events_present() -> None:
    bundle = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    assert isinstance(bundle.events, dict)
    # All universe symbols should have event entries
    for sym in bundle.universe.symbol_snapshots:
        assert sym in bundle.events


def test_assemble_context_journal_present() -> None:
    bundle = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    # SPY has a journal record in the mock
    assert "SPY" in bundle.journal
    assert len(bundle.journal["SPY"]) >= 1


def test_assemble_context_journal_cap_respected() -> None:
    bundle = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
        journal_max_per_symbol=3,
    )
    for records in bundle.journal.values():
        assert len(records) <= 3


def test_assemble_context_metadata_stamped() -> None:
    bundle = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="my-model-id",
        prompt_version="v2",
        limits_version="1.0.0",
    )
    assert bundle.model_id == "my-model-id"
    assert bundle.prompt_version == "v2"
    assert bundle.limits_version == "1.0.0"


def test_assemble_context_hash_non_empty() -> None:
    bundle = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    assert len(bundle.context_hash) == 16


def test_assemble_context_hash_deterministic() -> None:
    """Same inputs must produce the same hash on repeated calls."""
    b1 = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    b2 = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    assert b1.context_hash == b2.context_hash


def test_assemble_context_hash_changes_with_prompt_version() -> None:
    b1 = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    b2 = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.2",
        limits_version="0.2.0",
    )
    assert b1.context_hash != b2.context_hash


def test_assemble_context_greeks_computed_from_chain() -> None:
    """The assembler must overwrite the raw portfolio's net_dollar_* with values
    computed from chain data, not carry through the mock's hardcoded values."""
    bundle = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    # The mock position is a 2-lot 530/525 bull put spread on SPY at $545.20.
    # Expected values computed from mock chain deltas (see test module header):
    #   dollar_delta ≈ 6542.4, dollar_vega ≈ -20.0, dollar_theta ≈ 10.0
    assert abs(bundle.portfolio.net_dollar_delta - _EXPECTED_DELTA) < 0.01
    assert abs(bundle.portfolio.net_dollar_vega - _EXPECTED_VEGA) < 0.01
    assert abs(bundle.portfolio.net_dollar_theta - _EXPECTED_THETA) < 0.01


def test_assemble_context_excluded_dict_is_dict() -> None:
    bundle = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    assert isinstance(bundle.excluded, dict)


def test_assemble_context_greek_warnings_is_list() -> None:
    bundle = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    assert isinstance(bundle.greek_warnings, list)


# ──────────────────────────────────────────────────────────────────────────────
# render_overview
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_bundle() -> ContextBundle:
    return assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )


def test_render_overview_is_nonempty_string(sample_bundle: ContextBundle) -> None:
    overview = render_overview(sample_bundle)
    assert isinstance(overview, str)
    assert len(overview) > 100


def test_render_overview_has_portfolio_section(sample_bundle: ContextBundle) -> None:
    overview = render_overview(sample_bundle)
    assert "PORTFOLIO STATE" in overview


def test_render_overview_has_universe_section(sample_bundle: ContextBundle) -> None:
    overview = render_overview(sample_bundle)
    assert "UNIVERSE SNAPSHOT" in overview


def test_render_overview_lists_all_symbols(sample_bundle: ContextBundle) -> None:
    overview = render_overview(sample_bundle)
    for sym in sample_bundle.universe.symbol_snapshots:
        assert sym in overview


def test_render_overview_shows_portfolio_greeks(sample_bundle: ContextBundle) -> None:
    overview = render_overview(sample_bundle)
    assert "Net delta" in overview
    assert "Net vega" in overview
    assert "Net theta" in overview


def test_render_overview_shows_open_position(sample_bundle: ContextBundle) -> None:
    overview = render_overview(sample_bundle)
    # The mock has one open position on SPY
    assert "SPY" in overview
    assert "bull_put_spread" in overview


def test_render_overview_has_events_section_when_events_exist(
    sample_bundle: ContextBundle,
) -> None:
    overview = render_overview(sample_bundle)
    # AAPL has confirmed earnings in the mock
    assert "AAPL" in overview
    assert "earnings" in overview


def test_render_overview_has_journal_section(sample_bundle: ContextBundle) -> None:
    overview = render_overview(sample_bundle)
    assert "JOURNAL" in overview


def test_render_overview_shows_excluded_when_present() -> None:
    """If excluded dict is non-empty, render_overview shows a DATA GAPS section."""
    bundle = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    # Inject a fake exclusion to test rendering
    bundle_with_excluded = bundle.model_copy(
        update={"excluded": {"FAKE": "chain_unavailable"}}
    )
    overview = render_overview(bundle_with_excluded)
    assert "DATA GAPS" in overview
    assert "FAKE" in overview


def test_render_overview_shows_greek_warning_when_present() -> None:
    bundle = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    bundle_with_warnings = bundle.model_copy(
        update={"greek_warnings": ["pos test-001: leg not in chain"]}
    )
    overview = render_overview(bundle_with_warnings)
    assert "Greek warnings" in overview


# ──────────────────────────────────────────────────────────────────────────────
# to_context_snapshot
# ──────────────────────────────────────────────────────────────────────────────


def test_to_context_snapshot_returns_context_snapshot(
    sample_bundle: ContextBundle,
) -> None:
    snapshot = to_context_snapshot(sample_bundle)
    assert isinstance(snapshot, ContextSnapshot)


def test_to_context_snapshot_hash_matches_bundle(sample_bundle: ContextBundle) -> None:
    snapshot = to_context_snapshot(sample_bundle)
    assert snapshot.context_hash == sample_bundle.context_hash


def test_to_context_snapshot_model_id_matches(sample_bundle: ContextBundle) -> None:
    snapshot = to_context_snapshot(sample_bundle)
    assert snapshot.model_id == sample_bundle.model_id


def test_to_context_snapshot_prompt_version_matches(
    sample_bundle: ContextBundle,
) -> None:
    snapshot = to_context_snapshot(sample_bundle)
    assert snapshot.prompt_version == sample_bundle.prompt_version


def test_to_context_snapshot_assembled_context_is_dict(
    sample_bundle: ContextBundle,
) -> None:
    snapshot = to_context_snapshot(sample_bundle)
    assert isinstance(snapshot.assembled_context, dict)


def test_to_context_snapshot_assembled_context_has_portfolio(
    sample_bundle: ContextBundle,
) -> None:
    snapshot = to_context_snapshot(sample_bundle)
    assert "portfolio" in snapshot.assembled_context


def test_to_context_snapshot_assembled_context_has_universe(
    sample_bundle: ContextBundle,
) -> None:
    snapshot = to_context_snapshot(sample_bundle)
    assert "universe" in snapshot.assembled_context


def test_to_context_snapshot_assembled_context_has_events(
    sample_bundle: ContextBundle,
) -> None:
    snapshot = to_context_snapshot(sample_bundle)
    assert "events" in snapshot.assembled_context


def test_to_context_snapshot_assembled_context_has_journal(
    sample_bundle: ContextBundle,
) -> None:
    snapshot = to_context_snapshot(sample_bundle)
    assert "journal" in snapshot.assembled_context


def test_to_context_snapshot_assembled_context_has_limits_version(
    sample_bundle: ContextBundle,
) -> None:
    snapshot = to_context_snapshot(sample_bundle)
    assert "limits_version" in snapshot.assembled_context
    assert snapshot.assembled_context["limits_version"] == sample_bundle.limits_version


def test_to_context_snapshot_round_trip_hash() -> None:
    """Hashing the ContextSnapshot's assembled_context produces the same hash."""
    import hashlib
    import json

    bundle = assemble_context(
        MOCK_TOOL_IMPLS,
        model_id="claude-sonnet-4-6",
        prompt_version="0.1",
        limits_version="0.2.0",
    )
    snapshot = to_context_snapshot(bundle)

    # The context_hash is over portfolio/universe/events/journal/excluded/model_id/
    # prompt_version (NOT limits_version or assembled_at — see _compute_context_hash).
    payload = {
        "portfolio": bundle.portfolio.model_dump(mode="json"),
        "universe": bundle.universe.model_dump(mode="json"),
        "events": {
            sym: ei.model_dump(mode="json") for sym, ei in bundle.events.items()
        },
        "journal": {
            sym: [r.model_dump(mode="json") for r in records]
            for sym, records in bundle.journal.items()
        },
        "excluded": bundle.excluded,
        "model_id": bundle.model_id,
        "prompt_version": bundle.prompt_version,
    }
    expected_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    assert snapshot.context_hash == expected_hash
