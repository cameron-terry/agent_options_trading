"""WP-9.8: run_sql — the ask-the-journal analyst's only tool.

Definition only — see agent/sql_guard.py for the validation + execution the
tool implementation actually runs against, and agent/ask.py for how it is
dispatched inside the exploration loop.

build_run_sql_tool() takes row_cap/timeout_secs as parameters rather than
baking DEFAULT_ROW_CAP/DEFAULT_TIMEOUT_SECS into a module-level constant —
ask() must call it with the row_cap/timeout_secs it actually passes to
execute_guarded_select(), the same way ask_prompts.build_ask_system_prompt()
already takes them, so the tool description a caller with non-default limits
sees never drifts from what's actually enforced. A frozen constant here
would silently mislead the model the moment any caller overrides the
defaults (code-review finding, WP-9.8 PR #94).
"""

from anthropic.types import ToolParam

from options_agent.agent.sql_guard import DEFAULT_ROW_CAP, DEFAULT_TIMEOUT_SECS

TOOL_RUN_SQL = "run_sql"


def _build_description(*, row_cap: int, timeout_secs: float) -> str:
    return (
        "Execute one read-only SQL SELECT statement against the journal database"
        " (SQLite) and return the results. This is your only tool — there is no"
        " way to write, update, or delete data through it.\n"
        "\n"
        "RULES:\n"
        "  * Exactly one statement per call — no trailing semicolons, no"
        " multi-statement input.\n"
        "  * SELECT (or UNION/INTERSECT/EXCEPT of SELECTs) only. Any DDL, DML,"
        " PRAGMA, ATTACH, or other statement is rejected before it runs.\n"
        f"  * Results are capped at {row_cap} rows; if the true result is"
        ' larger, "truncated": true is returned alongside exactly that many'
        " rows — add a LIMIT and/or narrower WHERE/aggregation to see more.\n"
        f"  * Each query has a {timeout_secs:.0f}-second execution"
        " budget; a query that exceeds it is aborted and returned to you as an"
        " error.\n"
        "  * A rejected or failing query returns an error message explaining"
        " why — read it and correct the query rather than repeating it"
        " unchanged.\n"
        "\n"
        "See the system prompt for the schema and semantic conventions (e.g."
        " what a null iv_rank means, the definition of a 'hit') — apply them"
        " when writing queries and interpreting results.\n"
        "\n"
        "If you plan to cite specific cycle_ids in your final answer, make"
        " sure the query actually selects a literal `cycle_id` column (e.g."
        " `SELECT cycle_id, ... FROM journal_records ...`) — citations are"
        " checked against cycle_id values that appeared in a query result"
        " this turn, not values you recall without querying them."
    )


def build_run_sql_tool(
    *, row_cap: int = DEFAULT_ROW_CAP, timeout_secs: float = DEFAULT_TIMEOUT_SECS
) -> ToolParam:
    """Build the run_sql ToolParam with row_cap/timeout_secs interpolated
    into its description.

    Call with the same row_cap/timeout_secs values passed to
    execute_guarded_select() for a given ask() invocation — see this
    module's docstring for why a static tool definition is wrong here.
    """
    return {
        "name": TOOL_RUN_SQL,
        "description": _build_description(row_cap=row_cap, timeout_secs=timeout_secs),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        "A single read-only SELECT statement, SQLite dialect."
                    ),
                },
            },
            "required": ["sql"],
        },
    }


# Name-only constants — independent of row_cap/timeout_secs, safe to import
# as module-level values (unlike the tool definition itself).
AGENT_ASK_TOOL_NAMES: frozenset[str] = frozenset({TOOL_RUN_SQL})
