from __future__ import annotations

from datetime import datetime, timezone

import pytest

import db
import trade_center as tc


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


def test_closed_trades_fifo_match_partial_sells(isolated_db):
    db.log_trade(
        uid=1,
        mode="paper",
        action="buy",
        mint="mint-a",
        symbol="AAA",
        ts=_ts(9, 0),
        sol_amount=1.0,
        token_amount=1000,
        price_usd=0.001,
        entry_source="pumpfun_newest",
        entry_score_effective=88,
        entry_archetype="launch_snipe",
        entry_strategy="launch_snipe",
    )
    db.log_trade(
        uid=1,
        mode="paper",
        action="buy",
        mint="mint-a",
        symbol="AAA",
        ts=_ts(9, 5),
        sol_amount=2.0,
        token_amount=2000,
        price_usd=0.0012,
        entry_source="pumpfun_hot",
        entry_score_effective=79,
        entry_archetype="launch_snipe",
        entry_strategy="launch_snipe",
    )
    db.log_trade(
        uid=1,
        mode="paper",
        action="sell",
        mint="mint-a",
        symbol="AAA",
        ts=_ts(9, 10),
        sol_received=1.8,
        token_amount=1500,
        price_usd=0.0015,
        exit_reason="tp_target_hit",
        max_unrealized_pnl_pct=70.0,
        giveback_pct=10.0,
    )
    db.log_trade(
        uid=1,
        mode="paper",
        action="sell",
        mint="mint-a",
        symbol="AAA",
        ts=_ts(9, 20),
        sol_received=2.4,
        token_amount=1500,
        price_usd=0.0018,
        exit_reason="trailing_stop",
        max_unrealized_pnl_pct=90.0,
        giveback_pct=18.0,
    )

    closed = db.get_closed_trades(1, limit=10)

    assert len(closed) == 3

    newest = closed[0]
    assert newest["qty_sold"] == pytest.approx(1500)
    assert newest["sol_in"] == pytest.approx(1.5)
    assert newest["sol_out"] == pytest.approx(2.4)
    assert newest["pnl_sol"] == pytest.approx(0.9)
    assert newest["exit_reason"] == "trailing_stop"
    assert newest["max_unrealized_pnl_pct"] == pytest.approx(90.0)
    assert newest["giveback_pct"] == pytest.approx(18.0)

    older = sorted(closed[1:], key=lambda row: row["buy_ts"])
    assert older[0]["qty_sold"] == pytest.approx(1000)
    assert older[0]["sol_in"] == pytest.approx(1.0)
    assert older[0]["sol_out"] == pytest.approx(1.2)
    assert older[0]["entry_source"] == "pumpfun_newest"
    assert older[0]["entry_strategy"] == "launch_snipe"
    assert older[1]["qty_sold"] == pytest.approx(500)
    assert older[1]["sol_in"] == pytest.approx(0.5)
    assert older[1]["sol_out"] == pytest.approx(0.6)
    assert older[1]["entry_source"] == "pumpfun_hot"


