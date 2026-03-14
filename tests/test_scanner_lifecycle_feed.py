from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import scanner
from services.lifecycle import snapshot_to_scanner_token
from services.lifecycle.models import TokenEnrichment, TokenLifecycle, TokenSnapshot, TokenTradeMetrics


def _snapshot() -> TokenSnapshot:
    now = time.time()
    return TokenSnapshot(
        mint="mint-lifecycle",
        lifecycle=TokenLifecycle(
            mint="mint-lifecycle",
            symbol="LIFE",
            name="Lifecycle Token",
            state="pump_active",
            launch_ts=now,
            source_primary="pump_launch",
            source_rank=100,
            narrative="AI",
            archetype="MICRO_ROCKETSHIP",
            strategy_profile="launch_snipe",
            last_confidence=6.0,
        ),
        metrics=TokenTradeMetrics(
            mint="mint-lifecycle",
            buys_5m=12,
            sells_5m=3,
            volume_usd_5m=4_500,
            liquidity_usd=18_500,
        ),
        enrichment=TokenEnrichment(
            mint="mint-lifecycle",
            dex={
                "marketCap": 42_000,
                "pairCreatedAt": int(now * 1000),
                "priceUsd": 0.00042,
                "volume": {"m5": 4_500, "h1": 18_000},
                "txns": {"m5": {"buys": 12, "sells": 3}},
                "priceChange": {"h1": 14.2, "h24": 14.2},
                "liquidity": {"usd": 18_500},
                "dexId": "pumpfun",
                "pairAddress": "pair-life",
            },
            pump={
                "description": "AI launch token",
                "twitter": "https://x.com/lifetoken",
            },
        ),
        events=[],
    )


def test_lifecycle_snapshot_to_token_normalizes_scanner_shape():
    token = snapshot_to_scanner_token(_snapshot(), max_age_hours=scanner.MAX_TOKEN_AGE_HOURS)

    assert token is not None
    assert token["mint"] == "mint-lifecycle"
    assert token["symbol"] == "LIFE"
    assert token["mcap"] == 42_000
    assert token["txns_m5_buys"] == 12
    assert token["txns_m5_sells"] == 3
    assert token["liquidity"] == 18_500
    assert token["_source_name"] == "pump_launch"
    assert token["_source_rank"] == 100
    assert token["description"] == "AI launch token"


@patch("scanner.fetch_new_tokens")
@patch("scanner.fetch_lifecycle_tokens")
def test_collect_scan_tokens_prefers_lifecycle_and_dedupes(fetch_lifecycle_tokens, fetch_new_tokens):
    fetch_lifecycle_tokens.return_value = [
        {
            "mint": "mint-lifecycle",
            "name": "Lifecycle Token",
            "symbol": "LIFE",
            "mcap": 42_000,
            "pair_created": 9_999_999_999_999,
            "liquidity": 18_500,
            "dex": "pumpfun",
            "_source_name": "pump_launch",
            "_source_rank": 100,
        }
    ]
    fetch_new_tokens.return_value = [
        {
            "mint": "mint-lifecycle",
            "name": "Legacy Copy",
            "symbol": "LEG",
            "mcap": 42_000,
            "pair_created": 9_999_999_999_000,
            "liquidity": 19_000,
            "dex": "pumpfun",
            "_source_name": "dex_search",
            "_source_rank": 20,
        },
        {
            "mint": "mint-legacy-only",
            "name": "Legacy Only",
            "symbol": "ONLY",
            "mcap": 55_000,
            "pair_created": 9_999_999_998_000,
            "liquidity": 12_000,
            "dex": "pumpfun",
            "_source_name": "pumpfun_hot",
            "_source_rank": 90,
        },
    ]

    tokens = scanner.collect_scan_tokens()

    mints = [token["mint"] for token in tokens]
    assert mints.count("mint-lifecycle") == 1
    assert "mint-legacy-only" in mints

    lifecycle_token = next(token for token in tokens if token["mint"] == "mint-lifecycle")
    assert lifecycle_token["name"] == "Lifecycle Token"
    assert lifecycle_token["_source_name"] == "pump_launch"


@pytest.mark.asyncio
async def test_select_autobuy_candidates_prefers_first_preview_eligible_token():
    scored_tokens = [
        {"mint": "blocked-token", "mcap": 50_000, "pair_created": 200, "symbol": "BLK"},
        {"mint": "good-token", "mcap": 55_000, "pair_created": 199, "symbol": "GOOD"},
    ]
    user_settings_map = {123: {"scanner_mcap_min": 15_000, "scanner_mcap_max": 10_000_000}}

    async def fake_evaluate(uid, result, skip_freshness=False):
        return SimpleNamespace(gate_passed=result["mint"] == "good-token")

    with (
        patch("scanner._db.get_auto_buy_config", return_value={"enabled": True}),
        patch("autobuy.evaluate", side_effect=fake_evaluate),
    ):
        selected = await scanner.select_autobuy_candidates(scored_tokens, [123], user_settings_map)

    assert selected[123]["mint"] == "good-token"
