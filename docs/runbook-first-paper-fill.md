# First Paper Fill — Runbook

> **Audience:** WP owners who need to confirm that the WP-0.5 vertical slice
> works end-to-end and that their own integration point is reachable.
>
> **Scope:** happy path only. The architecture doc (`options-agent-plan.md`)
> covers design rationale; `WORKSTREAMS.md` covers the task breakdown.

---

## What this runbook does

Running `run_entry_cycle()` against Alpaca **paper** exercises the entire
entry pipeline with a hardcoded proposal:

```
stub_reasoner → validate_structural → size → submit_multi_leg
              → reconcile → write_journal_record
```

A successful run produces:
- A **limit order** on the Alpaca paper account dashboard
- A **`JournalRecord`** in the DB with the broker order ID

This is the integration target all subsequent WPs wire into.

---

## Component map — stub vs. real

| Pipeline step | Module | WP-0.5 state | Replaced by |
|---|---|---|---|
| Propose | `agent/stub_reasoner.py` | **STUB** — hardcoded SPY bull put spread | WP-6 (real LLM reasoner) |
| Context assembly | `orchestrator._stub_context_snapshot` | **STUB** — empty dict | WP-6 (full context assembler) |
| Validate | `risk/validator.py` | **REAL** — structural checks only | WP-4 extends with Greek bands, concentration, event blackout |
| Size | `risk/sizing.py` | **REAL** | WP-4 (no planned changes) |
| Submit | `execution/broker.py` | **REAL** — Alpaca paper | WP-1 extends (multi-leg fill edge cases) |
| Reconcile | `execution/reconcile.py` | **REAL** — detects fills against DB | WP-1 extends (expiry, assignment) |
| Journal | `state/journal.py` | **REAL** | WP-7 adds analytics queries |

**Stub proposal (hardcoded in `agent/stub_reasoner.py`):**
- Strategy: `bull_put_spread` on SPY
- Legs: sell SPY 450 put / buy SPY 445 put, expiry 2026-09-19
- Limit price: controlled by `slice_limit_price` in config (default `−1.50`; smoke test overrides to `−0.01` to guarantee a paper fill)

---

## 1. Prerequisites

