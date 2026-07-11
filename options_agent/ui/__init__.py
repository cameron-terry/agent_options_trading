"""WP-9 Ops Console — a read-only FastAPI service over the trading journal.

Runs beside the scheduler in docker-compose. No broker credentials in this
process's environment; its only inputs are the DB (read-only engine) and,
from WP-9.8 onward, ANTHROPIC_API_KEY for the ask-the-journal analyst.
"""

from __future__ import annotations
