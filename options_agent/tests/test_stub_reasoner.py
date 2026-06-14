from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from options_agent.agent.stub_reasoner import (
    _STUB_EXPIRY,
    _STUB_EXPIRY_GUARD_DTE,
    stub_reasoner,
)
from options_agent.contracts.proposal import ExitPlan, TradeProposal
from options_agent.contracts.state import ContextSnapshot
from options_agent.risk.limits import Limits
from options_agent.risk.validator import validate_structural


def test_returns_trade_proposal() -> None:
    assert isinstance(stub_reasoner(), TradeProposal)


def test_all_required_fields_populated() -> None:
    p = stub_reasoner()
    assert p.action == "OPEN"
    assert p.underlying == "SPY"
    assert p.strategy == "bull_put_spread"
    assert len(p.legs) > 0
    assert p.thesis != ""
    assert p.iv_rationale != ""
    assert p.catalyst_check != ""
    assert 0.0 <= p.conviction <= 1.0
    assert p.est_max_loss > 0
    assert p.est_max_profit > 0
    assert len(p.breakevens) > 0
    assert isinstance(p.exit_plan, ExitPlan)
    assert isinstance(p.informed_by, list)


def test_iv_rationale_and_catalyst_check_non_trivial() -> None:
    p = stub_reasoner()
    assert len(p.iv_rationale) > 20
    assert len(p.catalyst_check) > 20


def test_exit_plan_sane_nonzero_values() -> None:
    p = stub_reasoner()
    assert p.exit_plan.profit_target_pct > 0
    assert p.exit_plan.stop_loss_mult > 0
    assert p.exit_plan.time_stop_dte > 0


def test_strategy_in_allowed_playbook() -> None:
    assert stub_reasoner().strategy in Limits().allowed_strategies


def test_no_naked_short_legs() -> None:
    p = stub_reasoner()
    for right in ("call", "put"):
        buy_ratio = sum(
            leg.ratio for leg in p.legs if leg.right == right and leg.side == "buy"
        )
        sell_ratio = sum(
            leg.ratio for leg in p.legs if leg.right == right and leg.side == "sell"
        )
        assert sell_ratio <= buy_ratio or sell_ratio == 0


def test_passes_structural_validation() -> None:
    p = stub_reasoner()
    result = validate_structural(p, Limits())
    assert result.passed, result.reasons


def test_deterministic() -> None:
    assert stub_reasoner() == stub_reasoner()


def test_accepts_none_context() -> None:
    assert isinstance(stub_reasoner(context=None), TradeProposal)


def test_accepts_context_snapshot() -> None:
    ctx = ContextSnapshot(
        assembled_context={},
        context_hash="abc123",
        model_id="stub",
        prompt_version="0.0",
        assembled_at=datetime.now(),
    )
    assert isinstance(stub_reasoner(context=ctx), TradeProposal)


def test_expiry_guard_fires_when_close() -> None:
    near_expiry = _STUB_EXPIRY - timedelta(days=_STUB_EXPIRY_GUARD_DTE - 1)
    with patch("options_agent.agent.stub_reasoner.date") as mock_date:
        mock_date.today.return_value = near_expiry
        with pytest.raises(RuntimeError, match="bump _STUB_EXPIRY"):
            stub_reasoner()


def test_expiry_guard_fires_at_boundary() -> None:
    at_boundary = _STUB_EXPIRY - timedelta(days=_STUB_EXPIRY_GUARD_DTE)
    with patch("options_agent.agent.stub_reasoner.date") as mock_date:
        mock_date.today.return_value = at_boundary
        with pytest.raises(RuntimeError, match="bump _STUB_EXPIRY"):
            stub_reasoner()


def test_expiry_guard_silent_when_far() -> None:
    far_from_expiry = _STUB_EXPIRY - timedelta(days=_STUB_EXPIRY_GUARD_DTE + 1)
    with patch("options_agent.agent.stub_reasoner.date") as mock_date:
        mock_date.today.return_value = far_from_expiry
        assert isinstance(stub_reasoner(), TradeProposal)
