# Risk & Guardrails

**Module:** `options_agent/risk/`
**Credentials required:** none
**Status:** complete

The deterministic hard layer that every `TradeProposal` passes through before an order is submitted. No LLM, no live data — inputs are plain Python objects.

## Sub-modules

| File | Responsibility |
|---|---|
| `limits.py` | `Limits` model — all numeric thresholds, loaded from `config.toml` (`Config.from_toml(...).limits`, or `Limits()` for defaults) |
| `gates.py` | Pre-flight checks: market open, blackout windows, buying power, position cap |
| `validator.py` | Per-proposal validation — structural, market-access, and risk-cap checks |
| `sizing.py` | Conviction + risk budget → contract count |
| `structure.py` | Recomputes `est_max_loss`/`est_max_profit`/Greeks from proposal legs + chain quotes, overriding the agent's self-reported values before validation and sizing |

## Validator

Three validation layers, applied in order by the orchestrator. Each returns a `ValidationResult(passed, reasons)` where `reasons` is a list of structured `RejectionReason`s (`rule_id`, `message`) — every rejection is loggable and journaled.

- **`validate_structural(proposal, limits)`** — schema validity, playbook membership, naked-short rejection (unconditional). No market data needed.
- **`validate_market_access(proposal, portfolio, events, limits)`** — event-proximity blackout, buying-power floor, duplicate/conflict detection.
- **`validate_risk_caps(proposal, portfolio, limits)`** — max-loss cap, portfolio Greek bands, underlying concentration.

To see every rule ID and what trips it, browse `options_agent/tests/test_validator.py` — one hand-built fixture per rejection rule, each `test_reject_*` function a minimal example that trips exactly one rule.

## Sizing

`size(proposal, portfolio, limits) -> SizingResult` maps conviction + risk budget to a contract count. The result carries `contracts` (0 if capped), `capped_to_zero`, and `binding_constraint` (which limit bound the result).

## Pre-flight gates

Standalone functions called at the top of `run_entry_cycle()` before any data fetch, all returning `(bool, reason: str)`:

- `market_is_open(now, calendar)` and `within_blackout_window(now, calendar, open_mins, close_mins)` take an `exchange_calendars` calendar (`"XNYS"` by default).
- `has_buying_power(portfolio, limits)` and `under_position_cap(portfolio, limits)` take a `PortfolioState`.

## Structure metrics (don't trust LLM arithmetic)

The agent's `TradeProposal` carries self-reported `est_max_loss`, `est_max_profit`, and net Greeks. Nothing guarantees those numbers match the legs it proposed — in practice they have arrived as per-position totals or doubled values, corrupting sizing (see the incident-pattern regression tests in `options_agent/tests/test_structure.py`).

Every playbook strategy is a defined-risk, same-expiration structure, so its risk metrics are exactly computable from the legs and chain quotes via expiration-payoff analysis: a piecewise-linear payoff attains its extrema at the strike kinks, so evaluating P&L at each strike (plus the tails) gives exact max loss/profit — correctly handling asymmetric-wing structures where a naive "wing width" formula picks the wrong side.

The orchestrator calls `compute_structure_metrics` + `apply_structure_metrics` before `size()` and `validate_risk_caps()` on every entry-cycle proposal (including the retry path), so the sizer never consumes raw LLM arithmetic. Rules:

- Returns `None` (falls through to liquidity-check rejection) if any leg is absent from the chain.
- Greeks are always overridden; `est_max_loss`/`est_max_profit` only when the payoff analysis yields a finite positive bound — mixed-expiration or genuinely unbounded structures keep the agent's value.
- A >20% deviation between the agent's number and the computed one logs a warning (not yet journaled as a queryable field).
