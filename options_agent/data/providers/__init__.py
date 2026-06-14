"""Data provider abstraction layer.

DataProvider is the Protocol all market-data source adapters must satisfy.
AlpacaDataClient (alpaca_data.py) is the Alpaca implementation; a future
PolygonDataClient would implement the same Protocol.

Consumers (chains.py, greeks_iv.py, events.py, market.py) depend on
DataProvider — never on AlpacaDataClient directly. This keeps the swap to
Polygon a one-line injection change rather than a cross-file refactor.

The Protocol methods are shaped around what downstream consumers need, not
around Alpaca's wire format. Each adapter is responsible for translating its
provider's response format into these types.

Sequential-execution invariant (system-level, owned by WP-8):
  The entry and monitor loops MUST NOT run concurrently. This invariant
  keeps AlpacaDataClient free of thread-safety machinery. If the deployment
  model ever adds a second worker or subprocess, thread-safety must be added
  to the adapter before that change ships.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel


class DataUnavailableError(Exception):
    """Raised when a data fetch fails after all retry attempts are exhausted.

    Catch this at the orchestrator boundary and map it to a CycleError so the
    cycle is journaled rather than crashing the process. The caller should not
    treat this as recoverable within the same cycle.
    """

    def __init__(self, operation: str, symbol: str, cause: Exception) -> None:
        self.operation = operation
        self.symbol = symbol
        self.cause = cause
        super().__init__(f"{operation}({symbol!r}) unavailable after retries: {cause}")


class DataAuthError(Exception):
    """Raised when the provider rejects the supplied credentials (HTTP 401).

    This is not retried — bad credentials will not heal on their own. Catch at
    the top of the cycle, log, and halt (or trigger the kill switch).
    """


class RawOptionContract(BaseModel):
    """Provider-agnostic option contract snapshot from the market-data API.

    This is the bridge between a provider's wire format and the downstream
    WP-3 processing modules. It intentionally mirrors OptionContract (WP-0.2)
    but differs in two ways:

    1. Greek fields are Optional — not all providers guarantee them on every
       contract. WP-3.3 decides how to handle None (error / skip / compute).

    2. gamma and rho are included — Alpaca returns them; they are intentionally
       excluded from OptionContract (no per-row entry signal) but WP-3.3 may
       use them for plausibility checks.

    volume and open_interest: the Alpaca options-snapshot endpoint does not
    return cumulative daily volume or open interest. These are set to None
    by AlpacaDataClient. WP-3.2 must handle None — a contract with None OI
    must not be silently passed through a min_oi filter.
    """

    symbol: str
    underlying: str
    strike: float
    expiration: date
    right: Literal["call", "put"]
    bid: float | None
    ask: float | None
    volume: int | None
    open_interest: int | None
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    rho: float | None


@runtime_checkable
class DataProvider(Protocol):
    """Protocol all market-data provider adapters must satisfy.

    Implementations must be:
    - Long-lived: construct once per process; share across both loops.
    - Cycle-scoped cache: call begin_cycle() at the top of every
      run_entry_cycle() and run_monitor_cycle() to get fresh data.
    - Not thread-safe by default: the sequential-execution invariant
      (see module docstring) removes the need for locks. Add them if
      that invariant is ever relaxed.

    Raising conventions:
    - DataUnavailableError: retries exhausted; fetch failed.
    - DataAuthError: HTTP 401; do not retry.
    """

    def begin_cycle(self) -> None:
        """Clear the within-cycle cache.

        Must be called before the first data fetch in every cycle. Ensures
        each cycle sees a coherent, point-in-time snapshot rather than
        data that drifted during a long cycle or bled in from a prior one.
        """
        ...

    def fetch_option_chain(self, symbol: str) -> list[RawOptionContract]:
        """Return all option contracts for the given underlying symbol.

        Results are raw and unfiltered — WP-3.2 (chains.py) applies
        compaction and liquidity filtering to produce FilteredChain.
        """
        ...

    def fetch_latest_price(self, symbol: str) -> float:
        """Return the latest bar close price for the underlying equity."""
        ...
