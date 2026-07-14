"""Ask-the-journal analyst — package layout (WP-9.9 refactor).

loop.py       exploration/commit loop: ask_stream() (generator) + ask()
              (thin non-streaming wrapper)
schema.py     AskAnswer + the forced-commit submit_ask_answer tool
prompts.py    system prompt (schema + WP-0 semantic conventions)
tool.py       run_sql tool definition
sql_guard.py  SELECT-only guardrail (validate_select_only, execute_guarded_select)

Re-exports loop.py's public API here so `from options_agent.agent.ask import
ask_stream` (etc.) keeps working unchanged for every caller outside this
package — only code that imported the sibling modules directly
(ask_schema/ask_prompts/ask_tool/sql_guard) needs updated import paths.
"""

from options_agent.agent.ask.loop import (
    Answer,
    AskError,
    AskEvent,
    AskResult,
    HistoryTurn,
    QueryError,
    QueryResult,
    QueryStarted,
    ask,
    ask_stream,
)

__all__ = [
    "Answer",
    "AskError",
    "AskEvent",
    "AskResult",
    "HistoryTurn",
    "QueryError",
    "QueryResult",
    "QueryStarted",
    "ask",
    "ask_stream",
]
