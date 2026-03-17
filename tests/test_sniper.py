from __future__ import annotations

import asyncio

import pytest

import db
import sniper


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
    sniper._engines.clear()


def test_resolve_live_buy_fill_rejects_zero_token_fill():
    opened, tokens_bought, buy_price_sol = sniper._resolve_live_buy_fill(0, 0.1)

    assert opened is False
    assert tokens_bought == 0.0
    assert buy_price_sol == 0.0


def test_active_slots_locked_counts_open_and_pending(isolated_db):
    uid = 11
    sniper._insert_position(
        sniper.SniperPosition(
            uid=uid,
            mint="mintOPEN",
            symbol="OPEN",
            name="Open",
            tokens_bought=100,
            sol_spent=0.1,
            buy_price_sol=0.001,
            buy_time=1.0,
            mode="paper",
        )
    )
    engine = sniper.SniperEngine(uid)
    engine._pending.update({"mintP1", "mintP2"})

    open_positions, pending_positions, total_active = engine._active_slots_locked()

    assert open_positions == 1
    assert pending_positions == 2
    assert total_active == 3


def test_record_buy_attempt_persists_sol_delta_and_outcome(isolated_db):
    sniper._record_buy_attempt(
        31,
        "mintLEDGER",
        "LED",
        "Ledger",
        0.05,
        "tx123",
        "confirmed",
        0.0,
        1.25,
        1.247,
        "zero_tokens",
        "confirmed but empty",
        attempted_at=1000.0,
    )

    row = db._fetchone(
        "select tx_sig, tx_status, tokens_received, sol_before, sol_after, sol_delta, outcome, note from sniper_buy_attempts where uid = ?",
        (31,),
    )

    assert row["tx_sig"] == "tx123"
    assert row["tx_status"] == "confirmed"
    assert row["tokens_received"] == 0.0
    assert row["sol_before"] == 1.25
    assert row["sol_after"] == 1.247
    assert row["sol_delta"] == pytest.approx(0.003)
    assert row["outcome"] == "zero_tokens"
    assert row["note"] == "confirmed but empty"


def test_trip_zero_token_circuit_breaker_disables_sniper(isolated_db, monkeypatch):
    uid = 41
    cfg = sniper.SniperConfig(uid=uid, enabled=True)
    sniper._save_config(cfg)
    now_ts = 10_000.0
    monkeypatch.setattr(sniper.time, "time", lambda: now_ts)

    for idx in range(sniper.ZERO_TOKEN_CB_THRESHOLD):
        sniper._record_buy_attempt(
            uid,
            f"mint{idx}",
            f"S{idx}",
            f"Token {idx}",
            0.05,
            f"tx{idx}",
            "confirmed",
            0.0,
            1.0,
            0.997,
            "zero_tokens",
            attempted_at=now_ts - idx,
        )

    count = sniper._trip_zero_token_circuit_breaker(cfg)
    reloaded = sniper._get_config(uid)

    assert count == sniper.ZERO_TOKEN_CB_THRESHOLD
    assert reloaded.enabled is False


def test_recent_dangerous_buy_count_ignores_zero_receive_without_sol_loss(isolated_db, monkeypatch):
    uid = 51
    now_ts = 20_000.0
    monkeypatch.setattr(sniper.time, "time", lambda: now_ts)

    sniper._record_buy_attempt(
        uid,
        "mintA",
        "A",
        "A",
        0.05,
        "txA",
        "confirmed",
        0.0,
        1.0,
        1.0,
        "zero_tokens",
        attempted_at=now_ts,
    )
    sniper._record_buy_attempt(
        uid,
        "mintB",
        "B",
        "B",
        0.05,
        "txB",
        "confirmed",
        0.0,
        1.0,
        0.995,
        "buy_unconfirmed",
        attempted_at=now_ts,
    )

    assert sniper._recent_dangerous_buy_count(uid, now_ts=now_ts) == 1


def test_buy_path_halted_respects_in_memory_breaker(isolated_db):
    uid = 61
    sniper._save_config(sniper.SniperConfig(uid=uid, enabled=True))
    engine = sniper.SniperEngine(uid)
    engine._breaker_tripped = True

    assert engine._buy_path_halted() is True


@pytest.mark.asyncio
async def test_exit_position_rejects_duplicate_exit_for_same_mint(isolated_db):
    uid = 2
    pos = sniper.SniperPosition(
        uid=uid,
        mint="mintABC",
        symbol="ABC",
        name="Abc",
        tokens_bought=100,
        sol_spent=0.1,
        buy_price_sol=0.001,
        buy_time=1.0,
        mode="paper",
    )
    engine = sniper.SniperEngine(uid)
    engine._exiting.add(pos.mint)

    result = await engine._exit_position(pos, "MANUAL", asyncio.get_running_loop(), "rpc://unused")

    assert result == {"ok": False, "error": "exit_in_progress"}


@pytest.mark.asyncio
async def test_close_position_surfaces_exit_failure(isolated_db, monkeypatch):
    uid = 22
    pos = sniper.SniperPosition(
        uid=uid,
        mint="mintFAIL",
        symbol="FAIL",
        name="Fail",
        tokens_bought=100,
        sol_spent=0.1,
        buy_price_sol=0.001,
        buy_time=1.0,
        mode="paper",
    )
    sniper._insert_position(pos)
    engine = sniper.SniperEngine(uid)

    async def fake_exit_position(*args, **kwargs):
        return {"ok": False, "error": "sell_unconfirmed"}

    monkeypatch.setattr(engine, "_exit_position", fake_exit_position)

    result = await engine.close_position("mintFAIL")

    assert result == {"ok": False, "error": "sell_unconfirmed"}


@pytest.mark.asyncio
async def test_get_engine_resumes_sell_loop_for_persisted_open_positions(isolated_db, monkeypatch):
    uid = 3
    pos = sniper.SniperPosition(
        uid=uid,
        mint="mintRESUME",
        symbol="RSM",
        name="Resume",
        tokens_bought=100,
        sol_spent=0.1,
        buy_price_sol=0.001,
        buy_time=1.0,
        mode="paper",
    )
    sniper._insert_position(pos)

    resumed = {"called": 0}

    def fake_ensure(self):
        resumed["called"] += 1

    monkeypatch.setattr(sniper.SniperEngine, "_ensure_sell_loop", fake_ensure)

    engine = sniper.get_engine(uid)

    assert isinstance(engine, sniper.SniperEngine)
    assert resumed["called"] == 1
