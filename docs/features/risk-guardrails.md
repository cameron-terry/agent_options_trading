# Risk & Guardrails

**Module:** `options_agent/risk/`  
**Credentials required:** none  
**Status:** complete (WP-4)

The deterministic hard layer that every `TradeProposal` passes through before an order is submitted. No LLM, no live data — inputs are plain Python objects.

## Sub-modules

| File | Responsibility |
|---|---|
| `limits.py` | `Limits` model — all numeric thresholds, loaded from `config.toml` |
| `gates.py` | Pre-flight checks: market open, blackout windows, buying power, position cap |
| `validator.py` | Per-proposal validation — structural, market-access, and risk-cap checks |
| `sizing.py` | Conviction + risk budget → contract count |

## Loading limits from config

```python
from pathlib import Path
from options_agent.config import Config

config = Config.from_toml(Path("config.toml"))
limits = config.limits       # fully typed Limits object
```

Or use defaults without a file:

```python
from options_agent.risk.limits import Limits
limits = Limits()
```

## Validator

Three validation layers are applied in order by the orchestrator. Each returns a `ValidationResult(passed, reasons)`.

```python
from pathlib import Path
from options_agent.config import Config
from options_agent.agent.stub_reasoner import stub_reasoner
from options_agent.risk.validator import (
    validate_structural,
    validate_market_access,
    validate_risk_caps,
)

config = Config.from_toml(Path("config.toml"))
limits = config.limits
proposal = stub_reasoner()

result = validate_structural(proposal, limits)
print(result.passed)        # True / False
print(result.reasons)       # list[RejectionReason] — empty if passed

for r in result.reasons:
    print(r.rule_id, r.message)
```

**`validate_structural`** — schema validity, playbook membership, naked-short rejection. No market data needed.

**`validate_market_access`** — event proximity blackout, buying power floor, duplicate/conflict detection. Requires a `PortfolioState` and `list[EventInfo]`.

**`validate_risk_caps`** — max-loss cap, portfolio Greek bands, underlying concentration. Requires a `PortfolioState`.

### Triggering specific rejections

The test suite in [options_agent/tests/test_validator.py](../../options_agent/tests/test_validator.py) has one hand-built fixture per rejection rule. To see all rule IDs and what triggers them, browse the test file — each `test_reject_*` function is a minimal example that trips exactly one rule.

```python
# The stub proposal is designed to pass structural validation:
result = validate_structural(proposal, limits)
print(result.passed)   # True

# To exercise a rejection rule interactively, copy the relevant fixture from
# test_validator.py and call validate_structural (or validate_risk_caps /
# validate_market_access) against it.
```

## Sizing

```python
from pathlib import Path
from options_agent.config import Config
from options_agent.agent.stub_reasoner import stub_reasoner
from options_agent.risk.sizing import size
from options_agent.contracts.data import PortfolioState

config = Config.from_toml(Path("config.toml"))
proposal = stub_reasoner()

portfolio = PortfolioState(
    positions=[],
    account_equity=50_000.0,
    buying_power=25_000.0,
    options_buying_power=25_000.0,
    unrealized_pnl=0.0,
    realized_pnl_today=0.0,
    approval_level=2,
    net_dollar_delta=0.0,
    net_dollar_gamma=0.0,
    net_dollar_theta=0.0,
    net_dollar_vega=0.0,
)

sizing = size(proposal, portfolio, config.limits)
print(sizing.contracts)           # int — 0 if capped
print(sizing.capped_to_zero)      # True if conviction or budget forced to zero
print(sizing.binding_constraint)  # which limit bound the result
```

## Pre-flight gates

Gates are standalone functions — no proposal needed. They're called at the top of `run_entry_cycle()` before any data fetch.

`market_is_open` and `within_blackout_window` take an `exchange_calendars` calendar object (not `Config` directly). `has_buying_power` and `under_position_cap` take a `PortfolioState`.

```python
import exchange_calendars as xcals
from datetime import datetime, UTC
from pathlib import Path
from options_agent.config import Config
from options_agent.contracts.data import PortfolioState
from options_agent.risk.gates import (
    market_is_open,
    within_blackout_window,
    has_buying_power,
    under_position_cap,
)

config = Config.from_toml(Path("config.toml"))
calendar = xcals.get_calendar(config.exchange_calendar)   # "XNYS" by default
now = datetime.now(UTC)

is_open, reason = market_is_open(now, calendar)
print(is_open, reason)

in_blackout, reason = within_blackout_window(
    now, calendar,
    config.session_open_blackout_minutes,
    config.session_close_blackout_minutes,
)
print(in_blackout, reason)

portfolio = PortfolioState(
    positions=[],
    account_equity=50_000.0,
    buying_power=25_000.0,
    options_buying_power=25_000.0,
    unrealized_pnl=0.0,
    realized_pnl_today=0.0,
    approval_level=2,
    net_dollar_delta=0.0,
    net_dollar_gamma=0.0,
    net_dollar_theta=0.0,
    net_dollar_vega=0.0,
)

ok, reason = has_buying_power(portfolio, config.limits)
print(ok, reason)

ok, reason = under_position_cap(portfolio, config.limits)
print(ok, reason)
```

All four gate functions return `(bool, str)` — the string is a human-readable reason when the gate fails, empty string when it passes.