def test_closed_trade_cohorts_roll_up_source_score_and_age_bands(isolated_db):
    db.log_trade(
        uid=2,
        mode="paper",
        action="buy",
        mint="mint-x",
        symbol="XXX",
        ts=_ts(10, 0),
        sol_amount=1.0,
        token_amount=100,
        entry_source="pumpfun_newest",
        entry_age_mins=4.0,
        entry_score_effective=92,
        entry_archetype="launch_snipe",
        entry_strategy="launch_snipe",
        narrative="AI",
    )
    db.log_trade(
        uid=2,
        mode="paper",
        action="sell",
        mint="mint-x",
        symbol="XXX",
        ts=_ts(10, 30),
        sol_received=1.5,
        token_amount=100,
        exit_reason="tp_target_hit",
        narrative="AI",
        max_unrealized_pnl_pct=60.0,
        giveback_pct=10.0,
    )
    db.log_trade(
        uid=2,
        mode="paper",
        action="buy",
        mint="mint-y",
        symbol="YYY",
        ts=_ts(11, 0),
        sol_amount=1.0,
        token_amount=100,
        entry_source="dex_pairs_new",
        entry_age_mins=22.0,
        entry_score_effective=68,
        entry_archetype="narrative_breakout",
        entry_strategy="narrative_breakout",
        narrative="Animal",
    )
    db.log_trade(
        uid=2,
        mode="paper",
        action="sell",
        mint="mint-y",
        symbol="YYY",
        ts=_ts(11, 20),
        sol_received=0.8,
        token_amount=100,
        exit_reason="hard_stop",
        narrative="Animal",
        max_unrealized_pnl_pct=15.0,
        giveback_pct=12.0,
    )

    trades = db.get_trades(2, limit=20)
    closed = db.get_closed_trades(2, limit=10)
    cohorts = tc.summarize_closed_cohorts(closed)
    summary = tc.summarize_trades(trades, closed)

    assert cohorts["by_source"][0]["label"] == "pumpfun_newest"
    assert cohorts["by_source"][0]["realized_pnl_sol"] == pytest.approx(0.5)
    assert cohorts["by_narrative"][0]["label"] == "AI"
    assert cohorts["by_strategy"][0]["label"] == "launch_snipe"
    assert cohorts["by_exit_reason"][0]["label"] == "tp_target_hit"
    assert cohorts["by_exit_reason"][0]["avg_giveback_pct"] == pytest.approx(10.0)
    assert {row["label"] for row in cohorts["by_score_band"]} >= {"90-100", "55-69"}
    assert {row["label"] for row in cohorts["by_age_band"]} >= {"0-5m", "15-30m"}
    assert summary["avg_giveback_pct"] == pytest.approx(11.0)
    assert summary["avg_peak_unrealized_pct"] == pytest.approx(37.5)
    assert summary["best_exit_reason"] == "tp_target_hit"
    assert summary["top_strategy"] == "launch_snipe"


def test_weekly_optimization_report_ranks_recent_closed_cohorts(isolated_db):
    old_buy = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc).timestamp()
    old_sell = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc).timestamp()

    db.log_trade(
        uid=3,
        mode="paper",
        action="buy",
        mint="mint-old",
        symbol="OLD",
        ts=old_buy,
        sol_amount=1.0,
        token_amount=100,
        entry_source="dex_profiles",
        entry_age_mins=40.0,
        entry_score_effective=58,
        entry_archetype="narrative_breakout",
        entry_strategy="narrative_breakout",
        narrative="Animal",
    )
    db.log_trade(
        uid=3,
        mode="paper",
        action="sell",
        mint="mint-old",
        symbol="OLD",
        ts=old_sell,
        sol_received=2.2,
        token_amount=100,
        exit_reason="manual",
        narrative="Animal",
        max_unrealized_pnl_pct=140.0,
        giveback_pct=40.0,
    )

    db.log_trade(
        uid=3,
        mode="paper",
        action="buy",
        mint="mint-recent-a",
        symbol="NEWA",
        ts=_ts(8, 0),
        sol_amount=1.0,
        token_amount=100,
        entry_source="pumpfun_newest",
        entry_age_mins=3.0,
        entry_score_effective=91,
        entry_archetype="launch_snipe",
        entry_strategy="launch_snipe",
        narrative="AI",
    )
    db.log_trade(
        uid=3,
        mode="paper",
        action="sell",
        mint="mint-recent-a",
        symbol="NEWA",
        ts=_ts(8, 20),
        sol_received=1.6,
        token_amount=100,
        exit_reason="tp_target_hit",
        narrative="AI",
        max_unrealized_pnl_pct=70.0,
        giveback_pct=12.0,
    )

    db.log_trade(
        uid=3,
        mode="paper",
        action="buy",
        mint="mint-recent-b",
        symbol="NEWB",
        ts=_ts(9, 0),
        sol_amount=1.0,
        token_amount=100,
        entry_source="pumpfun_hot",
        entry_age_mins=12.0,
        entry_score_effective=82,
        entry_archetype="wallet_follow",
        entry_strategy="wallet_follow",
        narrative="Political",
    )
    db.log_trade(
        uid=3,
        mode="paper",
        action="sell",
        mint="mint-recent-b",
        symbol="NEWB",
        ts=_ts(9, 30),
        sol_received=0.8,
        token_amount=100,
        exit_reason="trailing_stop",
        narrative="Political",
        max_unrealized_pnl_pct=25.0,
        giveback_pct=8.0,
    )

    closed = db.get_closed_trades(3, limit=20)
    report = tc.build_optimization_report(
        closed,
        window_days=7,
        now_ts=datetime(2026, 3, 13, 12, 0, tzinfo=timezone.utc).timestamp(),
    )

    assert report["summary"]["closed_count"] == 2
    assert report["summary"]["realized_pnl_sol"] == pytest.approx(0.4)
    assert report["summary"]["avg_giveback_pct"] == pytest.approx(10.0)
    assert report["leaders"]["strategy"]["label"] == "launch_snipe"
    assert report["leaders"]["source"]["label"] == "pumpfun_newest"
    assert report["leaders"]["score_band"]["label"] == "90-100"
    assert report["leaders"]["age_band"]["label"] == "0-5m"
    assert report["leaders"]["exit_reason"]["label"] == "tp_target_hit"
    assert report["cohorts"]["by_strategy"][0]["label"] == "launch_snipe"
    assert report["insights"][0].startswith("Best strategy this window: launch_snipe")


