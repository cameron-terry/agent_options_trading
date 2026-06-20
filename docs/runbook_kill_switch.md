# Kill-Switch Runbook

Three escalating tiers, ordered by decreasing dependency on the system being healthy.
Reach for a lower tier only when the tier above it is unavailable.

---

## Tier 1 — Primary: CLI script

Use this for all routine operations.  The script validates input, records
`set_by`/`reason`, confirms the write, and echoes the new state.

```bash
# Arm HALT (stop new entries; monitor continues managing existing positions)
python -m options_agent.obs set HALT --reason "broker reconcile mismatch"

# Arm FLATTEN (close all open positions immediately; no new entries)
python -m options_agent.obs set FLATTEN --reason "vega band breached"

# Check current state
python -m options_agent.obs status

# Resume trading (requires explicit reason; interactive confirmation)
python -m options_agent.obs resume --reason "issue resolved, all positions reviewed"

# View history
python -m options_agent.obs history --n 20
```

**When to use HALT vs FLATTEN:**

| State | New entries | Monitor | Use when |
|-------|-------------|---------|----------|
| HALT | Blocked | Runs normally — stops/targets still fire | Unexpected broker state, reconcile anomaly, manual review needed |
| FLATTEN | Blocked | Closes all open positions immediately | Uncontrolled risk, emergency exit, critical system failure |

---

## Tier 2 — Break-glass: manual SQL INSERT

Use when the Python environment is unavailable (codebase broken, env missing,
dependencies uninstallable).  This is a raw DB write — double-check before executing.

**Schema reminder:** `kill_switch_log` is append-only.  Use `INSERT`, never `UPDATE`.

```sql
-- Arm HALT
INSERT INTO kill_switch_log (id, state, set_by, reason, created_at)
VALUES (
    lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
    substr(lower(hex(randomblob(2))),2) || '-' || substr('89ab',1+abs(random()) % 4, 1) ||
    substr(lower(hex(randomblob(2))),2) || '-' || lower(hex(randomblob(6))),
    'HALT',
    'your-name',
    'reason for halt',
    datetime('now')
);

-- Arm FLATTEN
INSERT INTO kill_switch_log (id, state, set_by, reason, created_at)
VALUES (
    lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
    substr(lower(hex(randomblob(2))),2) || '-' || substr('89ab',1+abs(random()) % 4, 1) ||
    substr(lower(hex(randomblob(2))),2) || '-' || lower(hex(randomblob(6))),
    'FLATTEN',
    'your-name',
    'reason for flatten',
    datetime('now')
);

-- Resume (clear to NONE)
INSERT INTO kill_switch_log (id, state, set_by, reason, created_at)
VALUES (
    lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
    substr(lower(hex(randomblob(2))),2) || '-' || substr('89ab',1+abs(random()) % 4, 1) ||
    substr(lower(hex(randomblob(2))),2) || '-' || lower(hex(randomblob(6))),
    'NONE',
    'your-name',
    'issue resolved: <describe what was fixed>',
    datetime('now')
);

-- Verify current state (most recent row)
SELECT state, set_by, reason, created_at
FROM kill_switch_log
ORDER BY created_at DESC, id DESC
LIMIT 1;
```

For Postgres, replace `randomblob` UUID generation with `gen_random_uuid()::text`.

**Connect to the DB:**
```bash
# SQLite (default)
sqlite3 options_agent.db

# Postgres
psql "$DB_URL"
```

---

## Tier 3 — Last resort: Alpaca API key revocation

Use when **nothing in the codebase can be trusted** — process is hung, DB is
inaccessible, the machine itself is compromised, or you need an instant
hardware-level stop with no dependency on the running system.

Revoking the paper key immediately cuts off all broker communication.
**This is irreversible until a new key is generated and the system is reconfigured.**

### Steps

1. **Log in to the Alpaca dashboard**
   - Paper account: https://app.alpaca.markets/paper-trading
   - Live account: https://app.alpaca.markets/

2. **Navigate to API Keys**
   - Top-right menu → "Overview" or "API Keys"

3. **Revoke the key in use**
   - Find the key matching `ALPACA_API_KEY` in your environment
   - Click "Revoke" or "Delete"
   - Confirm the revocation

4. **Verify the agent has stopped**
   - Any in-flight Alpaca API calls will begin returning 401 Unauthorized
   - The running process will log auth errors and (if WP-8 is implemented)
     halt the loop on a non-recoverable `CycleError`

5. **After the incident**
   - Generate a new API key pair in the Alpaca dashboard
   - Update `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in your environment
   - Clear the kill switch (Tier 1 or 2) before restarting
   - Document the incident in the kill-switch log with `set NONE --reason "..."`

---

## Resuming after any tier

Regardless of which tier was used to arm the kill switch, **always resume
through Tier 1** if possible:

```bash
python -m options_agent.obs resume --reason "issue resolved: <what was fixed>"
```

This ensures the audit log captures who resumed and why, which is the first
question asked after any incident.

After FLATTEN specifically: before resuming, verify that the monitor has
processed all closing orders and that open positions are at zero (or
intentionally retained).  Resuming into NONE with unexpected open positions
is the highest-risk resumption scenario.
