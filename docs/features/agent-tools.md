# Agent Tool Definitions & Mock Harness

**Module:** `options_agent/agent/`  
**Credentials required:** none (mock harness); WP-3 data credentials (real implementations)  
**Status:** complete (WP-6.1) — schemas and mock harness ready for WP-6.4 prompt engineering

Defines the six read-only Anthropic SDK tool schemas the agent uses at each reasoning cycle, and provides a deterministic mock harness so WP-6.4 (`reasoner.py`) can be developed and tested without live data.

## Files

| File | Responsibility |
|---|---|
| `agent/tools.py` | Tool name constants, `AGENT_TOOLS` list, `PositionHistory` model, `JOURNAL_MAX_RECORDS` |
| `agent/tools_mock.py` | Mock implementations for all 6 tools; `MOCK_TOOL_IMPLS` map |
| `tests/test_tools.py` | 42 tests: schema structure, mock type-correctness, P&L arithmetic, end-to-end dispatch |

## Tool surface (read-only invariant)

All 6 tools are read-only. No tool in `AGENT_TOOLS` can place, modify, or cancel an order. This is enforced by an inspection test in `test_tools.py`.

| Tool name | Args | Returns |
|---|---|---|
| `get_portfolio_state` | _(none)_ | `PortfolioState` |
| `get_universe_snapshot` | _(none)_ | `UniverseSnapshot` |
| `get_filtered_chain` | `symbol`, `strategy_hint?` | `FilteredChain` |
| `get_events` | `symbols: list[str]` | `dict[str, EventInfo]` |
| `get_journal_by_symbol` | `symbol` | `list[JournalRecord]` |
| `get_position_history` | `position_id` | `PositionHistory \| None` |

## Imports from tools.py

```python
from options_agent.agent.tools import (
    AGENT_TOOLS,           # list[ToolParam] — pass directly to the Anthropic SDK
    AGENT_TOOL_NAMES,      # frozenset[str] — use to validate tool call names at dispatch
    JOURNAL_MAX_RECORDS,   # int = 20 — WP-2 must enforce the same limit
    PositionHistory,       # Pydantic model returned by get_position_history
    TOOL_GET_PORTFOLIO_STATE,
    TOOL_GET_UNIVERSE_SNAPSHOT,
    TOOL_GET_FILTERED_CHAIN,
    TOOL_GET_EVENTS,
    TOOL_GET_JOURNAL_BY_SYMBOL,
    TOOL_GET_POSITION_HISTORY,
)
```

## Tool ordering and prompt caching

`AGENT_TOOLS` is ordered with no-argument, stable tools first (`get_portfolio_state`, `get_universe_snapshot`) so the tool-list prefix is cache-friendly. Do not reorder without considering the Anthropic prompt cache TTL (5 minutes).

## Description conventions encoded in the schemas

The tool descriptions encode WP-0 conventions the LLM must apply correctly. Key items:

**`None` semantics in `get_universe_snapshot`:**
- `iv_rank: null` / `iv_percentile: null` — warm-up period; insufficient IV history. Treat as **ineligible**, not as low IV or zero IV.
- `days_to_earnings: null` — no earnings confirmed within the lookahead window. **Not** a guarantee of absence.
- `days_to_earnings: N` — earnings confirmed N calendar days out. The validator's `EVENT_BLACKOUT` rule rejects OPEN proposals when `N < event_blackout_days` (default 5).

**Dollar-Greek units in `get_portfolio_state`:**
- `net_dollar_delta` — USD change per $1 move in the underlying
- `net_dollar_gamma` — USD change in delta per $1 move in the underlying
- `net_dollar_theta` — USD time decay per calendar day across all positions
- `net_dollar_vega` — USD change per 1 vol-point (1 pct-point) move in IV

**Delta sign convention in `get_filtered_chain`:**
- Call deltas: positive (0 to 1). Put deltas: negative (-1 to 0). Use `abs(delta)` when reasoning about moneyness or comparing to the configured delta range.