def test_closed_trade_filters_and_top_labels_follow_realized_pnl(isolated_db):
    db.log_trade(
        uid=4,
        mode="paper",
        action="buy",
        mint="mint-a",
        symbol="AAA",
        ts=_ts(8, 0),
        sol_amount=1.0,
        token_amount=100,
        entry_source="pumpfun_newest",
        entry_age_mins=4.0,
        entry_score_effective=91,
        entry_archetype="launch_snipe",
        entry_strategy="launch_snipe",
        narrative="AI",
    )
    db.log_trade(
        uid=4,
        mode="paper",
        action="sell",
        mint="mint-a",
        symbol="AAA",
        ts=_ts(8, 20),
        sol_received=1.4,
        token_amount=100,
        narrative="AI",
        exit_reason="tp_target_hit",
        max_unrealized_pnl_pct=50.0,
        giveback_pct=8.0,
    )
    db.log_trade(
        uid=4,
        mode="paper",
        action="buy",
        mint="mint-b",
        symbol="BBB",
        ts=_ts(9, 0),
        sol_amount=1.0,
        token_amount=100,
        entry_source="dex_pairs_new",
        entry_age_mins=12.0,
        entry_score_effective=82,
        entry_archetype="wallet_follow",
        entry_strategy="wallet_follow",
        narrative="Political",
    )
    db.log_trade(
        uid=4,
        mode="paper",
        action="buy",
        mint="mint-c",
        symbol="CCC",
        ts=_ts(9, 10),
        sol_amount=1.0,
        token_amount=100,
        entry_source="dex_pairs_new",
        entry_age_mins=14.0,
        entry_score_effective=80,
        entry_archetype="wallet_follow",
        entry_strategy="wallet_follow",
        narrative="Political",
    )
    db.log_trade(
        uid=4,
        mode="paper",
        action="sell",
        mint="mint-b",
        symbol="BBB",
        ts=_ts(9, 30),
        sol_received=0.7,
        token_amount=100,
        narrative="Political",
        exit_reason="hard_stop",
        max_unrealized_pnl_pct=12.0,
        giveback_pct=5.0,
    )
    db.log_trade(
        uid=4,
        mode="paper",
        action="sell",
        mint="mint-c",
        symbol="CCC",
        ts=_ts(9, 40),
        sol_received=0.8,
        token_amount=100,
        narrative="Political",
        exit_reason="hard_stop",
        max_unrealized_pnl_pct=18.0,
        giveback_pct=6.0,
    )

    trades = db.get_trades(4, limit=20)
    closed = db.get_closed_trades(4, limit=20)
    summary = tc.summarize_trades(trades, closed)

    assert tc.filter_closed_trades(closed, "buys") == []
    assert tc.filter_closed_trades(closed, "sells") == []
    assert summary["top_source"] == "pumpfun_newest"
    assert summary["top_narrative"] == "AI"
    assert summary["top_archetype"] == "launch_snipe"
