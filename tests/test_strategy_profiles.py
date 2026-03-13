from __future__ import annotations

import strategy_profiles as sp


def test_resolve_launch_snipe_for_fresh_pumpfun_launch():
    profile = sp.resolve_strategy_profile(
        {
            "_source_name": "pumpfun_newest",
            "age_mins": 4,
            "mcap": 120_000,
            "matched_narrative": "Animal",
        }
    )

    assert profile == "launch_snipe"


def test_resolve_wallet_follow_when_wallet_signal_is_strong():
    profile = sp.resolve_strategy_profile(
        {
            "_source_name": "dex_lookup",
            "age_mins": 45,
            "wallet_signal": 6,
            "matched_narrative": "AI",
        }
    )

    assert profile == "wallet_follow"


def test_strategy_rules_block_stale_launch_snipe_autobuy():
    flags = sp.evaluate_strategy_rules(
        {
            "strategy_profile": "launch_snipe",
            "_source_name": "pumpfun_newest",
            "age_mins": 28,
            "liquidity": 9_000,
            "txns_5m": 14,
            "buy_ratio_5m": 0.64,
            "wallet_signal": 0,
            "matched_narrative": "Animal",
            "mcap": 180_000,
        }
    )

    assert "launch_snipe missed early entry window" in flags["strategy_autobuy_only_reasons"]


def test_apply_auto_sell_profile_uses_wallet_follow_defaults():
    cfg = {
        "trailing_stop": {"enabled": False, "trail_pct": 30, "post_partial_trail_pct": 20},
        "trailing_tp": {"enabled": False, "activate_mult": 2.0, "trail_pct": 20, "sell_pct": 50},
        "time_exit": {"enabled": False, "hours": 24, "target_mult": 2.0},
        "first_risk_off": {"enabled": True, "activate_mult": 2.0, "sell_pct": 25},
        "velocity_rollover": {"enabled": False},
    }

    changed = sp.apply_auto_sell_profile(cfg, "wallet_follow")

    assert changed is True
    assert cfg["strategy_profile"] == "wallet_follow"
    assert cfg["trailing_tp"]["enabled"] is True
    assert cfg["trailing_tp"]["activate_mult"] == 2.5
    assert cfg["first_risk_off"]["activate_mult"] == 2.2
    assert cfg["velocity_rollover"]["enabled"] is True
