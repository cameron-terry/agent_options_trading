"""Tests for agent/prompts.py — PlaybookConfig and build_system_prompt.

Coverage:
  - PlaybookConfig defaults and threshold ordering validation
  - PlaybookConfig.allowed_for_iv_band: high / medium / low / None cases
  - PlaybookConfig.iv_band_label and regime_label helpers
  - PlaybookConfig.all_allowed_strategies includes all three band sets
  - build_system_prompt renders IV-rank thresholds in the prompt
  - build_system_prompt renders all strategy names in the prompt
  - build_system_prompt renders the NO_ACTION rule for unknown iv_rank
  - build_system_prompt renders playbook_version and limits_version
  - build_system_prompt renders iv_rationale and catalyst_check requirements
  - Config._sync_allowed_strategies derives limits.allowed_strategies from playbook
  - Config round-trip with PlaybookConfig fields
  - Config.from_toml populates playbook from [playbook] section
  - PlaybookConfig round-trip (frozenset serialisation)
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from options_agent.agent.prompts import build_system_prompt
from options_agent.config import Config, PlaybookConfig
from options_agent.contracts.data import MarketRegime
from options_agent.risk.limits import Limits

# ---------------------------------------------------------------------------
# PlaybookConfig — defaults and validators
# ---------------------------------------------------------------------------


def test_playbook_config_defaults() -> None:
    pb = PlaybookConfig()
    assert pb.playbook_version == "1.0.0"
    assert pb.iv_rank_high_threshold == 0.50
    assert pb.iv_rank_low_threshold == 0.25
    assert pb.vix_high_vol_threshold == 25.0
    assert pb.vix_low_vol_threshold == 15.0
    assert "iron_condor" in pb.high_iv_strategies
    assert "bull_call_spread" in pb.low_iv_strategies
    assert "bull_call_spread" in pb.medium_iv_strategies


def test_playbook_iv_rank_threshold_ordering_invalid() -> None:
    with pytest.raises(ValidationError, match="iv_rank_low_threshold"):
        PlaybookConfig(iv_rank_low_threshold=0.60, iv_rank_high_threshold=0.50)


def test_playbook_iv_rank_threshold_equal_invalid() -> None:
    with pytest.raises(ValidationError, match="iv_rank_low_threshold"):
        PlaybookConfig(iv_rank_low_threshold=0.50, iv_rank_high_threshold=0.50)


def test_playbook_vix_threshold_ordering_invalid() -> None:
    with pytest.raises(ValidationError, match="vix_low_vol_threshold"):
        PlaybookConfig(vix_low_vol_threshold=30.0, vix_high_vol_threshold=25.0)


def test_playbook_round_trip() -> None:
    pb = PlaybookConfig()
    assert PlaybookConfig.model_validate(pb.model_dump()) == pb


# ---------------------------------------------------------------------------
# PlaybookConfig.allowed_for_iv_band
# ---------------------------------------------------------------------------


def test_allowed_for_iv_band_high() -> None:
    pb = PlaybookConfig()
    allowed = pb.allowed_for_iv_band(0.65)
    assert allowed is not None
    assert "iron_condor" in allowed
    assert "bull_call_spread" not in allowed
    assert "bear_put_spread" not in allowed


def test_allowed_for_iv_band_low() -> None:
    pb = PlaybookConfig()
    allowed = pb.allowed_for_iv_band(0.10)
    assert allowed is not None
    assert "bull_call_spread" in allowed
    assert "bear_put_spread" in allowed
    assert "iron_condor" not in allowed


def test_allowed_for_iv_band_medium() -> None:
    pb = PlaybookConfig()
    allowed = pb.allowed_for_iv_band(0.35)
    assert allowed is not None
    # medium band includes both credit and debit structures
    assert "iron_condor" in allowed
    assert "bull_call_spread" in allowed


def test_allowed_for_iv_band_none_returns_none() -> None:
    pb = PlaybookConfig()
    assert pb.allowed_for_iv_band(None) is None


def test_allowed_for_iv_band_at_high_threshold() -> None:
    pb = PlaybookConfig()
    # Exactly at the high threshold → high band
    allowed = pb.allowed_for_iv_band(pb.iv_rank_high_threshold)
    assert allowed == pb.high_iv_strategies


def test_allowed_for_iv_band_just_below_high_threshold() -> None:
    pb = PlaybookConfig()
    allowed = pb.allowed_for_iv_band(pb.iv_rank_high_threshold - 0.001)
    assert allowed == pb.medium_iv_strategies


def test_allowed_for_iv_band_just_below_low_threshold() -> None:
    pb = PlaybookConfig()
    allowed = pb.allowed_for_iv_band(pb.iv_rank_low_threshold - 0.001)
    assert allowed == pb.low_iv_strategies


def test_allowed_for_iv_band_at_low_threshold() -> None:
    pb = PlaybookConfig()
    # Exactly at the low threshold → medium band (low is strictly < threshold)
    allowed = pb.allowed_for_iv_band(pb.iv_rank_low_threshold)
    assert allowed == pb.medium_iv_strategies


# ---------------------------------------------------------------------------
# PlaybookConfig.all_allowed_strategies
# ---------------------------------------------------------------------------


def test_all_allowed_strategies_is_union_of_bands() -> None:
    pb = PlaybookConfig()
    union = pb.high_iv_strategies | pb.medium_iv_strategies | pb.low_iv_strategies
    assert pb.all_allowed_strategies == union


def test_all_allowed_strategies_contains_all_eight_defaults() -> None:
    pb = PlaybookConfig()
    expected = {
        "bull_put_spread",
        "bear_call_spread",
        "bull_call_spread",
        "bear_put_spread",
        "iron_condor",
        "iron_butterfly",
        "covered_call",
        "cash_secured_put",
    }
    assert pb.all_allowed_strategies == expected


# ---------------------------------------------------------------------------
# PlaybookConfig label helpers
# ---------------------------------------------------------------------------


def test_iv_band_label_high() -> None:
    pb = PlaybookConfig()
    assert pb.iv_band_label(0.75) == "high"


def test_iv_band_label_medium() -> None:
    pb = PlaybookConfig()
    assert pb.iv_band_label(0.35) == "medium"


def test_iv_band_label_low() -> None:
    pb = PlaybookConfig()
    assert pb.iv_band_label(0.10) == "low"


def test_iv_band_label_none() -> None:
    pb = PlaybookConfig()
    assert pb.iv_band_label(None) == "unknown"


def test_regime_label_low_vol() -> None:
    pb = PlaybookConfig()
    assert pb.regime_label(10.0) == MarketRegime.LOW_VOL


def test_regime_label_normal() -> None:
    pb = PlaybookConfig()
    assert pb.regime_label(20.0) == MarketRegime.NORMAL


def test_regime_label_high_vol() -> None:
    pb = PlaybookConfig()
    assert pb.regime_label(30.0) == MarketRegime.HIGH_VOL


def test_regime_label_none() -> None:
    pb = PlaybookConfig()
    assert pb.regime_label(None) == MarketRegime.UNKNOWN


def test_regime_label_at_vix_high_threshold() -> None:
    pb = PlaybookConfig()
    # Exactly at high threshold → still normal (strictly >)
    assert pb.regime_label(pb.vix_high_vol_threshold) == MarketRegime.NORMAL


def test_regime_label_above_vix_high_threshold() -> None:
    pb = PlaybookConfig()
    assert pb.regime_label(pb.vix_high_vol_threshold + 0.1) == MarketRegime.HIGH_VOL


def test_regime_label_at_vix_low_threshold() -> None:
    pb = PlaybookConfig()
    # Exactly at low threshold → normal (low-vol is strictly <, not ≤)
    assert pb.regime_label(pb.vix_low_vol_threshold) == MarketRegime.NORMAL


def test_regime_label_just_below_vix_low_threshold() -> None:
    pb = PlaybookConfig()
    assert pb.regime_label(pb.vix_low_vol_threshold - 0.1) == MarketRegime.LOW_VOL


# ---------------------------------------------------------------------------
# Config — PlaybookConfig integration
# ---------------------------------------------------------------------------


def test_config_has_playbook_field() -> None:
    config = Config()
    assert isinstance(config.playbook, PlaybookConfig)


def test_config_syncs_allowed_strategies_from_playbook() -> None:
    config = Config()
    assert config.limits.allowed_strategies == config.playbook.all_allowed_strategies


def test_config_sync_uses_playbook_not_limits_default() -> None:
    # Even if Limits has its own default, Config always overrides with the playbook.
    custom_pb = PlaybookConfig(
        high_iv_strategies=frozenset({"iron_condor"}),
        medium_iv_strategies=frozenset({"iron_condor", "bull_call_spread"}),
        low_iv_strategies=frozenset({"bull_call_spread"}),
    )
    config = Config(playbook=custom_pb)
    assert config.limits.allowed_strategies == custom_pb.all_allowed_strategies
    assert "bear_call_spread" not in config.limits.allowed_strategies


def test_config_round_trip_with_playbook() -> None:
    config = Config()
    assert Config.model_validate(config.model_dump()) == config


def test_config_from_toml_populates_playbook() -> None:
    config = Config.from_toml(Path("config.toml"))
    assert isinstance(config.playbook, PlaybookConfig)
    assert config.playbook.playbook_version == "1.0.0"
    assert config.playbook.iv_rank_high_threshold == 0.50
    assert config.playbook.iv_rank_low_threshold == 0.25
    assert "iron_condor" in config.playbook.high_iv_strategies
    assert "bull_call_spread" in config.playbook.low_iv_strategies


def test_config_from_toml_syncs_allowed_strategies() -> None:
    config = Config.from_toml(Path("config.toml"))
    assert config.limits.allowed_strategies == config.playbook.all_allowed_strategies
    # Spot-check that both strategies from WP-4.3's original test still pass through
    assert "bull_put_spread" in config.limits.allowed_strategies
    assert "iron_condor" in config.limits.allowed_strategies


# ---------------------------------------------------------------------------
# build_system_prompt — structural correctness
# ---------------------------------------------------------------------------


def test_prompt_renders_playbook_version() -> None:
    pb = PlaybookConfig()
    prompt = build_system_prompt(pb, Limits())
    assert pb.playbook_version in prompt


def test_prompt_renders_limits_version() -> None:
    limits = Limits()
    prompt = build_system_prompt(PlaybookConfig(), limits)
    assert limits.limits_version in prompt


def test_prompt_renders_iv_rank_thresholds() -> None:
    pb = PlaybookConfig()
    prompt = build_system_prompt(pb, Limits())
    assert "50th percentile" in prompt
    assert "25th" in prompt


def test_prompt_renders_high_iv_strategies() -> None:
    pb = PlaybookConfig()
    prompt = build_system_prompt(pb, Limits())
    for strategy in pb.high_iv_strategies:
        assert strategy in prompt, f"Missing high-IV strategy: {strategy}"


def test_prompt_renders_low_iv_strategies() -> None:
    pb = PlaybookConfig()
    prompt = build_system_prompt(pb, Limits())
    for strategy in pb.low_iv_strategies:
        assert strategy in prompt, f"Missing low-IV strategy: {strategy}"


def test_prompt_renders_medium_iv_strategies() -> None:
    pb = PlaybookConfig()
    prompt = build_system_prompt(pb, Limits())
    # A strategy exclusive to medium band should appear
    assert "bull_call_spread" in prompt


def test_prompt_contains_no_action_rule_for_unknown_iv_rank() -> None:
    prompt = build_system_prompt(PlaybookConfig(), Limits())
    assert "NO_ACTION" in prompt
    assert "None" in prompt or "unknown" in prompt.lower()


def test_prompt_requires_iv_rationale() -> None:
    prompt = build_system_prompt(PlaybookConfig(), Limits())
    assert "iv_rationale" in prompt


def test_prompt_requires_catalyst_check() -> None:
    prompt = build_system_prompt(PlaybookConfig(), Limits())
    assert "catalyst_check" in prompt


def test_prompt_renders_vix_thresholds() -> None:
    pb = PlaybookConfig()
    prompt = build_system_prompt(pb, Limits())
    assert str(int(pb.vix_low_vol_threshold)) in prompt
    assert str(int(pb.vix_high_vol_threshold)) in prompt


def test_prompt_renders_event_blackout_days() -> None:
    limits = Limits(event_blackout_days=7)
    prompt = build_system_prompt(PlaybookConfig(), limits)
    assert "7" in prompt


def test_prompt_reflects_custom_thresholds() -> None:
    pb = PlaybookConfig(iv_rank_high_threshold=0.60, iv_rank_low_threshold=0.30)
    prompt = build_system_prompt(pb, Limits())
    assert "60th percentile" in prompt
    assert "30th" in prompt


def test_prompt_reflects_custom_strategy_set() -> None:
    pb = PlaybookConfig(
        high_iv_strategies=frozenset({"iron_condor"}),
        medium_iv_strategies=frozenset({"iron_condor", "bull_call_spread"}),
        low_iv_strategies=frozenset({"bull_call_spread"}),
    )
    prompt = build_system_prompt(pb, Limits())
    assert "iron_condor" in prompt
    # Removed strategies must not appear in the strategy table section.
    # Split at the worked examples header so the hardcoded examples in that
    # section don't satisfy the assertion — the table must be clean.
    table_section = prompt.split("## Worked examples")[0]
    assert "bear_call_spread" not in table_section
    assert "bear_put_spread" not in table_section


def test_prompt_contains_defined_risk_constraint() -> None:
    prompt = build_system_prompt(PlaybookConfig(), Limits())
    assert "naked" in prompt.lower() or "defined-risk" in prompt.lower()


def test_prompt_contains_no_execution_authority_constraint() -> None:
    prompt = build_system_prompt(PlaybookConfig(), Limits())
    assert "execution" in prompt.lower()
