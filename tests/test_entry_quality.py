from __future__ import annotations

from unittest.mock import patch

import autobuy
import scanner


def setup_function():
    scanner.clear_quality_state()


def test_search_pairs_enrich_only_does_not_create_new_candidates():
    tokens = {
        "knownmint": {"mint": "knownmint", "name": "Known", "_source_name": "pumpfun_newest", "_source_rank": 100}
    }
    pairs = [
        {
            "chainId": "solana",
            "pairCreatedAt": 9_999_999_999_999,
            "baseToken": {"address": "newmint", "name": "New", "symbol": "NEW"},
            "marketCap": 50_000,
            "priceUsd": 0.001,
            "volume": {"m5": 1_000, "h1": 4_000},
            "txns": {"m5": {"buys": 10, "sells": 2}},
            "priceChange": {"h1": 10, "h24": 10},
            "liquidity": {"usd": 10_000},
            "dexId": "raydium",
            "pairAddress": "pair1",
        }
    ]

    scanner._parse_pairs(
        pairs,
        tokens,
        source_name="dex_search",
        source_rank=scanner.SOURCE_RANKS["dex_search"],
        require_existing=True,
    )

    assert "newmint" not in tokens


def test_apply_entry_quality_rules_force_scouted_for_weak_source():
    rules = scanner.apply_entry_quality_rules(
        {
            "source_name": "dex_search",
            "age_mins": 4,
            "wallet_signal": 0,
            "txns_5m": 12,
            "buy_ratio_5m": 0.7,
            "buy_ratio_delta": 0.0,
            "liquidity_drop_pct": 0.0,
            "holder_concentration_pct": 8.0,
            "holder_concentration_delta": 0.0,
            "score_drop_from_peak": 0.0,
            "score_slope": 0.0,
            "liquidity_to_mcap_ratio": 0.10,
            "txns_per_10k_liq": 10.0,
            "mcap": 40_000,
            "narrative_cluster_count": 0,
        },
        effective_score=78,
        momentum_alive=True,
    )

    assert rules["force_scouted"] is True
    assert rules["alert_blocked"] is False
    assert rules["autobuy_blocked"] is True


def test_apply_entry_quality_rules_block_chop_and_stale_entries():
    rules = scanner.apply_entry_quality_rules(
        {
            "source_name": "pumpfun_hot",
            "age_mins": 32,
            "wallet_signal": 0,
            "txns_5m": 14,
            "buy_ratio_5m": 0.43,
            "buy_ratio_delta": -0.18,
            "liquidity_drop_pct": -18.0,
            "holder_concentration_pct": 17.5,
            "holder_concentration_delta": 3.5,
            "score_drop_from_peak": 12.0,
            "score_slope": -4.5,
            "liquidity_to_mcap_ratio": 0.02,
            "txns_per_10k_liq": 1.0,
            "mcap": 220_000,
            "narrative_cluster_count": 4,
        },
        effective_score=74,
        momentum_alive=True,
    )

    assert rules["alert_blocked"] is True
    assert rules["autobuy_blocked"] is True
    assert "buy ratio fading" in rules["quality_reasons"]
    assert "liquidity dropping fast" in rules["quality_reasons"]
    assert "outside first-20m auto-buy window" in rules["autobuy_only_reasons"]


def test_select_newest_alerts_skips_quality_blocked_tokens():
    scored_tokens = [
        {
            "mint": "newest_bad",
            "pair_created": 2_000,
            "mcap": 100_000,
            "total": 92,
            "effective_score": 92,
            "momentum_alive": True,
            "entry_quality_alert_blocked": True,
            "entry_quality_force_scouted": False,
        },
        {
            "mint": "older_good",
            "pair_created": 1_000,
            "mcap": 100_000,
            "total": 82,
            "effective_score": 82,
            "momentum_alive": True,
            "entry_quality_alert_blocked": False,
            "entry_quality_force_scouted": False,
        },
    ]

    users, channel = scanner.select_newest_alerts(
        scored_tokens,
        [1],
        {1: {"scanner_mcap_min": 0, "scanner_mcap_max": 1_000_000}},
        channel_enabled=True,
        channel_scouted_threshold=35,
        channel_hot_threshold=70,
    )

    assert users[1][1]["mint"] == "older_good"
    assert channel[1]["mint"] == "older_good"


def test_gate_entry_quality_blocks_scanner_flagged_token():
    passed, reason = autobuy.gate_entry_quality(
        {
            "entry_quality_autobuy_blocked": True,
            "entry_quality_reasons": ["buy ratio fading"],
            "entry_quality_force_scouted_reasons": ["weak discovery source"],
            "entry_quality_autobuy_only_reasons": ["outside first-20m auto-buy window"],
        }
    )

    assert passed is False
    assert "entry quality blocked" in reason
    assert "buy ratio fading" in reason


@patch("autobuy.requests.get")
def test_gate_freshness_blocks_falling_buy_ratio(mock_get):
    mock_get.return_value.json.return_value = {
        "pairs": [{
            "chainId": "solana",
            "volume": {"m5": 250, "h1": 2_000},
            "priceChange": {"h1": 3},
            "txns": {"m5": {"buys": 3, "sells": 9}},
            "liquidity": {"usd": 12_000},
        }]
    }

    passed, reason, _, _ = autobuy.gate_freshness("mint123")

    assert passed is False
    assert "buy ratio fading" in reason
