import tomllib
from datetime import time
from pathlib import Path

from pydantic import BaseModel, Field

from options_agent.risk.limits import Limits


class Config(BaseModel):
    """Operational configuration for one agent run.

    Load from a TOML file with Config.from_toml(path). The [limits] section
    maps directly to the Limits model. entry_times must be "HH:MM" strings
    in TOML (e.g. ["10:30", "13:00", "15:00"]).

    Secrets (ALPACA_API_KEY, ALPACA_SECRET_KEY) must be supplied via
    environment variables — never commit them to config.toml.

    Kill-switch state is stored as a DB row (not in this config) so it can
    be toggled live without a process restart. db_url is the connection
    string used by WP-2/WP-7/WP-8 to read and write that row.
    """

    # Universe
    universe_file: Path = Field(default=Path("universe.txt"))

    # Entry cadence
    entry_times: list[time] = Field(default=[time(10, 30), time(13, 0), time(15, 0)])
    timezone: str = Field(default="America/New_York")
    # Open/close blackout windows guard against wide spreads at the auction;
    # they are independently tunable because the risk profile differs.
    # le=120 is a sanity cap — a blackout longer than 2h is almost certainly
    # a misconfiguration, not intentional policy.
    session_open_blackout_minutes: int = Field(default=30, ge=0, le=120)
    session_close_blackout_minutes: int = Field(default=30, ge=0, le=120)
    # exchange_calendars calendar name; XNYS = NYSE (US equities).
    # exchange_calendars calendar data has a finite forward horizon — refresh
    # the pinned package version periodically so holidays beyond that horizon
    # are reflected correctly.
    exchange_calendar: str = Field(default="XNYS")

    # Persistence / connectivity
    db_url: str = Field(default="sqlite:///options_agent.db")
    alpaca_paper: bool = Field(default=True)

    # Risk limits (nested)
    limits: Limits = Field(default_factory=Limits)

    @classmethod
    def from_toml(cls, path: Path) -> "Config":
        """Load Config from a TOML file at *path*."""
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls.model_validate(data)
