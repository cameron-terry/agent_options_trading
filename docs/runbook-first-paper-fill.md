# First Paper Fill — Runbook

> **Audience:** anyone who needs to confirm the entry pipeline works end-to-end
> against Alpaca paper. Happy path only. The pipeline itself is documented in
> [docs/features/orchestrator.md](features/orchestrator.md).

---

## What this runbook does

Runs the full entry pipeline (`run_entry_cycle()`) against Alpaca **paper** via
the smoke test. In dev/CI mode (`use_real_data_tools=false`, the default),
context assembly uses fixed mock tool responses; everything else — kill switch,
reconcile, gates, the LLM call, validation, sizing, broker submit, journal
write — is real. A successful run produces:

- A **limit order** on the Alpaca paper account dashboard
- A **`JournalRecord`** in the DB with the broker order ID

The smoke test overrides the limit price to `−0.01` (a giveaway credit) to
guarantee a paper fill regardless of the LLM's proposed spread.

---

## 1. Prerequisites

- **Alpaca paper account** ([app.alpaca.markets](https://app.alpaca.markets), Paper Trading environment) with **Level 2 Options** or higher enabled, and a **Paper Trading API key** (not a live key).
- **`ANTHROPIC_API_KEY`** — the entry cycle makes a real LLM call even in dev/CI mode.
- `uv sync --dev`

## 2. Credential setup

Secrets go in environment variables — never in `config.toml`:

```bash
export ALPACA_API_KEY="your-paper-api-key"
export ALPACA_SECRET_KEY="your-paper-secret-key"
export ANTHROPIC_API_KEY="your-anthropic-api-key"
```

Confirm `config.toml` has `alpaca_paper = true`. `BrokerClient` raises `OSError`
naming any missing variable.

## 3. Run the smoke test

```bash
uv run pytest -m "integration and smoke" options_agent/tests/test_paper_smoke.py -v
```

Requires NYSE to be open and Alpaca keys set — it **skips** (not fails)
otherwise. The test asserts every acceptance criterion itself: cycle completes,
order appears on the paper account, reconcile detects a terminal state (fill,
or cancel-fallback after 300 s — Alpaca paper doesn't reliably simulate
multi-leg fills), and the `JournalRecord` round-trips with the broker order ID.

## 4. Verify on the Alpaca dashboard

Log in → Paper Trading → **Orders**. Look for a multi-leg options order on a
universe symbol; the strategy and strikes are chosen by the LLM at runtime, so
match against `result.proposal.underlying` / `.legs` if in doubt. Status
`filled` (or `partially_filled` during polling) is success; `rejected` means
the broker refused it — check options level, buying power, or a halted
underlying.

## 5. Verify the JournalRecord

The smoke test uses an in-memory DB. To keep a record around, run
`run_entry_cycle()` against a file-based engine (see the offline-testing
section of [orchestrator.md](features/orchestrator.md) for the pattern), then:

```bash
sqlite3 slice_verify.db \
  "SELECT cycle_id, action_taken, strategy, underlying, order_ids
   FROM journal_records ORDER BY timestamp DESC LIMIT 5;"
```

`action_taken = OPENED` with a populated `order_ids` list is success. Other
outcomes: `REJECTED` (check `rejection_rule_ids` against `config.toml` limits),
`SIZED_TO_ZERO` (conviction/equity capped the size), `EXECUTION_FAILED` (broker
rejected post-sizing; the journal record is still written).

## 6. Checkpoint summary

| Step | Success looks like | Failure looks like |
|---|---|---|
| Credentials | `BrokerClient(config)` constructs | `OSError: Missing required environment variable(s): ...` |
| reason (LLM) | Log: `reason() returned proposal strategy=...` | `ReasonerError` — check `ANTHROPIC_API_KEY` / model availability |
| validate | Log: `validation passed` | Log: `REJECTED [rule_ids]` — check `config.toml` limits |
| size | Log: `sized to N contracts` | `capped_to_zero=True` — equity or conviction floor |
| submit | Log: `order submitted broker_id=...` | `EXECUTION_FAILED` — check options level / buying power |
| reconcile | Fill detected; position `PENDING_OPEN → OPEN` | Stays `PENDING_OPEN` — paper fills can be slow; re-run reconcile |
| journal | `action_taken = OPENED` row | DB write error — check `db_url` / migrations |
