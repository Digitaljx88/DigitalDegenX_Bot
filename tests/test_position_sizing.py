from __future__ import annotations

import position_sizing as ps


BASE_CFG = {
    "sol_amount": 0.03,
    "max_sol_amount": 0.09,
    "min_confidence": 0.35,
    "confidence_scale_enabled": True,
    "max_narrative_exposure": 2,
    "max_archetype_exposure": 0,
}


def test_high_confidence_scales_to_max_tier():
    decision = ps.resolve_position_size(
        BASE_CFG,
        {
            "effective_score": 92,
            "_source_name": "pumpfun_newest",
            "_source_rank": 100,
            "age_mins": 3,
            "wallet_signal": 7,
            "liquidity_to_mcap_ratio": 0.12,
            "txns_per_10k_liq": 14,
            "buy_ratio_5m": 0.80,
            "score_slope": 4.8,
            "score_drop_from_peak": 0,
            "liquidity_drop_pct": 1.0,
            "holder_concentration_delta": 0.0,
            "narrative_cluster_count": 0,
            "matched_narrative": "AI",
            "archetype": "launch_snipe",
            "archetype_conf": 88,
        },
    )

    assert decision.block_reason == ""
    assert decision.confidence >= 0.85
    assert decision.size_multiplier == 2.0
    assert decision.sol_amount == 0.06


def test_low_confidence_valid_trade_gets_micro_size():
    decision = ps.resolve_position_size(
        BASE_CFG,
        {
            "effective_score": 72,
            "_source_name": "dex_lookup",
            "_source_rank": 60,
            "age_mins": 18,
            "wallet_signal": 1,
            "liquidity_to_mcap_ratio": 0.03,
            "txns_per_10k_liq": 3,
            "buy_ratio_5m": 0.58,
            "score_slope": 0.8,
            "score_drop_from_peak": 2,
            "liquidity_drop_pct": -1.0,
            "holder_concentration_delta": 0.0,
            "narrative_cluster_count": 0,
            "matched_narrative": "Animal",
            "archetype": "narrative_breakout",
            "archetype_conf": 55,
        },
    )

    assert decision.block_reason == ""
    assert 0.35 <= decision.confidence < 0.55
    assert decision.size_multiplier == 0.5
    assert decision.sol_amount == 0.015


def test_blocks_when_confidence_below_floor():
    decision = ps.resolve_position_size(
        {**BASE_CFG, "min_confidence": 0.45},
        {
            "effective_score": 70,
            "_source_name": "dex_profiles",
            "_source_rank": 40,
            "age_mins": 45,
            "wallet_signal": 0,
            "liquidity_to_mcap_ratio": 0.015,
            "txns_per_10k_liq": 1,
            "buy_ratio_5m": 0.52,
            "score_slope": -0.4,
            "score_drop_from_peak": 5,
            "liquidity_drop_pct": -8.0,
            "holder_concentration_delta": 1.5,
            "narrative_cluster_count": 2,
            "matched_narrative": "Other",
            "archetype": "SCOUT_V2",
            "archetype_conf": 30,
        },
    )

    assert decision.sol_amount == 0.0
    assert "confidence" in decision.block_reason


def test_blocks_when_narrative_exposure_cap_reached():
    decision = ps.resolve_position_size(
        {**BASE_CFG, "max_narrative_exposure": 2},
        {
            "effective_score": 88,
            "_source_name": "pumpfun_newest",
            "_source_rank": 100,
            "age_mins": 4,
            "wallet_signal": 6,
            "liquidity_to_mcap_ratio": 0.09,
            "txns_per_10k_liq": 9,
            "buy_ratio_5m": 0.75,
            "score_slope": 3.2,
            "score_drop_from_peak": 1,
            "liquidity_drop_pct": 0.0,
            "holder_concentration_delta": 0.0,
            "narrative_cluster_count": 0,
            "matched_narrative": "AI",
            "archetype": "launch_snipe",
            "archetype_conf": 78,
        },
        exposure={"narrative": {"AI": 2}, "archetype": {}},
    )

    assert decision.sol_amount == 0.0
    assert "narrative exposure" in decision.block_reason


def test_blocks_when_archetype_exposure_cap_reached():
    decision = ps.resolve_position_size(
        {**BASE_CFG, "max_archetype_exposure": 1},
        {
            "effective_score": 86,
            "_source_name": "pumpfun_hot",
            "_source_rank": 90,
            "age_mins": 6,
            "wallet_signal": 5,
            "liquidity_to_mcap_ratio": 0.08,
            "txns_per_10k_liq": 8,
            "buy_ratio_5m": 0.72,
            "score_slope": 2.2,
            "score_drop_from_peak": 0,
            "liquidity_drop_pct": 0.0,
            "holder_concentration_delta": 0.0,
            "narrative_cluster_count": 0,
            "matched_narrative": "Political",
            "archetype": "wallet_follow",
            "archetype_conf": 82,
        },
        exposure={"narrative": {}, "archetype": {"wallet_follow": 1}},
    )

    assert decision.sol_amount == 0.0
    assert "archetype exposure" in decision.block_reason


def test_strategy_size_bias_scales_final_amount_when_present():
    decision = ps.resolve_position_size(
        BASE_CFG,
        {
            "effective_score": 88,
            "_source_name": "pumpfun_newest",
            "_source_rank": 100,
            "age_mins": 4,
            "wallet_signal": 6,
            "liquidity_to_mcap_ratio": 0.09,
            "txns_per_10k_liq": 12,
            "buy_ratio_5m": 0.76,
            "score_slope": 3.6,
            "score_drop_from_peak": 0,
            "liquidity_drop_pct": 0.0,
            "holder_concentration_delta": 0.0,
            "narrative_cluster_count": 0,
            "matched_narrative": "AI",
            "archetype": "MICRO_ROCKETSHIP",
            "archetype_conf": 84,
            "strategy_profile": "launch_snipe",
            "strategy_size_bias": 1.15,
        },
    )

    assert decision.block_reason == ""
    assert decision.strategy_profile == "launch_snipe"
    assert decision.sol_amount == 0.069
    assert decision.size_multiplier == 2.3