**Alpaca paper account**
- Sign up at [app.alpaca.markets](https://app.alpaca.markets) and switch to the
  **Paper Trading** environment.
- Enable **Level 2 Options** (or higher) in Account → Trading Experience.
- Generate a **Paper Trading API Key** (not a live key) from the API Keys page.

**Python environment**
```bash
uv sync --dev
```

---

## 2. Credential setup

Secrets go in environment variables — never in `config.toml`.

```bash
export ALPACA_API_KEY="your-paper-api-key"
export ALPACA_SECRET_KEY="your-paper-secret-key"
```

Confirm `config.toml` has:
```toml
alpaca_paper = true
```

The `BrokerClient` reads the env vars at construction time and raises
`ValueError` if either is missing.

---

## 3. Run the smoke test

This is the canonical verification path. It runs `run_entry_cycle()` against
Alpaca paper, polls for a fill, reads back the `JournalRecord`, and asserts
every acceptance criterion automatically.

```bash
uv run pytest -m "integration and smoke" \
    options_agent/tests/test_paper_smoke.py -v
```

**Requirements:**
- NYSE must be open (regular trading hours, Eastern time).
- `ALPACA_API_KEY` must be set.

The test skips (not fails) if either condition is not met.

**Expected output (success):**

```
options_agent/tests/test_paper_smoke.py::test_paper_smoke_run_entry_cycle PASSED
```

Pytest internally verifies:
- AC #1: `run_entry_cycle()` completed without exception
- AC #2: limit order appears in the Alpaca paper account
- AC #3: `reconcile()` transitions the position `PENDING_OPEN → OPEN` within 90 s
- AC #4: `JournalRecord` contains the broker order ID
- AC #5: `JournalRecord` round-trips losslessly from the DB

---

## 4. Verify: Alpaca paper order dashboard

1. Log in at [app.alpaca.markets](https://app.alpaca.markets) and select **Paper Trading**.
2. Navigate to **Orders**.
3. Look for a multi-leg order on **SPY** with two put legs (450/445 strikes).

**Success indicator:** the order status is `filled` (or `partially_filled`
during the 90 s polling window). A `new` / `accepted` status means the fill
hasn't been detected yet — the smoke test polls until it transitions.

**Failure indicator:** status `rejected`. This means the broker refused the
order (insufficient buying power, options level too low, or the spread was
rejected because the underlying was halted). The smoke test will fail with a
message explaining the broker status.

---

## 5. Verify: JournalRecord in the DB

The smoke test uses an in-memory SQLite database (isolated from
`options_agent.db`). To inspect the record after the fact, run a short Python
snippet that calls `run_entry_cycle()` against a file-based DB:

```python
import logging
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from options_agent.config import Config
from options_agent.orchestrator import run_entry_cycle
from options_agent.state.db import get_connection, metadata
from options_agent.state.journal import read_journal_record, query_journal
from options_agent.contracts.state import ActionTaken

logging.basicConfig(level=logging.INFO)

# File-based DB so the record persists after the script exits.
config = Config(alpaca_paper=True, slice_limit_price=-0.01)
engine = sa.create_engine("sqlite:///slice_verify.db")
metadata.create_all(engine)

result = run_entry_cycle(config, engine=engine)
print(f"action_taken : {result.action_taken}")
print(f"cycle_id     : {result.journal_record_id}")

with get_connection(engine) as conn:
    jr = read_journal_record(conn, result.journal_record_id)

print(f"order_ids    : {jr.order_ids}")
print(f"position_ids : {jr.position_ids}")
print(f"strategy     : {jr.strategy}")
print(f"underlying   : {jr.underlying}")
```

Or query the DB directly via the `sqlite3` CLI:

```bash
sqlite3 slice_verify.db \
  "SELECT cycle_id, action_taken, strategy, underlying, order_ids
   FROM journal_records
   ORDER BY timestamp DESC LIMIT 5;"
```

**Success indicators:**
- `action_taken = OPENED` — the order was submitted and the journal was written.
- `order_ids` is a JSON list containing one ID (the local order UUID).
- `strategy = bull_put_spread`, `underlying = SPY`.

**Other outcomes:**

| `action_taken` | Meaning |
|---|---|
| `REJECTED` | Structural validation failed (`rejection_rule_ids` shows which rule). Proposal is hardcoded — this is unexpected and indicates a config or contract mismatch. |
| `SIZED_TO_ZERO` | Sizing capped contracts to 0 (conviction below floor or equity too low). |
| `EXECUTION_FAILED` | Broker rejected the order post-sizing. `JournalRecord` is still written; check Alpaca paper order status. |

---

## 6. Step-by-step checkpoint summary

| Step | What to check | Success looks like | Failure looks like |
|---|---|---|---|
| Credentials | `BrokerClient(config)` constructs without error | No exception | `ValueError: ALPACA_API_KEY not set` |
| stub_reasoner | Returns `TradeProposal` | SPY bull_put_spread, 2 put legs | `RuntimeError`: expiry within 30 DTE → bump `_STUB_EXPIRY` in `stub_reasoner.py` |
| validate_structural | `ValidationResult.passed == True` | Log: `validation passed` | Log: `REJECTED [rule_ids]` — check `config.toml` limits |
| size | `SizingResult.contracts > 0` | Log: `sized to N contracts` | `capped_to_zero=True` — insufficient equity or conviction below `conviction_floor` |
| submit_multi_leg | Order appears in Alpaca paper | Log: `order submitted broker_id=...` | Log: `EXECUTION_FAILED` — check Alpaca options level and buying power |
| reconcile | Position transitions `PENDING_OPEN → OPEN` | Log fill detected within 90 s | Position stays `PENDING_OPEN` — paper fill may be slow; re-run reconcile manually |
| write_journal_record | Row in `journal_records` table | `action_taken = OPENED` | DB write error — check `db_url` and file permissions |

---

## 7. Maintenance note: bump the stub expiry

`stub_reasoner.py` raises `RuntimeError` when the hardcoded expiry
(`_STUB_EXPIRY`) is within 30 days. When that fires:

1. Open `options_agent/agent/stub_reasoner.py`.
2. Update `_STUB_EXPIRY` to a real quarterly options expiration date (third
   Friday of March, June, September, or December, at least 45 days out).
3. Confirm the new expiry satisfies the chain filter (`min_dte=20`, `max_dte=45`
   in `config.toml`).
