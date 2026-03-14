from __future__ import annotations

import heat_score_v2


def test_lifecycle_narrative_and_wallet_signal_feed_heat_score():
    result = heat_score_v2.calculate_heat_score_v2(
        {
            "mint": "mint-life-1",
            "name": "Plain Token",
            "symbol": "PLAIN",
            "created_timestamp": 9_999_999_999,
            "lifecycle_narrative": "AI",
            "wallet_signal": 8,
            "txns_m5_buys": 9,
            "txns_m5_sells": 2,
            "buy_ratio_5m": 0.82,
        },
        {},
        {},
    )

    assert result["factors"]["social_narrative"]["pts"] >= 4
    assert result["factors"]["wallets"]["pts"] >= 10
    assert result["factors"]["directional_bias"]["pts"] >= 6


def test_lifecycle_migration_state_affects_migration_factor():
    pending = heat_score_v2.calculate_heat_score_v2(
        {
            "mint": "mint-life-2",
            "name": "Pending Token",
            "symbol": "PEND",
            "created_timestamp": 9_999_999_999,
            "lifecycle_state": "migration_pending",
        },
        {},
        {},
    )
    migrated = heat_score_v2.calculate_heat_score_v2(
        {
            "mint": "mint-life-3",
            "name": "Migrated Token",
            "symbol": "MIG",
            "created_timestamp": 9_999_999_999,
            "lifecycle_state": "raydium_live",
        },
        {},
        {},
    )

    assert pending["factors"]["migration"]["pts"] >= migrated["factors"]["migration"]["pts"]
    assert pending["factors"]["migration"]["details"]["lifecycle_state"] == "migration_pending"
    assert migrated["factors"]["migration"]["details"]["lifecycle_state"] == "raydium_live"
