# Agent Tool Definitions & Mock Harness

**Module:** `options_agent/agent/`
**Credentials required:** none (mock harness); data credentials (real implementations)
**Status:** complete

Defines the six read-only Anthropic SDK tool schemas the agent uses each reasoning cycle, plus a deterministic mock harness so the reasoner can be developed and tested without live data.

## Files

| File | Responsibility |
|---|---|
| `agent/tools.py` | Tool name constants, `AGENT_TOOLS` list, `PositionHistory` model, `JOURNAL_MAX_RECORDS` |
| `agent/tools_mock.py` | Mock implementations for all 6 tools; `MOCK_TOOL_IMPLS` map |
| `agent/eval_scenarios.py` | `EvalScenario` fixtures for the prompt eval harness |

## Tool surface (read-only invariant)

All 6 tools are read-only — no tool can place, modify, or cancel an order (enforced by an inspection test).

| Tool name | Args | Returns |
|---|---|---|
| `get_portfolio_state` | _(none)_ | `PortfolioState` |
| `get_universe_snapshot` | _(none)_ | `UniverseSnapshot` |
| `get_filtered_chain` | `symbol`, `strategy_hint?` | `FilteredChain` |
| `get_events` | `symbols: list[str]` | `dict[str, EventInfo]` |
| `get_journal_by_symbol` | `symbol` | `list[JournalRecord]` (capped at `JOURNAL_MAX_RECORDS = 20` — `state/journal.py` imports and enforces the same constant) |
| `get_position_history` | `position_id` | `PositionHistory \| None` |

`AGENT_TOOLS` is ordered with no-argument, stable tools first so the tool-list prefix is prompt-cache-friendly — don't reorder casually. `PositionHistory` lives in `agent/tools.py` (an agent-tool concern, not a contract); its `opening_record=None` means a system anomaly — do not trade.

## Semantic conventions encoded in the schemas

The tool descriptions encode conventions the LLM must apply correctly:

- **`None` semantics in `get_universe_snapshot`:** `iv_rank: null` = warm-up / insufficient history → treat as **ineligible**, not as low IV. `days_to_earnings: null` = no earnings confirmed within the lookahead — not a guarantee of absence. `days_to_earnings: N` within `event_blackout_days` (default 5) will be rejected by the validator's `EVENT_BLACKOUT` rule.
- **Dollar-Greek units in `get_portfolio_state`:** `net_dollar_delta` = USD per $1 underlying move; `net_dollar_gamma` = delta change per $1 move; `net_dollar_theta` = USD decay per calendar day; `net_dollar_vega` = USD per 1 vol-point.
- **Delta signs in `get_filtered_chain`:** calls positive, puts negative; use `abs(delta)` for moneyness comparisons. `strategy_hint` narrows the chain to the relevant right(s) (puts-only, calls-only, or both for condors/butterflies).

## Mock harness

`MOCK_TOOL_IMPLS` (`dict[str, Callable]`) is importable from non-test code so the reasoner can run end-to-end, but it must never reach production — `reasoner.py` receives tool implementations by dependency injection with no default fallback to mocks.

The mock universe covers the three data states the real agent encounters — develop and test against all three, not just the happy path:

| Symbol | State |
|---|---|
| `SPY` | Clean, tradeable (`iv_rank=62.0`, no events) — plus one open bull-put-spread position sitting exactly at its 50% profit target, a deliberate portfolio-awareness test state |
| `AAPL` | Earnings in 5 days — inside the default blackout window; validator will reject any OPEN |
| `NVDA` | Warm-up (`iv_rank=None`) — must be treated as ineligible |

## Prompt eval harness

`eval_scenarios.py` packages the mocks into named `EvalScenario`s, each with **invariants** (must pass 100% of runs) and **preferences** (rate-based thresholds): high-IV neutral, low-IV bullish, earnings blackout (NO_ACTION expected), no IV history (NO_ACTION mandatory), and portfolio-aware. Requires `ANTHROPIC_API_KEY`; costs real API money per suite, so run deliberately — not on every push:

```bash
uv run pytest tests/evals/ -m eval --tb=line          # full suite
uv run pytest tests/evals/ -m eval -k A_high_iv_neutral --tb=line
```

(`--tb=line` keeps fixture values — which can embed context — out of failure tracebacks; see CLAUDE.md's secret-safety notes.)
