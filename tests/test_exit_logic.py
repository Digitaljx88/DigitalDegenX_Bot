from __future__ import annotations

import exit_logic


def test_narrative_exit_profile_uses_faster_political_defaults():
    profile_name, profile = exit_logic.narrative_exit_profile("Political")

    assert profile_name == "political_spike"
    assert profile["first_risk_off"]["activate_mult"] == 1.5
    assert profile["first_risk_off"]["sell_pct"] == 35
    assert profile["velocity_rollover"]["min_score_drop"] == 8


def test_ensure_exit_blocks_backfills_new_sections_and_keeps_entry_score():
    cfg = {
        "symbol": "TEST",
        "trailing_stop": {
            "enabled": True,
            "trail_pct": 25,
            "sell_pct": 100,
            "peak_price": 0.0,
            "triggered": False,
        },
    }

    changed = exit_logic.ensure_exit_blocks(cfg, narrative="AI", entry_score_effective=84)

    assert changed is True
    assert cfg["exit_profile"] == "ai_runner"
    assert cfg["narrative"] == "AI"
    assert cfg["first_risk_off"]["activate_mult"] == 1.6
    assert cfg["velocity_rollover"]["peak_score"] == 84
    assert cfg["velocity_rollover"]["last_score"] == 84
    assert cfg["trailing_stop"]["post_partial_trail_pct"] == 15
    assert cfg["trailing_stop"]["trail_pct"] == 25


def test_tighten_trailing_after_partial_enables_and_lowers_trail():
    cfg = {
        "first_risk_off": {
            "enabled": True,
            "activate_mult": 1.75,
            "sell_pct": 30,
            "tighten_trailing": True,
            "tighten_to_pct": 18,
            "triggered": False,
        },
        "trailing_stop": {
            "enabled": False,
            "trail_pct": 25,
            "sell_pct": 100,
            "peak_price": 0.0,
            "triggered": False,
            "post_partial_trail_pct": 18,
            "tightened": False,
        },
    }

    changed = exit_logic.tighten_trailing_after_partial(cfg, current_price=0.0042)

    assert changed is True
    assert cfg["trailing_stop"]["enabled"] is True
    assert cfg["trailing_stop"]["trail_pct"] == 18
    assert cfg["trailing_stop"]["peak_price"] == 0.0042
    assert cfg["trailing_stop"]["tightened"] is True
