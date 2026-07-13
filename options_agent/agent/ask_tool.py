"""WP-9.8: run_sql — the ask-the-journal analyst's only tool.

Definition only — see agent/sql_guard.py for the validation + execution the
tool implementation actually runs against, and agent/ask.py for how it is
dispatched inside the exploration loop.
"""

from anthropic.types import ToolParam

from options_agent.agent.sql_guard import DEFAULT_ROW_CAP, DEFAULT_TIMEOUT_SECS

TOOL_RUN_SQL = "run_sql"

_DESC_RUN_SQL = (
    "Execute one read-only SQL SELECT statement against the journal database"
    " (SQLite) and return the results. This is your only tool — there is no"
    " way to write, update, or delete data through it.\n"
    "\n"
    "RULES:\n"
    "  * Exactly one statement per call — no trailing semicolons, no"
    " multi-statement input.\n"
    "  * SELECT (or UNION/INTERSECT/EXCEPT of SELECTs) only. Any DDL, DML,"
    " PRAGMA, ATTACH, or other statement is rejected before it runs.\n"
    f"  * Results are capped at {DEFAULT_ROW_CAP} rows; if the true result is"
    ' larger, "truncated": true is returned alongside exactly that many'
    " rows — add a LIMIT and/or narrower WHERE/aggregation to see more.\n"
    f"  * Each query has a {DEFAULT_TIMEOUT_SECS:.0f}-second execution"
    " budget; a query that exceeds it is aborted and returned to you as an"
    " error.\n"
    "  * A rejected or failing query returns an error message explaining"
    " why — read it and correct the query rather than repeating it"
    " unchanged.\n"
    "\n"
    "See the system prompt for the schema and semantic conventions (e.g."
    " what a null iv_rank means, the definition of a 'hit') — apply them"
    " when writing queries and interpreting results."
)

RUN_SQL: ToolParam = {
    "name": TOOL_RUN_SQL,
    "description": _DESC_RUN_SQL,
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": ("A single read-only SELECT statement, SQLite dialect."),
            },
        },
        "required": ["sql"],
    },
}

AGENT_ASK_TOOLS: list[ToolParam] = [RUN_SQL]
AGENT_ASK_TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in AGENT_ASK_TOOLS)
