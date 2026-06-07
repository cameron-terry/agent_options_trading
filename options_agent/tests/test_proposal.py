from datetime import date

import pytest

from options_agent.contracts import ExitPlan, Leg, TradeProposal


def _make_proposal(**overrides: object) -> TradeProposal:
    defaults: dict = {
        "action": "OPEN",
        "underlying": "SPY",
        "strategy": "bull_put_spread",
        "legs": [
            Leg(right="put", side="sell", strike=450.0, expiration=date(2026, 7, 18)),
            Leg(right="put", side="buy", strike=445.0, expiration=date(2026, 7, 18)),
        ],
        "thesis": "SPY near support with bullish bias",
        "iv_rationale": "IV rank at 65th percentile — selling premium is favourable",
        "catalyst_check": (
            "No earnings within 30 days; next FOMC in 3 weeks, outside blackout"
        ),
        "conviction": 0.7,
        "est_max_loss": 350.0,
        "est_max_profit": 150.0,
        "breakevens": [447.50],
        "net_delta": 0.12,
        "net_theta": 8.50,
        "net_vega": -0.30,
        "exit_plan": ExitPlan(
            profit_target_pct=0.50,
            stop_loss_mult=2.0,
            time_stop_dte=21,
        ),
        "informed_by": [],
    }
    defaults.update(overrides)
    return TradeProposal(**defaults)


def test_round_trip_json() -> None:
    proposal = _make_proposal()
    serialized = proposal.model_dump_json()
    restored = TradeProposal.model_validate_json(serialized)
    assert restored == proposal


def test_round_trip_dict() -> None:
    proposal = _make_proposal()
    restored = TradeProposal.model_validate(proposal.model_dump())
    assert restored == proposal


def test_action_enum_enforced() -> None:
    with pytest.raises(Exception):
        _make_proposal(action="BUY")


def test_conviction_upper_bound() -> None:
    with pytest.raises(Exception):
        _make_proposal(conviction=1.1)


def test_conviction_lower_bound() -> None:
    with pytest.raises(Exception):
        _make_proposal(conviction=-0.1)


def test_conviction_boundary_values() -> None:
    assert _make_proposal(conviction=0.0).conviction == 0.0
    assert _make_proposal(conviction=1.0).conviction == 1.0


def test_leg_ratio_default() -> None:
    leg = Leg(right="call", side="buy", strike=500.0, expiration=date(2026, 8, 15))
    assert leg.ratio == 1


def test_all_action_literals() -> None:
    for action in ("OPEN", "CLOSE", "ROLL", "NO_ACTION"):
        p = _make_proposal(action=action)
        assert p.action == action


def test_no_action_empty_legs() -> None:
    p = _make_proposal(action="NO_ACTION", legs=[])
    assert p.legs == []
