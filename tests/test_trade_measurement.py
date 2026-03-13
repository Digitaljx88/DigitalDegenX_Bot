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
    )

    closed = db.get_closed_trades(1, limit=10)

    assert len(closed) == 3

    newest = closed[0]
    assert newest["qty_sold"] == pytest.approx(1500)
    assert newest["sol_in"] == pytest.approx(1.5)
    assert newest["sol_out"] == pytest.approx(2.4)
    assert newest["pnl_sol"] == pytest.approx(0.9)
    assert newest["exit_reason"] == "trailing_stop"

    older = sorted(closed[1:], key=lambda row: row["buy_ts"])
    assert older[0]["qty_sold"] == pytest.approx(1000)
    assert older[0]["sol_in"] == pytest.approx(1.0)
    assert older[0]["sol_out"] == pytest.approx(1.2)
    assert older[0]["entry_source"] == "pumpfun_newest"
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
    )

    closed = db.get_closed_trades(2, limit=10)
    cohorts = tc.summarize_closed_cohorts(closed)

    assert cohorts["by_source"][0]["label"] == "pumpfun_newest"
    assert cohorts["by_source"][0]["realized_pnl_sol"] == pytest.approx(0.5)
    assert cohorts["by_narrative"][0]["label"] == "AI"
    assert {row["label"] for row in cohorts["by_score_band"]} >= {"90-100", "55-69"}
    assert {row["label"] for row in cohorts["by_age_band"]} >= {"0-5m", "15-30m"}
