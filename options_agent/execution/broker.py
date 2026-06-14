import logging
import os
import uuid
from datetime import UTC, datetime
from time import monotonic, sleep
from typing import Any, cast

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.models import TradeAccount
from alpaca.trading.requests import GetOrdersRequest

from options_agent.config import Config
from options_agent.contracts.proposal import TradeProposal
from options_agent.contracts.state import LegFill, Order, OrderRole, OrderStatus
from options_agent.execution.orders import (
    build_multi_leg_request,
    build_single_leg_request,
)

logger = logging.getLogger(__name__)

# Alpaca status strings that signal no further state changes are coming.
_TERMINAL = frozenset(
    {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"}
)

# Mapping from Alpaca status strings to our canonical OrderStatus enum.
# Public so reconcile.py can reuse without coupling to broker internals.
STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.WORKING,
    "pending_new": OrderStatus.WORKING,
    "accepted": OrderStatus.WORKING,
    "accepted_for_bidding": OrderStatus.WORKING,
    "pending_review": OrderStatus.WORKING,
    "held": OrderStatus.WORKING,
    "stopped": OrderStatus.WORKING,
    "suspended": OrderStatus.WORKING,
    "calculated": OrderStatus.WORKING,
    "pending_cancel": OrderStatus.WORKING,
    "pending_replace": OrderStatus.WORKING,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "done_for_day": OrderStatus.CANCELLED,
    "replaced": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}

# Exponential back-off delays for 429 retries (seconds).
_RATE_LIMIT_DELAYS = [1.0, 2.0, 4.0]


