from __future__ import annotations

from datetime import datetime, timezone

import pytest

import db
import heat_score_v2
import scanner


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    existing_conn = getattr(db._local, "conn", None)
    if existing_conn is not None:
        existing_conn.close()
    db._local.conn = None
    db.clear_seen_tokens()
    db.init()

    yield

    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
    db._local.conn = None
    db.clear_seen_tokens()


def _utc_ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def test_auto_buy_history_is_scoped_to_current_utc_day(isolated_db):
    now = datetime.now(timezone.utc)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp()

    assert db.record_buy(1, "mint_old", 0.2, bought_at=day_start - 60) is True
    assert db.record_buy(1, "mint_today", 0.3, bought_at=day_start + 60) is True

    assert db.has_bought(1, "mint_old") is False
    assert db.has_bought(1, "mint_today") is True
    assert set(db.get_bought_list(1)) == {"mint_today"}


def test_record_buy_allows_rebuy_on_later_day_without_double_counting_same_day(isolated_db):
    day_one = _utc_ts(2026, 3, 10, 12, 0)
    day_two = _utc_ts(2026, 3, 11, 12, 0)

    assert db.record_buy(1, "mint_repeat", 0.25, bought_at=day_one) is True
    assert db.record_buy(1, "mint_repeat", 0.25, bought_at=day_one + 60) is False
    assert db.record_buy(1, "mint_repeat", 0.40, bought_at=day_two) is True

    assert db.has_bought(1, "mint_repeat", since_ts=day_one) is True
    assert db.has_bought(1, "mint_repeat", since_ts=day_two) is True


def test_open_position_count_uses_non_sol_portfolio_holdings(isolated_db):
    db.set_asset(1, "SOL", 10)
    db.set_asset(1, "mint_a", 123)
    db.set_asset(1, "mint_b", 0)
    db.set_auto_sell(1, "stale_exit_only", {"symbol": "OLD"})

    assert db.get_open_position_count(1) == 1


def test_seen_tokens_are_session_scoped(isolated_db):
    assert db.has_seen_token("mint123") is False
    db.mark_seen_token("mint123")
    assert db.has_seen_token("mint123") is True
    db.clear_seen_tokens()
    assert db.has_seen_token("mint123") is False


def test_wallet_alert_rejects_invalid_address(isolated_db):
    with pytest.raises(ValueError):
        db.add_wallet_alert(1, "wallet123", "bad")


def test_cleanup_invalid_wallet_alerts_removes_bad_rows(isolated_db):
    valid_wallet = "So11111111111111111111111111111111111111112"
    db._exec(
        "INSERT INTO wallet_alerts(uid, wallet, label) VALUES(?,?,?)",
        (1, "wallet123", "bad"),
    )
    db.add_wallet_alert(1, valid_wallet, "good")

    removed = db.cleanup_invalid_wallet_alerts()

    assert removed == 1
    assert db.get_wallet_alerts(1) == [{"wallet": valid_wallet, "label": "good"}]


def test_heat_score_disqualifies_active_mint_authority():
    result = heat_score_v2.calculate_heat_score_v2(
        {"mint": "mint1", "name": "Test", "symbol": "TST"},
        {"mintAuthority": "active-authority"},
        {},
    )

    assert result["disqualified"] == "Mint authority active"
    assert result["score"] == 0


def test_heat_score_disqualifies_rugcheck_danger():
    result = heat_score_v2.calculate_heat_score_v2(
        {"mint": "mint2", "name": "Danger", "symbol": "DNG"},
        {"risks": [{"name": "LP unlocked", "level": "danger"}]},
        {},
    )

    assert result["disqualified"] == "RugCheck DANGER"
    assert result["score"] == 0


def test_classify_alert_tier_caps_dead_momentum_to_scouted():
    tier = scanner.classify_alert_tier(
        effective_score=92,
        momentum_alive=False,
        watch_threshold=35,
        warm_threshold=55,
        hot_threshold=70,
        ultra_hot_threshold=85,
    )

    assert tier == "SCOUTED"


def test_channel_alert_ready_respects_cooldown():
    scanner._last_channel_alert_ts = 0.0

    assert scanner.channel_alert_ready(now_ts=100.0) is True
    scanner.mark_channel_alert_sent(now_ts=100.0)
    assert scanner.channel_alert_ready(now_ts=110.0) is False
    assert scanner.channel_alert_ready(
        now_ts=100.0 + scanner.CHANNEL_ALERT_COOLDOWN_SECS + 0.1
    ) is True


def test_select_newest_alerts_prefers_newest_qualifying_token():
    scored_tokens = [
        {
            "mint": "newest",
            "pair_created": 2_000,
            "mcap": 100_000,
            "total": 74,
            "effective_score": 74,
            "momentum_alive": True,
        },
        {
            "mint": "older",
            "pair_created": 1_000,
            "mcap": 100_000,
            "total": 99,
            "effective_score": 99,
            "momentum_alive": True,
        },
    ]
    user_settings_map = {
        1: {
            "alert_scouted_threshold": 35,
            "alert_warm_threshold": 55,
            "alert_hot_threshold": 70,
            "alert_ultra_hot_threshold": 85,
            "scanner_mcap_min": 15_000,
            "scanner_mcap_max": 10_000_000,
        }
    }

    selected_users, selected_channel = scanner.select_newest_alerts(
        scored_tokens,
        [1],
        user_settings_map,
        channel_enabled=True,
        channel_scouted_threshold=35,
        channel_hot_threshold=70,
    )

    assert selected_users[1][1]["mint"] == "newest"
    assert selected_users[1][0] == "HOT"
    assert selected_channel is not None
    assert selected_channel[1]["mint"] == "newest"


def test_merge_token_entry_preserves_higher_trust_source_metadata():
    tokens = {}

    scanner._merge_token_entry(
        tokens,
        "mint123",
        {
            "name": "Fresh Pump",
            "symbol": "PUMP",
            "pair_created": 2_000,
            "dex": "pumpfun",
            "description": "newest launch",
        },
        source_name="pumpfun_newest",
        source_rank=scanner.SOURCE_RANKS["pumpfun_newest"],
    )
    scanner._merge_token_entry(
        tokens,
        "mint123",
        {
            "name": "Noisy Search",
            "symbol": "NOISE",
            "pair_created": 1_000,
            "dex": "raydium",
            "price_usd": 0.001,
        },
        source_name="dex_search",
        source_rank=scanner.SOURCE_RANKS["dex_search"],
    )

    token = tokens["mint123"]
    assert token["name"] == "Fresh Pump"
    assert token["symbol"] == "PUMP"
    assert token["pair_created"] == 2_000
    assert token["dex"] == "pumpfun"
    assert token["price_usd"] == 0.001


def test_merge_token_entry_keeps_newest_timestamp_even_from_lower_rank_source():
    tokens = {}

    scanner._merge_token_entry(
        tokens,
        "mint456",
        {"name": "Existing", "pair_created": 1_000, "dex": "pumpfun"},
        source_name="pumpfun_hot",
        source_rank=scanner.SOURCE_RANKS["pumpfun_hot"],
    )
    scanner._merge_token_entry(
        tokens,
        "mint456",
        {"pair_created": 1_500, "price_usd": 0.002},
        source_name="dex_search",
        source_rank=scanner.SOURCE_RANKS["dex_search"],
    )

    token = tokens["mint456"]
    assert token["pair_created"] == 1_500
    assert token["dex"] == "pumpfun"
    assert token["price_usd"] == 0.002
