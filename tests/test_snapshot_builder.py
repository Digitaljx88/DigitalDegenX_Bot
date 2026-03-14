from __future__ import annotations

import time

from services.trading import build_trading_snapshot
from services.lifecycle.models import TokenEnrichment, TokenLifecycle, TokenSnapshot, TokenTradeMetrics


def _snapshot() -> TokenSnapshot:
    now = time.time()
    return TokenSnapshot(
        mint="builder-mint",
        lifecycle=TokenLifecycle(
            mint="builder-mint",
            symbol="BLDR",
            name="Builder Token",
            state="pump_active",
            launch_ts=now - 300,
            source_primary="pumpfun_newest",
            source_rank=100,
            narrative="AI",
            archetype="MICRO_ROCKETSHIP",
            strategy_profile="launch_snipe",
            last_score=72,
            last_effective_score=78,
            last_confidence=0.82,
            dev_wallet="dev-wallet",
        ),
        metrics=TokenTradeMetrics(
            mint="builder-mint",
            buys_5m=10,
            sells_5m=2,
            volume_usd_5m=4200,
            liquidity_usd=19000,
            unique_buyers_5m=9,
            holder_concentration=0.12,
            liquidity_delta_pct=18.5,
            bonding_curve_fill_pct=41.0,
            score_slope=3.0,
            score_acceleration=1.4,
            peak_score=80,
            time_since_peak_s=45,
        ),
        enrichment=TokenEnrichment(
            mint="builder-mint",
            dex={
                "marketCap": 52000,
                "pairCreatedAt": int((now - 300) * 1000),
                "priceUsd": 0.00052,
                "volume": {"m5": 4200, "h1": 21000},
                "txns": {"m5": {"buys": 10, "sells": 2}},
                "priceChange": {"h1": 12, "h24": 12},
                "liquidity": {"usd": 19000},
                "dexId": "pumpfun",
                "pairAddress": "pair-builder",
            },
            pump={
                "description": "AI builder token",
                "twitter": "https://x.com/builder",
            },
            wallet={
                "wallet_signal": 7,
            },
        ),
        events=[{"event_type": "launch"}, {"event_type": "score_update"}],
    )


def test_build_trading_snapshot_normalizes_lifecycle_fields():
    token = build_trading_snapshot(_snapshot(), max_age_hours=4)

    assert token is not None
    assert token["mint"] == "builder-mint"
    assert token["symbol"] == "BLDR"
    assert token["source_primary"] == "pumpfun_newest"
    assert token["strategy_profile"] == "launch_snipe"
    assert token["snapshot_score_raw"] == 72
    assert token["snapshot_score_effective"] == 78
    assert token["snapshot_confidence"] == 0.82
    assert token["buy_ratio_5m"] > 0.8
    assert token["unique_buyers_5m"] == 9
    assert token["holder_concentration"] == 0.12
    assert token["raydium_pool"] == ""
    assert token["dev_wallet"] == "dev-wallet"
    assert token["snapshot_events"][0]["event_type"] == "launch"
    assert token["age_mins"] > 0


def test_build_trading_snapshot_respects_freshness_cutoff():
    snapshot = _snapshot()
    snapshot.lifecycle.launch_ts = time.time() - 5 * 3600
    snapshot.enrichment.dex["pairCreatedAt"] = int((time.time() - 5 * 3600) * 1000)

    assert build_trading_snapshot(snapshot, max_age_hours=4) is None