**`strategy_hint` in `get_filtered_chain`:**
- Puts only: `bull_put_spread`, `bear_put_spread`, `cash_secured_put`
- Calls only: `bear_call_spread`, `bull_call_spread`, `covered_call`
- Both rights: `iron_condor`, `iron_butterfly`
- Omit to receive both rights with the full delta window.

## PositionHistory model

`PositionHistory` is defined in `agent/tools.py` (not in `contracts/`) because it is an agent-tool concern.

```python
class PositionHistory(BaseModel):
    opening_record: JournalRecord | None   # None = system anomaly; do not trade
    outcome_records: list[OutcomeRecord]   # empty while position is still open
```

## Using the mock harness

The mock harness is in `agent/tools_mock.py`. It is importable from non-test code so WP-6.4 can run `reasoner.py` end-to-end, but **it must never reach a production code path**. `reasoner.py` receives tool implementations by dependency injection — there is no default fallback to mocks.

```python
from options_agent.agent.tools_mock import MOCK_TOOL_IMPLS

# MOCK_TOOL_IMPLS is: dict[str, Callable[[dict[str, Any]], Any]]
# Pass it to the reasoner harness by DI:
result = reason(context, tool_impls=MOCK_TOOL_IMPLS)

# Dispatching a single call manually:
impl = MOCK_TOOL_IMPLS["get_universe_snapshot"]
snapshot = impl({})
```

### Mock universe — three representative data states

The mock universe covers the three distinct states the real agent will encounter:

| Symbol | State | Key signals |
|---|---|---|
| `SPY` | Clean, tradeable | `iv_rank=62.0`, `days_to_earnings=None`, no events |
| `AAPL` | Earnings approaching | `days_to_earnings=5` — within the default blackout window; validator will `EVENT_BLACKOUT` any OPEN proposal |
| `NVDA` | Warm-up period | `iv_rank=None`, `iv_percentile=None` — insufficient IV history; agent must treat as ineligible |

WP-6.4 must develop and test against all three states, not only the SPY happy-path scenario.

### Mock position (SPY bull put spread at 50% profit target)

The mock portfolio holds one open position:

```
Strategy:      bull_put_spread on SPY
Legs:          sell 530P / buy 525P, 2 contracts, expiry 2026-07-18 (~34 DTE)
Entry credit:  $2.70 (= (2.45 - 1.10) × 2 contracts)
Current mark:  $1.35 (halfway to worthless)
Unrealized:    $135.0 (= (2.70 - 1.35) × 100)
Est max loss:  $500.0 | Est max profit: $270.0
```

This places the position exactly at the 50% profit target, which is the default `profit_target_pct` in `ExitPlan`. It is a deliberate WP-6.4 test state: the agent should consider rolling or closing.

### Mock journal

`get_journal_by_symbol("SPY")` returns one `JournalRecord` (`action_taken=OPENED`, `cycle_id="cycle-20260607-001"`). AAPL and NVDA return empty lists. Unknown symbols also return empty lists.

`get_position_history("pos-001")` returns the `PositionHistory` for the open SPY position with `outcome_records=[]` (no exit events yet). Unknown position IDs return `None`.

## Cross-WP alignment notes

**WP-2 (`state/journal.py`):** `JOURNAL_MAX_RECORDS = 20` is defined in `agent/tools.py` and imported by `tools_mock.py`. WP-2 must import and enforce this same constant in `query_journal` — it must not hard-code 20 or rely on prose documentation.

**WP-3.5 (events data):** The `get_events` schema accepts `symbols: list[str]` (batch). WP-3.5 currently describes a single-symbol `get_events(symbol)` interface. WP-3.5 must either implement a batch interface directly, or the dispatch layer in `reasoner.py` must fan out individual calls and reassemble the `dict[str, EventInfo]` result.
