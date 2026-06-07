from datetime import time
from pathlib import Path

import pytest

from options_agent.config import Config
from options_agent.risk.limits import ChainFilterLimits, ExitPlanDefaults, Limits

# ---------------------------------------------------------------------------
# ChainFilterLimits
# ---------------------------------------------------------------------------


def test_chain_filter_defaults() -> None:
    cf = ChainFilterLimits()
    assert cf.min_open_interest == 500
    assert cf.max_spread_pct_of_mid == 0.10
    assert cf.max_spread_abs_floor == 0.05
    assert cf.min_dte == 20
    assert cf.max_dte == 45
    assert cf.min_abs_delta == 0.15
    assert cf.max_abs_delta == 0.45


def test_chain_filter_dte_range_invalid() -> None:
    with pytest.raises(ValueError, match="min_dte"):
        ChainFilterLimits(min_dte=45, max_dte=20)


def test_chain_filter_dte_equal_invalid() -> None:
    with pytest.raises(ValueError, match="min_dte"):
        ChainFilterLimits(min_dte=30, max_dte=30)


def test_chain_filter_delta_range_invalid() -> None:
    with pytest.raises(ValueError, match="min_abs_delta"):
        ChainFilterLimits(min_abs_delta=0.45, max_abs_delta=0.15)


def test_chain_filter_round_trip() -> None:
    cf = ChainFilterLimits()
    assert ChainFilterLimits.model_validate(cf.model_dump()) == cf


# ---------------------------------------------------------------------------
# ExitPlanDefaults
# ---------------------------------------------------------------------------


def test_exit_plan_defaults() -> None:
    epd = ExitPlanDefaults()
    assert epd.profit_target_pct == 0.50
    assert epd.stop_loss_mult == 2.0
    assert epd.time_stop_dte == 21


def test_exit_plan_round_trip() -> None:
    epd = ExitPlanDefaults()
    assert ExitPlanDefaults.model_validate(epd.model_dump()) == epd


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_limits_defaults() -> None:
    limits = Limits()
    assert limits.limits_version == "0.1.0"
    assert limits.max_loss_per_trade_pct == 0.01
    assert limits.max_open_positions == 5
    assert limits.max_dollar_delta_pct == 0.20
    assert limits.max_dollar_vega_pct == 0.025
    assert limits.min_total_theta is None
    assert limits.max_underlying_concentration_pct == 0.20
    assert limits.max_sector_concentration_pct is None
    assert isinstance(limits.chain_filter, ChainFilterLimits)
    assert isinstance(limits.exit_plan_defaults, ExitPlanDefaults)


def test_limits_round_trip() -> None:
    limits = Limits()
    assert Limits.model_validate(limits.model_dump()) == limits


def test_limits_version_override() -> None:
    limits = Limits(limits_version="1.2.3")
    assert limits.limits_version == "1.2.3"


def test_limits_optional_fields_settable() -> None:
    limits = Limits(min_total_theta=0.5, max_sector_concentration_pct=0.30)
    assert limits.min_total_theta == 0.5
    assert limits.max_sector_concentration_pct == 0.30


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_defaults() -> None:
    config = Config()
    assert config.universe_file == Path("universe.txt")
    assert config.entry_times == [time(10, 30), time(13, 0), time(15, 0)]
    assert config.timezone == "America/New_York"
    assert config.blackout_minutes == 30
    assert config.db_url == "sqlite:///options_agent.db"
    assert config.alpaca_paper is True
    assert isinstance(config.limits, Limits)


def test_config_round_trip() -> None:
    config = Config()
    assert Config.model_validate(config.model_dump()) == config


def test_config_from_toml(tmp_path: Path) -> None:
    toml_content = """\
universe_file = "universe.txt"
timezone = "America/New_York"
entry_times = ["10:30", "13:00", "15:00"]
blackout_minutes = 30
db_url = "sqlite:///test.db"
alpaca_paper = true

[limits]
limits_version = "0.1.0"
max_loss_per_trade_pct = 0.01
max_open_positions = 5
max_dollar_delta_pct = 0.20
max_dollar_vega_pct = 0.025
max_underlying_concentration_pct = 0.20

[limits.chain_filter]
min_open_interest = 500
max_spread_pct_of_mid = 0.10
max_spread_abs_floor = 0.05
min_dte = 20
max_dte = 45
min_abs_delta = 0.15
max_abs_delta = 0.45

[limits.exit_plan_defaults]
profit_target_pct = 0.50
stop_loss_mult = 2.0
time_stop_dte = 21
"""
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(toml_content)

    config = Config.from_toml(toml_path)
    assert config.timezone == "America/New_York"
    assert config.entry_times == [time(10, 30), time(13, 0), time(15, 0)]
    assert config.db_url == "sqlite:///test.db"
    assert config.limits.limits_version == "0.1.0"
    assert config.limits.max_loss_per_trade_pct == 0.01
    assert config.limits.chain_filter.min_dte == 20
    assert config.limits.chain_filter.max_dte == 45
    assert config.limits.exit_plan_defaults.profit_target_pct == 0.50


def test_config_from_toml_partial_limits(tmp_path: Path) -> None:
    """Omitted limit fields fall back to model defaults."""
    toml_content = """\
[limits]
max_loss_per_trade_pct = 0.02
"""
    toml_path = tmp_path / "config_partial.toml"
    toml_path.write_text(toml_content)

    config = Config.from_toml(toml_path)
    assert config.limits.max_loss_per_trade_pct == 0.02
    assert config.limits.max_open_positions == 5  # default
    assert config.limits.min_total_theta is None  # default


# ---------------------------------------------------------------------------
# contracts module re-export
# ---------------------------------------------------------------------------


def test_contracts_module_exports() -> None:
    from options_agent.contracts import ChainFilterLimits as ContractsCF
    from options_agent.contracts import Config as ContractsConfig
    from options_agent.contracts import ExitPlanDefaults as ContractsEPD
    from options_agent.contracts import Limits as ContractsLimits

    assert ContractsConfig is Config
    assert ContractsLimits is Limits
    assert ContractsCF is ChainFilterLimits
    assert ContractsEPD is ExitPlanDefaults
