from datetime import date

from options_agent.contracts.proposal import ExitPlan, Leg, TradeProposal
from options_agent.contracts.state import ContextSnapshot

# Hardcoded expiry for the WP-0.5 vertical slice — a real quarterly options
# expiration. Bump this when the guard below fires.
_STUB_EXPIRY = date(2026, 9, 19)
_STUB_EXPIRY_GUARD_DTE = 30


def stub_reasoner(context: ContextSnapshot | None = None) -> TradeProposal:
    """Return a hardcoded, schema-valid TradeProposal for the WP-0.5 slice.

    Accepts (and ignores) a ContextSnapshot to match the eventual signature of
    the real reasoner in agent/reasoner.py. WP-0.5.2 can import either function
    with the same call site; swapping stub→real is a one-line import change.

    Raises RuntimeError if the hardcoded expiry drifts within _STUB_EXPIRY_GUARD_DTE
    days of today — bump _STUB_EXPIRY when that fires.
    """
    days_to_expiry = (_STUB_EXPIRY - date.today()).days
    if days_to_expiry < _STUB_EXPIRY_GUARD_DTE:
        raise RuntimeError(
            f"stub_reasoner: hardcoded expiry {_STUB_EXPIRY} is within"
            f" {_STUB_EXPIRY_GUARD_DTE} days — bump _STUB_EXPIRY in"
            " options_agent/agent/stub_reasoner.py"
        )

    return TradeProposal(
        action="OPEN",
        underlying="SPY",
        strategy="bull_put_spread",
        legs=[
            Leg(right="put", side="sell", strike=450.0, expiration=_STUB_EXPIRY),
            Leg(right="put", side="buy", strike=445.0, expiration=_STUB_EXPIRY),
        ],
        thesis=(
            "SPY is trading near the 450 level with a bullish macro backdrop;"
            " probability of closing below 445 at expiry is low given current"
            " technical support and trend structure."
        ),
        iv_rationale=(
            "IV rank is in the 60th–70th percentile — elevated enough that"
            " selling premium captures a meaningful vol risk premium without"
            " chasing IV at extremes. A bull put spread keeps defined risk while"
            " collecting inflated extrinsic value."
        ),
        catalyst_check=(
            "No SPY-level earnings catalyst; SPY tracks the S&P 500 index and"
            " has no single-company earnings date. No FOMC meeting within the"
            " next 5 calendar days. Macro calendar clear for the 21-DTE window."
        ),
        conviction=0.65,
        est_max_loss=350.0,
        est_max_profit=150.0,
        breakevens=[448.50],
        net_delta=0.12,
        net_theta=8.50,
        net_vega=-0.30,
        exit_plan=ExitPlan(
            profit_target_pct=0.50,
            stop_loss_mult=2.0,
            time_stop_dte=21,
        ),
        informed_by=[],
    )
