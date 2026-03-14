from __future__ import annotations

from datetime import datetime, timezone
import time

from fastapi.testclient import TestClient
import pytest

import db
from api_server import API_KEY, app
from services.lifecycle import store


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    existing_conn = getattr(db._local, "conn", None)
    if existing_conn is not None:
        existing_conn.close()
    db._local.conn = None
    db.init()

    yield

    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
    db._local.conn = None


def _ts(hour: int, minute: int = 0) -> float:
    return datetime(2026, 3, 13, hour, minute, tzinfo=timezone.utc).timestamp()


def test_lifecycle_store_builds_snapshot_and_timeline(isolated_db):
    store.record_launch_event(
        "mint-lifecycle",
        symbol="MINT",
        name="Mint Token",
        launch_ts=_ts(9, 0),
        dev_wallet="dev-wallet-1",
    )
    store.record_swap_metrics(
        "mint-lifecycle",
        buys_5m=12,
        sells_5m=2,
        buy_ratio_5m=0.857,
        liquidity_usd=18_000,
        updated_ts=_ts(9, 3),
    )
    store.record_migration_detected("mint-lifecycle", migration_ts=_ts(9, 10))
    store.attach_raydium_pool("mint-lifecycle", "ray-pool-1", ts=_ts(9, 11))
    store.attach_dex_pair("mint-lifecycle", "dex-pair-1", ts=_ts(9, 12))
    store.upsert_enrichment("mint-lifecycle", dex={"marketCap": 125000}, rugcheck={"score": 88})
    snapshot = store.update_score_state(
        "mint-lifecycle",
        narrative="AI",
        archetype="MICRO_ROCKETSHIP",
        strategy_profile="launch_snipe",
        last_score=82,
        last_effective_score=87,
        last_confidence=0.74,
        metrics={"score_slope": 1.4, "peak_score": 87},
    )

    assert snapshot is not None
    assert snapshot.lifecycle.state == "dex_indexed"
    assert snapshot.lifecycle.strategy_profile == "launch_snipe"
    assert snapshot.metrics.liquidity_usd == pytest.approx(18_000)
    assert snapshot.enrichment.dex["marketCap"] == 125000
    assert len(snapshot.events) >= 5


def test_lifecycle_snapshot_endpoint_returns_normalized_payload(isolated_db):
    store.record_launch_event("mint-api", symbol="API", name="API Token", launch_ts=_ts(10, 0))
    store.record_swap_metrics("mint-api", buys_5m=4, sells_5m=1, buy_ratio_5m=0.8, liquidity_usd=12_000, updated_ts=_ts(10, 1))
    store.update_score_state(
        "mint-api",
        narrative="Other",
        archetype="MICRO_ROCKETSHIP",
        strategy_profile="launch_snipe",
        last_score=61,
        last_effective_score=64,
        last_confidence=0.41,
    )

    client = TestClient(app)
    response = client.get(
        "/token/mint-api/snapshot",
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mint"] == "mint-api"
    assert payload["lifecycle"]["symbol"] == "API"
    assert payload["metrics"]["buy_ratio_5m"] == pytest.approx(0.8)
    assert payload["lifecycle"]["strategy_profile"] == "launch_snipe"
    assert payload["trading_snapshot"]["symbol"] == "API"
    assert payload["trading_snapshot"]["buy_ratio_5m"] == pytest.approx(0.8)
    assert payload["analysis"]["strategy_profile"] == "launch_snipe"
    assert payload["analysis"]["quality_flags"]["strategy_profile"] == "launch_snipe"
    assert "breakdown" in payload["analysis"]


def test_scanner_feed_merges_lifecycle_and_scanner_log_without_duplicate_mints(isolated_db):
    now = time.time()
    store.record_launch_event("mint-feed", symbol="FEE", name="Feed Token", launch_ts=now)
    store.record_swap_metrics("mint-feed", buys_5m=9, sells_5m=1, buy_ratio_5m=0.9, updated_ts=now + 60)
    store.update_score_state(
        "mint-feed",
        narrative="AI",
        archetype="MICRO_ROCKETSHIP",
        strategy_profile="launch_snipe",
        last_score=72,
        last_effective_score=78,
        last_confidence=0.55,
    )
    db.append_scan_log({
        "mint": "mint-feed",
        "name": "Feed Token",
        "symbol": "FEE",
        "score": 78,
        "mcap": 42000,
        "narrative": "AI",
        "archetype": "MICRO_ROCKETSHIP",
    })
    db.append_scan_log({
        "mint": "mint-log-only",
        "name": "Legacy Token",
        "symbol": "LEG",
        "score": 62,
        "mcap": 16000,
        "narrative": "Other",
        "archetype": "MICRO_ROCKETSHIP",
    })

    client = TestClient(app)
    response = client.get("/scanner/feed?limit=10", headers={"X-API-Key": API_KEY})

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "lifecycle+scanner_log"
    mints = [item["mint"] for item in payload["items"]]
    assert "mint-feed" in mints
    assert "mint-log-only" in mints
    assert mints.count("mint-feed") == 1
    feed_item = next(item for item in payload["items"] if item["mint"] == "mint-feed")
    assert feed_item["state"] == "pump_active"
    assert feed_item["source_primary"] in {"launch", "pumpfun_newest", "pump_launch", None} or isinstance(feed_item["source_primary"], str)
    assert feed_item["strategy_profile"] == "launch_snipe"
    assert feed_item["confidence"] == pytest.approx(0.55)
    assert feed_item["buy_ratio_5m"] == pytest.approx(0.9)
