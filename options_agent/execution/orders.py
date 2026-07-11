"""Order construction utilities for WP-1.2 and WP-1.3.

Primitives consumed by BrokerClient.submit() and BrokerClient.submit_multi_leg():
  - compute_limit_price           — single-leg mid ± offset
  - compute_multi_leg_limit_price — sign-aware net combo mid, conservative rounding
  - build_single_leg_request      — LimitOrderRequest for one-leg proposals
  - build_multi_leg_request       — LimitOrderRequest (MLEG) for ≥2-leg proposals
"""

import math
import uuid
from typing import Literal

from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

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


def compute_multi_leg_limit_price(
    legs: list[Leg],
    quotes: list[tuple[float, float]],
    tick_size: float = 0.01,
    offset_toward_fill: float = 0.0,
) -> float:
    """Compute a net combo limit price at mid-or-better for a multi-leg order.

    Sign convention — matches Alpaca's mleg limit_price and Position.entry_net_amount:
        buy  legs contribute  +mid × ratio  (cost / debit we pay)
        sell legs contribute  -mid × ratio  (premium / credit we receive)
    A positive result is a net debit; a negative result is a net credit.

    Rounding is conservative — always toward a price more favourable to us:
        net debit  (positive): floor to tick  → pay slightly less than mid
        net credit (negative): floor to tick  → receive slightly more than mid
    In both cases: floor(net / tick_size) × tick_size.

    offset_toward_fill (non-negative) is a slippage allowance applied AFTER
    the conservative rounding, moving the limit toward the market so the
    order fills: a net debit pays up to offset more than mid; a net credit
    accepts up to offset less than mid. Both directions are net + offset
    (a credit is negative, so adding the offset moves it toward zero).

    This is the last validation gate before the order builder.  The chain
    filter (WP-3) is the primary liquidity screen; this function asserts
    sanity and fails loudly rather than silently pricing off a broken quote.

    Raises:
        ValueError: if len(quotes) != len(legs), any leg quote is
                    pathological (bid < 0, ask ≤ 0, or bid ≥ ask), or
                    offset_toward_fill is negative.
    """
    if offset_toward_fill < 0:
        raise ValueError(
            f"offset_toward_fill must be non-negative; got {offset_toward_fill}"
        )
    if len(quotes) != len(legs):
        raise ValueError(
            f"quotes length {len(quotes)} does not match legs length {len(legs)}"
        )

    net = 0.0
    for i, (leg, (bid, ask)) in enumerate(zip(legs, quotes)):
        if bid < 0 or ask <= 0:
            raise ValueError(
                f"Leg {i} ({leg.side} {leg.right} {leg.strike}): invalid quote "
                f"(bid={bid}, ask={ask}); both must be ≥ 0 with ask > 0."
            )
        if bid >= ask:
            raise ValueError(
                f"Leg {i} ({leg.side} {leg.right} {leg.strike}): pathological "
                f"quote (bid={bid} ≥ ask={ask}); refusing to price off an "
                "inverted or zero spread."
            )
        mid = (bid + ask) / 2.0
        sign = 1.0 if leg.side == "buy" else -1.0
        net += sign * mid * leg.ratio

    # floor(net / tick_size) rounds toward −∞ in both the debit and credit cases,
    # which is the conservative direction:
    #   positive net (debit)  → floor rounds down  → we pay less than mid.
    #   negative net (credit) → floor rounds more negative → we receive more than mid.
    # round(net / tick_size, 8) first to suppress floating-point noise at boundaries.
    ticks = math.floor(round(net / tick_size, 8))
    return round(ticks * tick_size + offset_toward_fill, 2)


def build_multi_leg_request(
    proposal: TradeProposal,
    qty: int,
    limit_price: float,
    client_order_id: str | None = None,
) -> LimitOrderRequest:
    """Build a LimitOrderRequest for a multi-leg (mleg) options combo order.

    qty is the base position size from SizingResult.quantity.  Alpaca computes
    the actual contract count per leg as qty × leg.ratio (ratio_qty), so a
    1×2 backspread with qty=5 submits 5 contracts on the ratio=1 leg and 10
    on the ratio=2 leg.  Never pre-multiply — pass the raw ratio and the
    sized quantity separately.

    limit_price is the net combo price (positive = net debit; negative = net
    credit), matching Alpaca's mleg convention and Position.entry_net_amount.
    Compute it with compute_multi_leg_limit_price().

    The order type is statically LIMIT via LimitOrderRequest; no code path in
    this module can produce a market order on an option.

    Alpaca constraints enforced here: 2–4 legs required.  Alpaca additionally
    requires all leg OCC symbols to be unique; duplicate legs in the proposal
    will be caught by the SDK validator on submission.

    Raises ValueError for < 2 or > 4 legs.
    """
    n = len(proposal.legs)
    if n < 2:
        raise ValueError(
            f"build_multi_leg_request requires at least 2 legs; got {n}. "
            "Use build_single_leg_request for single-leg proposals."
        )
    if n > 4:
        raise ValueError(
            f"build_multi_leg_request requires at most 4 legs (Alpaca limit); got {n}."
        )

    coid = client_order_id or str(uuid.uuid4())
    leg_requests = [
        OptionLegRequest(
            symbol=occ_symbol(proposal.underlying, leg),
            ratio_qty=float(leg.ratio),
            side=OrderSide.BUY if leg.side == "buy" else OrderSide.SELL,
        )
        for leg in proposal.legs
    ]

    return LimitOrderRequest(
        qty=qty,
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        legs=leg_requests,
        client_order_id=coid,
    )


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
