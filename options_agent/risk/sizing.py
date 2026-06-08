import math

from options_agent.contracts.data import PortfolioState
from options_agent.contracts.proposal import TradeProposal
from options_agent.contracts.results import SizingConstraint, SizingResult
from options_agent.risk.limits import Limits


def size(
    proposal: TradeProposal,
    portfolio_state: PortfolioState,
    limits: Limits,
) -> SizingResult:
    """Translate a validated proposal into a concrete contract count.

    Gate-and-flat model: conviction either clears the floor (full budget) or
    does not (zero contracts). The agent's raw conviction score is not trusted
    to scale position size until WP-7 shows it is predictive.

    Precondition: validate() returned passed=True for this proposal. Callers
    must not invoke size() on a rejected proposal — est_max_loss is assumed
    finite and positive (enforced by MAX_LOSS_NOT_FINITE in the validator).

    Returns a SizingResult with contracts ≥ 0. contracts=0 with capped_to_zero=True
    means the orchestrator should record a SIZED_TO_ZERO cycle and skip execution.
    """
    # Step 1: conviction gate — below floor means no position regardless of budget.
    if proposal.conviction <= limits.conviction_floor:
        return SizingResult(
            contracts=0,
            sized_max_loss=0.0,
            sized_max_profit=0.0,
            risk_budget_used=0.0,
            binding_constraint=SizingConstraint.CONVICTION_FLOOR,
            capped_to_zero=True,
        )

    # Step 2: how many contracts fit in the per-trade risk budget?
    # Precondition: validate() must have passed before size() is called.
    # MAX_LOSS_NOT_FINITE ensures est_max_loss is finite and positive.
    assert proposal.est_max_loss > 0, (
        f"size() called with est_max_loss={proposal.est_max_loss!r}; "
        "validator must pass MAX_LOSS_NOT_FINITE check before sizing"
    )
    risk_budget = portfolio_state.account_equity * limits.max_loss_per_trade_pct
    contracts = math.floor(risk_budget / proposal.est_max_loss)

    # Step 3: even 1 contract exceeds the budget — size to zero, don't round up.
    # Flooring to 1 would override the risk budget; the correct action is no trade.
    if contracts == 0:
        return SizingResult(
            contracts=0,
            sized_max_loss=0.0,
            sized_max_profit=0.0,
            risk_budget_used=0.0,
            binding_constraint=SizingConstraint.BELOW_MIN_SIZE,
            capped_to_zero=True,
        )

    # Step 4: normal case — risk budget is the binding constraint.
    sized_max_loss = contracts * proposal.est_max_loss
    sized_max_profit = contracts * proposal.est_max_profit
    risk_budget_used = sized_max_loss / risk_budget

    return SizingResult(
        contracts=contracts,
        sized_max_loss=sized_max_loss,
        sized_max_profit=sized_max_profit,
        risk_budget_used=risk_budget_used,
        binding_constraint=SizingConstraint.RISK_BUDGET,
        capped_to_zero=False,
    )