class BrokerClient:
    """Execution-only Alpaca broker wrapper.

    Owns the authenticated TradingClient (orders, account, positions).
    Does not import or touch the DB — all state writes happen at the
    caller boundary through WP-2's state interface.

    Credentials (ALPACA_API_KEY / ALPACA_SECRET_KEY) are read from the
    environment at construction time and never stored or logged.
    Config supplies non-secret settings only (alpaca_paper flag, poll
    timing, limit-price offset).
    """

    def __init__(self, config: Config) -> None:
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret_key = os.environ.get("ALPACA_SECRET_KEY", "")

        missing = [
            name
            for name, value in (
                ("ALPACA_API_KEY", api_key),
                ("ALPACA_SECRET_KEY", secret_key),
            )
            if not value
        ]
        if missing:
            raise OSError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Set them before constructing BrokerClient."
            )

        try:
            self._client = TradingClient(api_key, secret_key, paper=config.alpaca_paper)
        except Exception as exc:
            logger.error(
                "Alpaca TradingClient failed to initialise. "
                "Check that credentials are valid (key values withheld from log)."
            )
            raise RuntimeError(
                "Alpaca TradingClient failed to initialise; "
                "check credentials are valid."
            ) from exc

        self._config = config
        self._is_paper = config.alpaca_paper

    @property
    def is_paper(self) -> bool:
        """True when this client is connected to Alpaca paper trading."""
        return self._is_paper

    def get_account(self) -> TradeAccount:
        """Read account information from Alpaca."""
        return cast(TradeAccount, self._client.get_account())

    # ------------------------------------------------------------------
    # Order submission (WP-1.2)
    # ------------------------------------------------------------------

    def submit(
        self,
        proposal: TradeProposal,
        qty: int,
        limit_price: float,
        position_id: str,
        role: OrderRole = OrderRole.OPEN,
    ) -> Order:
        """Submit a single-leg limit option order and poll for fill status.

        Returns an Order whose status is one of:
          FILLED           — fully filled within the poll timeout
          PARTIALLY_FILLED — partially filled at timeout; unfilled remainder
                             stays live at the broker
          WORKING          — no fill detected within the poll timeout; order
                             is still live at the broker
          REJECTED         — broker rejected the order synchronously

        CONTRACT: This method may return a live WORKING or PARTIALLY_FILLED
        order.  The caller owns any unfilled working order and must track it
        through reconcile (WP-1.4).  submit() never unilaterally cancels.

        limit_price must be pre-computed by the caller; use
        orders.compute_limit_price(bid, ask, leg.side,
                                   config.order_limit_offset_from_mid).

        Transport errors (after retries) are raised as exceptions.
        Broker rejections are returned as Order(status=REJECTED), not raised.

        Raises ValueError for multi-leg proposals (WP-1.3 scope).
        """
        if len(proposal.legs) != 1:
            raise ValueError(
                f"submit() handles single-leg proposals only; "
                f"got {len(proposal.legs)} legs."
            )

        client_order_id = str(uuid.uuid4())
        request = build_single_leg_request(proposal, qty, limit_price, client_order_id)

        alpaca_order = self._submit_with_retry(request, client_order_id)
        broker_id = str(alpaca_order.id)

        alpaca_order = self._poll_order(broker_id)
        return self._build_order(alpaca_order, position_id, role, limit_price, proposal)

    def _submit_with_retry(self, request: object, client_order_id: str) -> AlpacaOrder:
        """Call submit_order with retry for rate-limits and session expiry.

        Retry semantics:
          429 — sleep immediately after the 429 (honouring Retry-After if
                present), then retry; raises after _RATE_LIMIT_DELAYS exhausted.
          401 — attempts one re-auth then retries once; raises on second 401.
                Does not consume a rate-limit retry slot.

        Only retries before an order_id is known.  If submit_order() returns
        successfully, the returned AlpacaOrder is forwarded unchanged.
        """
        reauthed = False
        rate_attempt = 0
        while rate_attempt <= len(_RATE_LIMIT_DELAYS):
            try:
                result = self._client.submit_order(request)  # type: ignore[arg-type]
                return cast(AlpacaOrder, result)
            except APIError as exc:
                code = exc.status_code
                if code == 429:
                    if rate_attempt >= len(_RATE_LIMIT_DELAYS):
                        logger.error(
                            "Rate-limit (429) persists after %d retries; "
                            "client_order_id=%s",
                            len(_RATE_LIMIT_DELAYS),
                            client_order_id,
                        )
                        raise
                    # Honour Retry-After if Alpaca sends one; fall back to
                    # the exponential back-off table otherwise.
                    retry_after: float | None = None
                    if exc.response is not None:
                        raw = exc.response.headers.get("Retry-After")
                        if raw:
                            try:
                                retry_after = float(raw)
                            except ValueError:
                                pass
                    delay = (
                        retry_after
                        if retry_after is not None
                        else _RATE_LIMIT_DELAYS[rate_attempt]
                    )
                    logger.warning(
                        "Rate-limit (429) on attempt %d; sleeping %.1fs "
                        "(client_order_id=%s)",
                        rate_attempt + 1,
                        delay,
                        client_order_id,
                    )
                    sleep(delay)
                    rate_attempt += 1
                elif code == 401:
                    if reauthed:
                        logger.error(
                            "Session expired (401) after re-auth; client_order_id=%s",
                            client_order_id,
                        )
                        raise
                    logger.warning(
                        "Session expired (401); re-initialising TradingClient "
                        "(client_order_id=%s)",
                        client_order_id,
                    )
                    self._reinit_client()
                    reauthed = True
                    # 401 does not count against the rate-limit budget.
                else:
                    raise
        # Unreachable — the loop always raises or returns before exhaustion.
        raise RuntimeError(  # pragma: no cover
            "_submit_with_retry: unexpected loop exit"
        )

    def _poll_order(self, broker_id: str) -> AlpacaOrder:
        """Poll get_order_by_id until terminal status or timeout.

        Always performs at least one poll.  Returns the order in its current
        state regardless of whether a terminal status was reached — the caller
        should not assume FILLED; check Order.status.

        Raises APIError only if the deadline-expiry final fetch also fails
        (transient errors mid-loop are swallowed and retried until deadline).
        """
        deadline = monotonic() + self._config.order_poll_timeout_secs
        while True:
            try:
                alpaca_order = cast(
                    AlpacaOrder, self._client.get_order_by_id(broker_id)
                )
            except APIError as exc:
                logger.warning(
                    "Transient error polling order %s: %s; will retry",
                    broker_id,
                    exc,
                )
                # Still consume time; fall through to timeout/sleep check.
                alpaca_order = None  # type: ignore[assignment]

            if alpaca_order is not None:
                status_str = str(alpaca_order.status.value)
                if status_str in _TERMINAL:
                    return alpaca_order
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return alpaca_order
                sleep(min(self._config.order_poll_interval_secs, remaining))
            else:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    # Deadline expired while fetch was failing — one last attempt.
                    # May raise APIError; callers must handle it.
                    return cast(AlpacaOrder, self._client.get_order_by_id(broker_id))
                sleep(min(self._config.order_poll_interval_secs, remaining))

    def _build_order(
        self,
        alpaca_order: AlpacaOrder,
        position_id: str,
        role: OrderRole,
        limit_price: float,
        proposal: TradeProposal,
    ) -> Order:
        """Map an Alpaca order response to our local Order model."""
        leg = proposal.legs[0]
        status_str = str(alpaca_order.status.value)
        status = STATUS_MAP.get(status_str, OrderStatus.WORKING)

        filled_qty = int(alpaca_order.filled_qty or 0)
        fill_price_raw = alpaca_order.filled_avg_price
        fill_price = float(fill_price_raw) if fill_price_raw is not None else 0.0

        legs_filled: list[LegFill] = []
        if filled_qty > 0:
            legs_filled.append(
                LegFill(leg=leg, filled_qty=filled_qty, fill_price=fill_price)
            )

        submitted_at = alpaca_order.submitted_at or datetime.now(UTC)

        return Order(
            id=str(uuid.uuid4()),
            broker_order_id=str(alpaca_order.id),
            position_id=position_id,
            role=role,
            status=status,
            broker_status_raw=status_str,
            submitted_at=submitted_at,
            filled_at=alpaca_order.filled_at,
            limit_price=limit_price,
            legs_filled=legs_filled,
            net_fill_price=fill_price if filled_qty > 0 else None,
            filled_qty=filled_qty,
        )

    # ------------------------------------------------------------------
    # Multi-leg order submission (WP-1.3)
    # ------------------------------------------------------------------

    def submit_multi_leg(
        self,
        proposal: TradeProposal,
        qty: int,
        limit_price: float,
        position_id: str,
        role: OrderRole = OrderRole.OPEN,
    ) -> Order:
        """Submit a multi-leg (mleg) limit option order and poll for fill status.

        qty is the base position size; Alpaca multiplies by each leg's ratio_qty
        to compute the per-leg contract count.  Verify on paper before first use
        that filled quantities match intent (qty × ratio per leg).

        limit_price is the net combo price (positive = debit, negative = credit),
        matching Alpaca's mleg convention.  Compute with
        orders.compute_multi_leg_limit_price(legs, quotes).

        Returns the same status semantics as submit() — FILLED, PARTIALLY_FILLED,
        WORKING, or REJECTED.  See submit() docstring for the WORKING contract.

        CONTRACT: Entry orders that are WORKING at poll timeout must be cancelled
        before the next entry cycle.  Cancellation policy is owned by WP-1.4
        (reconcile) and WP-8 (orchestration).  This method never unilaterally
        cancels.

        net_fill_price on the returned Order is Alpaca's filled_avg_price for
        the combo (positive = net debit paid, negative = net credit received),
        consistent with Position.entry_net_amount.  Per-leg fill prices are not
        populated here; WP-1.4 reconcile fills them via a nested Alpaca fetch.

        Raises ValueError if the proposal has fewer than 2 legs.
        """
        if len(proposal.legs) < 2:
            raise ValueError(
                f"submit_multi_leg() requires at least 2 legs; "
                f"got {len(proposal.legs)}. Use submit() for single-leg proposals."
            )

        client_order_id = str(uuid.uuid4())
        request = build_multi_leg_request(proposal, qty, limit_price, client_order_id)

        alpaca_order = self._submit_with_retry(request, client_order_id)
        broker_id = str(alpaca_order.id)

        alpaca_order = self._poll_order(broker_id)
        return self._build_order_multi_leg(
            alpaca_order, position_id, role, limit_price, proposal
        )

    def _build_order_multi_leg(
        self,
        alpaca_order: AlpacaOrder,
        position_id: str,
        role: OrderRole,
        limit_price: float,
        proposal: TradeProposal,
    ) -> Order:
        """Map an Alpaca mleg order response to our local Order model.

        legs_filled is left empty — per-leg fill prices are not available from
        the parent-order poll response.  WP-1.4 reconcile populates them via
        a nested Alpaca fetch (get_order_by_id with nested=True) after the
        order settles.

        filled_qty is the number of combo units filled (not per-leg contracts).
        net_fill_price is Alpaca's filled_avg_price for the combo: positive =
        net debit paid, negative = net credit received, consistent with
        Position.entry_net_amount.
        """
        status_str = str(alpaca_order.status.value)
        status = STATUS_MAP.get(status_str, OrderStatus.WORKING)

        filled_qty = int(alpaca_order.filled_qty or 0)
        fill_price_raw = alpaca_order.filled_avg_price
        net_fill_price = (
            float(fill_price_raw)
            if fill_price_raw is not None and filled_qty > 0
            else None
        )

        submitted_at = alpaca_order.submitted_at or datetime.now(UTC)

        return Order(
            id=str(uuid.uuid4()),
            broker_order_id=str(alpaca_order.id),
            position_id=position_id,
            role=role,
            status=status,
            broker_status_raw=status_str,
            submitted_at=submitted_at,
            filled_at=alpaca_order.filled_at,
            limit_price=limit_price,
            legs_filled=[],
            net_fill_price=net_fill_price,
            filled_qty=filled_qty,
        )

    # ------------------------------------------------------------------
    # Order queries (WP-1.4 reconcile)
    # ------------------------------------------------------------------

    def list_open_orders(self) -> list[AlpacaOrder]:
        """Return all non-terminal orders currently known to Alpaca.

        Used by reconcile to detect orphans (broker orders with no local record)
        and to avoid a per-order round-trip for orders that are still open.
        Alpaca caps page size at 500; for larger portfolios a paginated fetch
        would be needed, but 500 far exceeds any reasonable position count for
        this system.
        """
        results = self._client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        )
        return list(results)  # type: ignore[arg-type]

    def get_broker_order(self, broker_order_id: str) -> AlpacaOrder | None:
        """Fetch a single order by its broker ID; returns None if not found.

        Used by reconcile to retrieve terminal status for orders that have
        dropped off the open-orders list since the last pass.
        """
        try:
            return cast(AlpacaOrder, self._client.get_order_by_id(broker_order_id))
        except APIError as exc:
            if exc.status_code == 404:
                return None
            raise

    # ------------------------------------------------------------------
    # Position and activity queries (WP-1.5 expiry/assignment detection)
    # ------------------------------------------------------------------

    def get_all_positions(self) -> list[AlpacaPosition]:
        """Return all open positions currently held at the broker.

        Used by the WP-1.5 absence backstop: a DB position whose option legs
        are absent from this list and whose nearest_expiration is in the past
        is a candidate for expiry marking.
        """
        results = self._client.get_all_positions()
        return list(results)  # type: ignore[arg-type]

    def get_account_activities(
        self,
        activity_types: list[str],
        after: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return account activity records filtered by type.

        Uses TradingClient.get() directly because the alpaca-py SDK does not
        yet expose a typed get_activities() method on TradingClient.
        Migrate to the SDK method if/when it is added (watch alpaca-py releases).

        activity_types — e.g. ["OPEXP", "OPASN"]
        after          — only return activities after this UTC datetime (ISO 8601)

        Returns a list of raw dicts from the Alpaca /v2/account/activities
        endpoint.  Relevant keys: activity_type, symbol, date, qty, price,
        description, net_amount.
        """
        params: dict[str, Any] = {
            "activity_types": ",".join(activity_types),
        }
        if after is not None:
            params["after"] = after.isoformat()

        result = self._client.get("/account/activities", params)  # type: ignore[attr-defined]
        if result is None:
            return []
        return list(result) if isinstance(result, list) else [result]

    # ------------------------------------------------------------------
    # Order cancellation (WP-1.6)
    # ------------------------------------------------------------------

    def cancel(self, order: Order) -> Order:
        """Cancel an open order at Alpaca and return the updated Order.

        Takes the caller's local Order (which supplies position_id, role, id,
        submitted_at, limit_price — fields the broker never knows) and returns
        an updated Order with broker-authoritative fields overlaid: status,
        broker_status_raw, filled_at, legs_filled, net_fill_price, filled_qty.

        Cancel is idempotent: retrying after a transport timeout carries no
        duplicate-order risk, unlike submit.

        IMPORTANT — the returned status is the order's TRUE current state, not
        a guaranteed CANCELLED:

          CANCELLED / EXPIRED  — cancel succeeded or order was already terminal.
          FILLED               — fill raced the cancel; a real position now exists.
                                 Callers must treat this as a live position, not a
                                 closed loop.  This is the broker-as-source-of-truth
                                 invariant applied to cancellation.

        If Alpaca returns 422 (order already in a terminal state), we re-fetch
        the current state and return it — no exception is raised.

        For multi-leg orders that end up FILLED, legs_filled is empty here; WP-1.4
        reconcile populates per-leg fills via a nested fetch, consistent with
        submit_multi_leg().

        Transport errors (after retries) are raised as exceptions.
        """
        try:
            self._cancel_with_retry(order.broker_order_id)
        except APIError as exc:
            if exc.status_code == 422:
                # Order is already in a terminal state — return whatever the
                # broker actually holds rather than raising.
                alpaca_order = self.get_broker_order(order.broker_order_id)
                if alpaca_order is None:
                    # 422 then 404 is unexpected; re-raise the original error.
                    raise
                return self._overlay_broker_state(order, alpaca_order)
            raise

        # cancel_order_by_id returns None (204 No Content); fetch to get state.
        alpaca_order = self.get_broker_order(order.broker_order_id)
        if alpaca_order is None:
            raise RuntimeError(
                f"Order {order.broker_order_id!r} not found immediately after cancel; "
                "broker may have cleaned it up before the fetch."
            )
        return self._overlay_broker_state(order, alpaca_order)

    def _cancel_with_retry(self, broker_order_id: str) -> None:
        """Call cancel_order_by_id with retry for rate-limits and session expiry.

        Cancel is idempotent — transport retries carry no duplicate-order risk.
        Retry semantics mirror _submit_with_retry: 429 uses exponential back-off
        (honouring Retry-After when present); 401 triggers one re-auth then retries.
        422 (already terminal) is NOT retried here; cancel() handles it.
        """
        reauthed = False
        rate_attempt = 0
        while rate_attempt <= len(_RATE_LIMIT_DELAYS):
            try:
                self._client.cancel_order_by_id(broker_order_id)
                return
            except APIError as exc:
                code = exc.status_code
                if code == 429:
                    if rate_attempt >= len(_RATE_LIMIT_DELAYS):
                        logger.error(
                            "Rate-limit (429) persists after %d retries; "
                            "broker_order_id=%s",
                            len(_RATE_LIMIT_DELAYS),
                            broker_order_id,
                        )
                        raise
                    retry_after: float | None = None
                    if exc.response is not None:
                        raw = exc.response.headers.get("Retry-After")
                        if raw:
                            try:
                                retry_after = float(raw)
                            except ValueError:
                                pass
                    delay = (
                        retry_after
                        if retry_after is not None
                        else _RATE_LIMIT_DELAYS[rate_attempt]
                    )
                    logger.warning(
                        "Rate-limit (429) on cancel attempt %d; sleeping %.1fs "
                        "(broker_order_id=%s)",
                        rate_attempt + 1,
                        delay,
                        broker_order_id,
                    )
                    sleep(delay)
                    rate_attempt += 1
                elif code == 401:
                    if reauthed:
                        logger.error(
                            "Session expired (401) after re-auth on cancel; "
                            "broker_order_id=%s",
                            broker_order_id,
                        )
                        raise
                    logger.warning(
                        "Session expired (401); re-initialising TradingClient "
                        "(broker_order_id=%s)",
                        broker_order_id,
                    )
                    self._reinit_client()
                    reauthed = True
                else:
                    raise
        raise RuntimeError(  # pragma: no cover
            "_cancel_with_retry: unexpected loop exit"
        )

    def _overlay_broker_state(
        self, local_order: Order, alpaca_order: AlpacaOrder
    ) -> Order:
        """Return a new Order with broker fields overlaid on the local Order.

        Broker-authoritative (from alpaca_order): status, broker_status_raw,
        filled_at, net_fill_price, filled_qty.
        Local (from local_order): id, broker_order_id, position_id, role,
        submitted_at, limit_price.

        legs_filled is always empty here — callers that need per-leg fill prices
        for multi-leg orders must use WP-1.4 reconcile (nested get_order_by_id).
        """
        status_str = str(alpaca_order.status.value)
        status = STATUS_MAP.get(status_str, OrderStatus.WORKING)

        filled_qty = int(alpaca_order.filled_qty or 0)
        fill_price_raw = alpaca_order.filled_avg_price
        net_fill_price = (
            float(fill_price_raw)
            if fill_price_raw is not None and filled_qty > 0
            else None
        )

        return Order(
            id=local_order.id,
            broker_order_id=local_order.broker_order_id,
            position_id=local_order.position_id,
            role=local_order.role,
            status=status,
            broker_status_raw=status_str,
            submitted_at=local_order.submitted_at,
            filled_at=alpaca_order.filled_at,
            limit_price=local_order.limit_price,
            legs_filled=[],
            net_fill_price=net_fill_price,
            filled_qty=filled_qty,
        )

    def _reinit_client(self) -> None:
        """Re-initialise TradingClient from current environment credentials."""
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
        if not api_key or not secret_key:
            raise OSError(
                "Cannot re-auth: ALPACA_API_KEY / ALPACA_SECRET_KEY missing "
                "from environment."
            )
        self._client = TradingClient(api_key, secret_key, paper=self._is_paper)
