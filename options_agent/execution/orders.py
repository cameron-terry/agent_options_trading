"""Order construction utilities for WP-1.2.

build_single_leg_request and compute_limit_price are the two primitives
consumed by BrokerClient.submit().  They live here rather than in broker.py
so multi-leg order construction (WP-1.3) can extend this module without
touching the broker wrapper.
"""

import uuid
from typing import Literal

from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from options_agent.contracts.proposal import Leg, TradeProposal


def occ_symbol(underlying: str, leg: Leg) -> str:
    """Build the OCC option symbol for a single leg.

    Format: {underlying}{YYMMDD}{C|P}{strike_in_thousandths:08d}
    Example: AAPL, $150.00 call expiring 2024-01-19 → "AAPL240119C00150000"

    The OCC standard encodes the strike as integer thousandths (× 1000),
    zero-padded to 8 digits.  $150.00 → 150000 → "00150000".
    Alpaca uses unpadded underlying root (no trailing spaces).
    """
    yy = leg.expiration.strftime("%y")
    mm = leg.expiration.strftime("%m")
    dd = leg.expiration.strftime("%d")
    right = "C" if leg.right == "call" else "P"
    strike_int = round(leg.strike * 1000)
    return f"{underlying}{yy}{mm}{dd}{right}{strike_int:08d}"


def compute_limit_price(
    bid: float,
    ask: float,
    side: Literal["buy", "sell"],
    offset_from_mid: float = 0.0,
) -> float:
    """Compute a limit price from bid/ask at mid ± slippage offset.

    offset_from_mid is a non-negative allowance toward fill:
      buy  → limit = mid + offset  (willing to pay a bit more than mid)
      sell → limit = mid - offset  (willing to accept a bit less than mid)

    Rounded to 2 decimal places (standard options tick size).
    """
    mid = (bid + ask) / 2.0
    if side == "buy":
        return round(mid + offset_from_mid, 2)
    else:
        return round(mid - offset_from_mid, 2)


def build_single_leg_request(
    proposal: TradeProposal,
    qty: int,
    limit_price: float,
    client_order_id: str | None = None,
) -> LimitOrderRequest:
    """Build an Alpaca LimitOrderRequest for a single-leg option proposal.

    Raises ValueError if the proposal has more than one leg.
    client_order_id is set to a fresh UUID when not supplied; the caller can
    pass a pre-generated value to enable idempotency checking on retry.
    """
    if len(proposal.legs) != 1:
        raise ValueError(
            f"build_single_leg_request requires exactly one leg; "
            f"got {len(proposal.legs)}."
        )
    leg = proposal.legs[0]
    symbol = occ_symbol(proposal.underlying, leg)
    side = OrderSide.BUY if leg.side == "buy" else OrderSide.SELL
    coid = client_order_id or str(uuid.uuid4())
    return LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        client_order_id=coid,
    )
