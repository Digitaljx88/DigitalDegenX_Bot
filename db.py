"""
db.py — Single SQLite data layer for meme-trade-bot.

All persistence goes through this module. Nothing else touches bot.db directly.

Design:
- WAL journal mode: concurrent reads, serialised writes, no locking issues with async bot
- Thread-local connections: each thread (asyncio event loop + executor threads) gets its own conn
- No ORM: plain sqlite3, zero new dependencies
- Every public function is safe to call from any thread
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "bot.db"

_local = threading.local()
_session_seen_tokens: set[str] = set()
_SOLANA_ADDRESS_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

SEEN_TOKEN_TTL = 3600  # legacy constant kept for backward compatibility
_FIFO_EPSILON = 1e-9


# ── Connection management ──────────────────────────────────────────────────────

@contextmanager
def _conn():
    """Thread-local WAL connection. Creates on first use per thread."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    yield _local.conn


def _exec(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _conn() as c:
        cur = c.execute(sql, params)
        c.commit()
        return cur


def _fetchone(sql: str, params: tuple = ()):
    with _conn() as c:
        return c.execute(sql, params).fetchone()


def _fetchall(sql: str, params: tuple = ()):
    with _conn() as c:
        return c.execute(sql, params).fetchall()


def _utc_day_start_ts(now_ts: float | None = None) -> float:
    now = datetime.fromtimestamp(now_ts or time.time(), tz=timezone.utc)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return day_start.timestamp()


def is_valid_solana_address(address: str) -> bool:
    """Cheap local validation for a Solana base58 public key."""
    addr = str(address or "").strip()
    if len(addr) < 32 or len(addr) > 44:
        return False
    return all(ch in _SOLANA_ADDRESS_ALPHABET for ch in addr)


def _decode_json_value(value, default):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


# ── Schema init ────────────────────────────────────────────────────────────────

def init():
    """Create all tables. Safe to call multiple times (CREATE IF NOT EXISTS)."""
    with _conn() as c:
        c.executescript("""
            -- User portfolio holdings (SOL + token balances)
            CREATE TABLE IF NOT EXISTS portfolios (
                uid     INTEGER NOT NULL,
                asset   TEXT    NOT NULL,   -- "SOL" or mint address
                amount  REAL    NOT NULL DEFAULT 0,
                PRIMARY KEY (uid, asset)
            );

            -- Full trade history (buy and sell events)
            CREATE TABLE IF NOT EXISTS trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL    NOT NULL,
                date         TEXT    NOT NULL,
                uid          INTEGER NOT NULL,
                mode         TEXT    NOT NULL,   -- "paper" | "live"
                action       TEXT    NOT NULL,   -- "buy" | "sell"
                mint         TEXT    NOT NULL,
                symbol       TEXT,
                name         TEXT,
                narrative    TEXT,
                heat_score   INTEGER,
                sol_amount   REAL,
                sol_received REAL,
                token_amount REAL,
                price_usd    REAL,
                buy_price_usd REAL,
                mcap         REAL,
                pnl_pct      REAL,
                tx_sig       TEXT,
                entry_source TEXT,
                entry_age_mins REAL,
                entry_liquidity_usd REAL,
                entry_txns_5m INTEGER,
                entry_score_raw INTEGER,
                entry_score_effective INTEGER,
                entry_tier TEXT,
                entry_wallet_signal REAL,
                entry_archetype TEXT,
                entry_strategy TEXT,
                entry_source_rank INTEGER,
                entry_confidence REAL,
                exit_reason TEXT,
                exit_trigger TEXT,
                exit_score_effective INTEGER,
                exit_mcap REAL,
                hold_seconds REAL,
                max_unrealized_pnl_pct REAL,
                giveback_pct REAL
            );
            CREATE INDEX IF NOT EXISTS idx_trades_uid_ts ON trades(uid, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_trades_mint   ON trades(uid, mint);

            -- Closed-trade attribution with FIFO lot matching
            CREATE TABLE IF NOT EXISTS closed_trades (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                uid                   INTEGER NOT NULL,
                mint                  TEXT    NOT NULL,
                mode                  TEXT    NOT NULL,
                symbol                TEXT,
                name                  TEXT,
                narrative             TEXT,
                buy_trade_id          INTEGER NOT NULL,
                sell_trade_id         INTEGER NOT NULL,
                buy_ts                REAL    NOT NULL,
                sell_ts               REAL    NOT NULL,
                qty_sold              REAL    NOT NULL,
                sol_in                REAL    NOT NULL,
                sol_out               REAL    NOT NULL,
                pnl_sol               REAL    NOT NULL,
                pnl_pct               REAL    NOT NULL,
                hold_s                REAL    NOT NULL,
                buy_price_usd         REAL,
                sell_price_usd        REAL,
                tx_sig                TEXT,
                entry_source          TEXT,
                entry_age_mins        REAL,
                entry_score_effective INTEGER,
                entry_confidence      REAL,
                entry_archetype       TEXT,
                entry_strategy        TEXT,
                exit_reason           TEXT,
                max_unrealized_pnl_pct REAL,
                giveback_pct          REAL
            );
            CREATE INDEX IF NOT EXISTS idx_closed_trades_uid_sell_ts ON closed_trades(uid, sell_ts DESC);
            CREATE INDEX IF NOT EXISTS idx_closed_trades_uid_mint ON closed_trades(uid, mint);

            -- Auto-sell rules per user/token (complex config stored as JSON blob)
            CREATE TABLE IF NOT EXISTS auto_sell (
                uid          INTEGER NOT NULL,
                mint         TEXT    NOT NULL,
                symbol       TEXT,
                config_json  TEXT    NOT NULL DEFAULT '{}',
                updated_at   REAL    NOT NULL,
                PRIMARY KEY (uid, mint)
            );

            -- Auto-buy config per user
            CREATE TABLE IF NOT EXISTS auto_buy_config (
                uid                INTEGER PRIMARY KEY,
                enabled            INTEGER NOT NULL DEFAULT 0,
                sol_amount         REAL    NOT NULL DEFAULT 0.03,
                max_sol_amount     REAL    NOT NULL DEFAULT 0.10,
                min_confidence     REAL    NOT NULL DEFAULT 0.35,
                confidence_scale_enabled INTEGER NOT NULL DEFAULT 1,
                min_score          INTEGER NOT NULL DEFAULT 55,
                max_mcap           REAL    NOT NULL DEFAULT 500000,
                min_mcap_usd       REAL    NOT NULL DEFAULT 0,
                daily_limit_sol    REAL    NOT NULL DEFAULT 1.0,
                spent_today        REAL    NOT NULL DEFAULT 0.0,
                spent_date         TEXT,
                max_positions      INTEGER NOT NULL DEFAULT 5,
                max_narrative_exposure INTEGER NOT NULL DEFAULT 2,
                max_archetype_exposure INTEGER NOT NULL DEFAULT 0,
                buy_tier           TEXT    NOT NULL DEFAULT 'warm',
                min_liquidity_usd  REAL    NOT NULL DEFAULT 0,
                max_liquidity_usd  REAL    NOT NULL DEFAULT 0,
                min_age_mins       INTEGER NOT NULL DEFAULT 0,
                max_age_mins       INTEGER NOT NULL DEFAULT 0,
                min_txns_5m        INTEGER NOT NULL DEFAULT 0
            );

            -- Per-user list of already-bought token mints
            CREATE TABLE IF NOT EXISTS auto_buy_history (
                uid       INTEGER NOT NULL,
                mint      TEXT    NOT NULL,
                bought_at REAL    NOT NULL,
                sol_spent REAL    NOT NULL DEFAULT 0,
                PRIMARY KEY (uid, mint)
            );

            -- Per-user auto-buy decision/activity log for dashboard observability
            CREATE TABLE IF NOT EXISTS auto_buy_activity (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                uid               INTEGER NOT NULL,
                ts                REAL    NOT NULL,
                mint              TEXT,
                symbol            TEXT,
                name              TEXT,
                score             INTEGER,
                effective_score   INTEGER,
                mcap              REAL,
                strategy_profile  TEXT,
                confidence        REAL,
                sol_amount        REAL,
                size_multiplier   REAL,
                mode              TEXT,
                status            TEXT    NOT NULL,
                block_reason      TEXT,
                block_category    TEXT,
                source            TEXT,
                narrative         TEXT,
                archetype         TEXT,
                fresh_vol_m5      REAL,
                fresh_price_h1    REAL
            );
            CREATE INDEX IF NOT EXISTS idx_auto_buy_activity_uid_ts ON auto_buy_activity(uid, ts DESC);

            -- Scanner: legacy seen-token table retained for compatibility
            CREATE TABLE IF NOT EXISTS scanner_seen (
                mint    TEXT PRIMARY KEY,
                seen_at REAL NOT NULL
            );

            -- Scanner: watchlist entries (global)
            CREATE TABLE IF NOT EXISTS scanner_watchlist (
                mint       TEXT    PRIMARY KEY,
                data_json  TEXT    NOT NULL DEFAULT '{}',
                added_at   REAL    NOT NULL
            );

            -- Scanner: configuration (scanning flag, scan_targets, etc.)
            CREATE TABLE IF NOT EXISTS scanner_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- Scanner: rolling log of scored tokens (last 500)
            CREATE TABLE IF NOT EXISTS scanner_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                date      TEXT    NOT NULL,
                ts        REAL    NOT NULL,
                mint      TEXT    NOT NULL,
                name      TEXT,
                symbol    TEXT,
                score     INTEGER,
                mcap      REAL,
                narrative TEXT,
                archetype TEXT,
                alerted   INTEGER NOT NULL DEFAULT 0,  -- bool
                dq        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_scan_log_date ON scanner_log(date, alerted);

            -- Key-value settings store (covers global_settings.json)
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL   -- JSON-encoded value
            );

            -- User wallet alert subscriptions
            CREATE TABLE IF NOT EXISTS wallet_alerts (
                uid    INTEGER NOT NULL,
                wallet TEXT    NOT NULL,
                label  TEXT,
                PRIMARY KEY (uid, wallet)
            );

            -- Token lifecycle spine: launch -> pump trades -> migration -> Raydium -> Dex
            CREATE TABLE IF NOT EXISTS token_lifecycle (
                mint                TEXT PRIMARY KEY,
                symbol              TEXT,
                name                TEXT,
                state               TEXT NOT NULL DEFAULT 'launched',
                launch_ts           REAL,
                last_trade_ts       REAL,
                migration_ts        REAL,
                raydium_pool        TEXT,
                dex_pair            TEXT,
                dev_wallet          TEXT,
                source_primary      TEXT,
                source_rank         INTEGER,
                narrative           TEXT,
                archetype           TEXT,
                strategy_profile    TEXT,
                last_score          REAL,
                last_effective_score REAL,
                last_confidence     REAL,
                last_updated_ts     REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_token_lifecycle_updated
                ON token_lifecycle(last_updated_ts DESC);
            CREATE INDEX IF NOT EXISTS idx_token_lifecycle_state
                ON token_lifecycle(state, last_updated_ts DESC);

            CREATE TABLE IF NOT EXISTS token_trade_metrics (
                mint                   TEXT PRIMARY KEY,
                buys_1m                INTEGER NOT NULL DEFAULT 0,
                sells_1m               INTEGER NOT NULL DEFAULT 0,
                buys_5m                INTEGER NOT NULL DEFAULT 0,
                sells_5m               INTEGER NOT NULL DEFAULT 0,
                volume_usd_1m          REAL    NOT NULL DEFAULT 0,
                volume_usd_5m          REAL    NOT NULL DEFAULT 0,
                buy_ratio_5m           REAL    NOT NULL DEFAULT 0,
                unique_buyers_5m       INTEGER NOT NULL DEFAULT 0,
                holder_concentration   REAL    NOT NULL DEFAULT 0,
                dev_activity_score     REAL    NOT NULL DEFAULT 0,
                liquidity_usd          REAL    NOT NULL DEFAULT 0,
                liquidity_delta_pct    REAL    NOT NULL DEFAULT 0,
                bonding_curve_fill_pct REAL    NOT NULL DEFAULT 0,
                score_slope            REAL    NOT NULL DEFAULT 0,
                score_acceleration     REAL    NOT NULL DEFAULT 0,
                peak_score             REAL    NOT NULL DEFAULT 0,
                time_since_peak_s      REAL    NOT NULL DEFAULT 0,
                updated_ts             REAL    NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_token_trade_metrics_updated
                ON token_trade_metrics(updated_ts DESC);

            CREATE TABLE IF NOT EXISTS token_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                mint         TEXT NOT NULL,
                event_type   TEXT NOT NULL,
                ts           REAL NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_token_events_mint_ts
                ON token_events(mint, ts DESC);

            CREATE TABLE IF NOT EXISTS token_enrichment (
                mint         TEXT PRIMARY KEY,
                rugcheck_json TEXT NOT NULL DEFAULT '{}',
                dex_json     TEXT NOT NULL DEFAULT '{}',
                pump_json    TEXT NOT NULL DEFAULT '{}',
                wallet_json  TEXT NOT NULL DEFAULT '{}',
                updated_ts   REAL NOT NULL DEFAULT 0
            );
        """)
        c.commit()

    # Migrate existing auto_buy_config tables that predate the new filter columns
    _new_ab_cols = [
        ("max_sol_amount",     "REAL    NOT NULL DEFAULT 0.10"),
        ("min_confidence",     "REAL    NOT NULL DEFAULT 0.35"),
        ("confidence_scale_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("min_mcap_usd",      "REAL    NOT NULL DEFAULT 0"),
        ("min_liquidity_usd", "REAL    NOT NULL DEFAULT 0"),
        ("max_liquidity_usd", "REAL    NOT NULL DEFAULT 0"),
        ("min_age_mins",      "INTEGER NOT NULL DEFAULT 0"),
        ("max_age_mins",      "INTEGER NOT NULL DEFAULT 0"),
        ("min_txns_5m",       "INTEGER NOT NULL DEFAULT 0"),
        ("max_narrative_exposure", "INTEGER NOT NULL DEFAULT 2"),
        ("max_archetype_exposure", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col, defn in _new_ab_cols:
        try:
            with _conn() as c:
                c.execute(f"ALTER TABLE auto_buy_config ADD COLUMN {col} {defn}")
                c.commit()
        except Exception:
            pass  # column already exists

    _new_trade_cols = [
        ("entry_source", "TEXT"),
        ("entry_age_mins", "REAL"),
        ("entry_liquidity_usd", "REAL"),
        ("entry_txns_5m", "INTEGER"),
        ("entry_score_raw", "INTEGER"),
        ("entry_score_effective", "INTEGER"),
        ("entry_tier", "TEXT"),
        ("entry_wallet_signal", "REAL"),
        ("entry_archetype", "TEXT"),
        ("entry_strategy", "TEXT"),
        ("entry_source_rank", "INTEGER"),
        ("entry_confidence", "REAL"),
        ("exit_reason", "TEXT"),
        ("exit_trigger", "TEXT"),
        ("exit_score_effective", "INTEGER"),
        ("exit_mcap", "REAL"),
        ("hold_seconds", "REAL"),
        ("max_unrealized_pnl_pct", "REAL"),
        ("giveback_pct", "REAL"),
    ]
    for col, defn in _new_trade_cols:
        try:
            with _conn() as c:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} {defn}")
                c.commit()
        except Exception:
            pass

    _new_closed_trade_cols = [
        ("max_unrealized_pnl_pct", "REAL"),
        ("giveback_pct", "REAL"),
    ]
    for col, defn in _new_closed_trade_cols:
        try:
            with _conn() as c:
                c.execute(f"ALTER TABLE closed_trades ADD COLUMN {col} {defn}")
                c.commit()
        except Exception:
            pass


# ── Portfolios ─────────────────────────────────────────────────────────────────

def get_portfolio(uid: int) -> dict:
    """Return {asset: amount} for uid. Empty dict if no holdings."""
    rows = _fetchall("SELECT asset, amount FROM portfolios WHERE uid=?", (uid,))
    return {r["asset"]: r["amount"] for r in rows}


def set_asset(uid: int, asset: str, amount: float):
    """Upsert a single asset balance. Removes the row if amount <= 0."""
    if amount <= 0:
        _exec("DELETE FROM portfolios WHERE uid=? AND asset=?", (uid, asset))
    else:
        _exec(
            "INSERT INTO portfolios(uid, asset, amount) VALUES(?,?,?) "
            "ON CONFLICT(uid, asset) DO UPDATE SET amount=excluded.amount",
            (uid, asset, amount),
        )


def get_all_portfolios() -> dict:
    """Return {uid: {asset: amount}} for all users."""
    rows = _fetchall("SELECT uid, asset, amount FROM portfolios")
    result: dict = {}
    for r in rows:
        result.setdefault(r["uid"], {})[r["asset"]] = r["amount"]
    return result


def reset_portfolio(uid: int, starting_sol: float = 10.0):
    """Delete all holdings for uid and set SOL to starting_sol.
    Also wipes paper-mode trades and closed_trades so PnL starts fresh."""
    _exec("DELETE FROM portfolios WHERE uid=?", (uid,))
    _exec("DELETE FROM trades WHERE uid=? AND mode='paper'", (uid,))
    _exec("DELETE FROM closed_trades WHERE uid=? AND mode='paper'", (uid,))
    set_asset(uid, "SOL", starting_sol)


# ── Trades ─────────────────────────────────────────────────────────────────────

def log_trade(uid: int, mode: str, action: str, mint: str, symbol: str = "",
              **kwargs) -> int:
    """
    Insert a trade record. kwargs accepts any column from the trades table.
    Returns the new row id.
    """
    now = time.time()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = {
        "ts":           kwargs.get("ts", now),
        "date":         kwargs.get("date", today),
        "uid":          uid,
        "mode":         mode,
        "action":       action,
        "mint":         mint,
        "symbol":       symbol,
        "name":         kwargs.get("name"),
        "narrative":    kwargs.get("narrative"),
        "heat_score":   kwargs.get("heat_score"),
        "sol_amount":   kwargs.get("sol_amount"),
        "sol_received": kwargs.get("sol_received"),
        "token_amount": kwargs.get("token_amount"),
        "price_usd":    kwargs.get("price_usd"),
        "buy_price_usd":kwargs.get("buy_price_usd"),
        "mcap":         kwargs.get("mcap"),
        "pnl_pct":      kwargs.get("pnl_pct"),
        "tx_sig":       kwargs.get("tx_sig"),
        "entry_source": kwargs.get("entry_source"),
        "entry_age_mins": kwargs.get("entry_age_mins"),
        "entry_liquidity_usd": kwargs.get("entry_liquidity_usd"),
        "entry_txns_5m": kwargs.get("entry_txns_5m"),
        "entry_score_raw": kwargs.get("entry_score_raw"),
        "entry_score_effective": kwargs.get("entry_score_effective"),
        "entry_tier": kwargs.get("entry_tier"),
        "entry_wallet_signal": kwargs.get("entry_wallet_signal"),
        "entry_archetype": kwargs.get("entry_archetype"),
        "entry_strategy": kwargs.get("entry_strategy"),
        "entry_source_rank": kwargs.get("entry_source_rank"),
        "entry_confidence": kwargs.get("entry_confidence"),
        "exit_reason": kwargs.get("exit_reason"),
        "exit_trigger": kwargs.get("exit_trigger"),
        "exit_score_effective": kwargs.get("exit_score_effective"),
        "exit_mcap": kwargs.get("exit_mcap"),
        "hold_seconds": kwargs.get("hold_seconds"),
        "max_unrealized_pnl_pct": kwargs.get("max_unrealized_pnl_pct"),
        "giveback_pct": kwargs.get("giveback_pct"),
    }
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" * len(row))
    cur = _exec(
        f"INSERT INTO trades({cols}) VALUES({placeholders})",
        tuple(row.values()),
    )
    reconcile_closed_trades(uid)
    return cur.lastrowid


def get_trades(uid: int, limit: int = 200, offset: int = 0,
               mode: str | None = None, action: str | None = None) -> list[dict]:
    """Return trade history for uid, newest first."""
    where = "uid=?"
    params: list = [uid]
    if mode:
        where += " AND mode=?"
        params.append(mode)
    if action:
        where += " AND action=?"
        params.append(action)
    rows = _fetchall(
        f"SELECT * FROM trades WHERE {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return [dict(r) for r in rows]


def get_trade_count(uid: int, mode: str | None = None) -> int:
    """Returns number of tokens traded (buy events only — each token = one trade)."""
    where = "uid=? AND action='buy'"
    params: list = [uid]
    if mode:
        where += " AND mode=?"
        params.append(mode)
    row = _fetchone(f"SELECT COUNT(*) as n FROM trades WHERE {where}", tuple(params))
    return row["n"] if row else 0


def reconcile_closed_trades(uid: int | None = None) -> int:
    """
    Rebuild FIFO closed-trade attribution from raw trades.

    Each sell is matched against prior buy lots for the same uid/mode/mint.
    Partial sells are allocated proportionally across remaining buy lots.
    """
    where = ""
    params: tuple = ()
    if uid is not None:
        where = "WHERE uid=?"
        params = (uid,)

    with _conn() as c:
        if uid is None:
            c.execute("DELETE FROM closed_trades")
        else:
            c.execute("DELETE FROM closed_trades WHERE uid=?", (uid,))

        rows = c.execute(
            f"SELECT * FROM trades {where} ORDER BY uid ASC, mode ASC, mint ASC, ts ASC, id ASC",
            params,
        ).fetchall()

        lots: dict[tuple[int, str, str], list[dict]] = defaultdict(list)
        inserted = 0
        for row in rows:
            trade = dict(row)
            action = str(trade.get("action") or "").lower()
            key = (
                int(trade.get("uid") or 0),
                str(trade.get("mode") or ""),
                str(trade.get("mint") or ""),
            )

            if action == "buy":
                qty = float(trade.get("token_amount") or 0)
                sol_in = float(trade.get("sol_amount") or 0)
                if qty > _FIFO_EPSILON and sol_in > 0:
                    lots[key].append({
                        "trade": trade,
                        "qty_left": qty,
                        "sol_left": sol_in,
                    })
                continue

            if action != "sell":
                continue

            sell_qty = float(trade.get("token_amount") or 0)
            sell_sol_total = float(trade.get("sol_received") or 0)
            if sell_qty <= _FIFO_EPSILON or sell_sol_total <= 0:
                continue

            queue = lots.get(key) or []
            if not queue:
                continue

            qty_remaining = sell_qty
            sol_out_remaining = sell_sol_total
            while qty_remaining > _FIFO_EPSILON and queue:
                buy_lot = queue[0]
                buy_trade = buy_lot["trade"]
                qty_left = float(buy_lot["qty_left"] or 0)
                if qty_left <= _FIFO_EPSILON:
                    queue.pop(0)
                    continue

                matched_qty = min(qty_remaining, qty_left)
                buy_sol_share = float(buy_lot["sol_left"] or 0) * (matched_qty / qty_left)
                is_last_match = matched_qty >= (qty_remaining - _FIFO_EPSILON) or len(queue) == 1
                sell_sol_share = sol_out_remaining if is_last_match else sell_sol_total * (matched_qty / sell_qty)
                pnl_sol = sell_sol_share - buy_sol_share
                pnl_pct = (pnl_sol / buy_sol_share * 100.0) if buy_sol_share > 0 else 0.0
                hold_s = max(0.0, float(trade.get("ts") or 0) - float(buy_trade.get("ts") or 0))

                c.execute(
                    """
                    INSERT INTO closed_trades(
                        uid, mint, mode, symbol, name, narrative,
                        buy_trade_id, sell_trade_id, buy_ts, sell_ts,
                        qty_sold, sol_in, sol_out, pnl_sol, pnl_pct, hold_s,
                        buy_price_usd, sell_price_usd, tx_sig,
                        entry_source, entry_age_mins, entry_score_effective,
                        entry_confidence, entry_archetype, entry_strategy, exit_reason,
                        max_unrealized_pnl_pct, giveback_pct
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        buy_trade.get("uid"),
                        buy_trade.get("mint"),
                        trade.get("mode") or buy_trade.get("mode"),
                        trade.get("symbol") or buy_trade.get("symbol"),
                        trade.get("name") or buy_trade.get("name"),
                        buy_trade.get("narrative") or trade.get("narrative"),
                        buy_trade.get("id"),
                        trade.get("id"),
                        buy_trade.get("ts"),
                        trade.get("ts"),
                        matched_qty,
                        buy_sol_share,
                        sell_sol_share,
                        pnl_sol,
                        pnl_pct,
                        hold_s,
                        buy_trade.get("price_usd"),
                        trade.get("price_usd"),
                        trade.get("tx_sig"),
                        buy_trade.get("entry_source"),
                        buy_trade.get("entry_age_mins"),
                        buy_trade.get("entry_score_effective") or buy_trade.get("heat_score"),
                        buy_trade.get("entry_confidence"),
                        buy_trade.get("entry_archetype"),
                        buy_trade.get("entry_strategy"),
                        trade.get("exit_reason") or trade.get("exit_trigger"),
                        trade.get("max_unrealized_pnl_pct"),
                        trade.get("giveback_pct"),
                    ),
                )
                inserted += 1

                buy_lot["qty_left"] = qty_left - matched_qty
                buy_lot["sol_left"] = float(buy_lot["sol_left"] or 0) - buy_sol_share
                qty_remaining -= matched_qty
                sol_out_remaining -= sell_sol_share

                if buy_lot["qty_left"] <= _FIFO_EPSILON:
                    queue.pop(0)

        c.commit()
        return inserted


def get_closed_trades(uid: int, limit: int = 200, offset: int = 0,
                      mode: str | None = None) -> list[dict]:
    """Return FIFO-attributed closed trades for uid, newest closed first."""
    where = "uid=?"
    params: list = [uid]
    if mode:
        where += " AND mode=?"
        params.append(mode)
    rows = _fetchall(
        f"SELECT * FROM closed_trades WHERE {where} ORDER BY sell_ts DESC, id DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return [dict(r) for r in rows]


# ── Auto-sell ──────────────────────────────────────────────────────────────────

def get_auto_sell(uid: int, mint: str) -> dict | None:
    """Return the full config dict for a position, or None if not tracked."""
    row = _fetchone(
        "SELECT config_json FROM auto_sell WHERE uid=? AND mint=?", (uid, mint)
    )
    return json.loads(row["config_json"]) if row else None


def set_auto_sell(uid: int, mint: str, config: dict, symbol: str = ""):
    """Upsert auto-sell config for a position."""
    _exec(
        "INSERT INTO auto_sell(uid, mint, symbol, config_json, updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(uid, mint) DO UPDATE SET config_json=excluded.config_json, "
        "symbol=excluded.symbol, updated_at=excluded.updated_at",
        (uid, mint, symbol or config.get("symbol", ""), json.dumps(config), time.time()),
    )


def remove_auto_sell(uid: int, mint: str):
    _exec("DELETE FROM auto_sell WHERE uid=? AND mint=?", (uid, mint))


def get_all_auto_sells(uid: int) -> dict:
    """Return {mint: config_dict} for all tracked positions of uid."""
    rows = _fetchall("SELECT mint, config_json FROM auto_sell WHERE uid=?", (uid,))
    return {r["mint"]: json.loads(r["config_json"]) for r in rows}


def get_all_auto_sells_all_users() -> dict:
    """Return {uid: {mint: config}} across all users (for the price-check loop)."""
    rows = _fetchall("SELECT uid, mint, config_json FROM auto_sell")
    result: dict = {}
    for r in rows:
        result.setdefault(r["uid"], {})[r["mint"]] = json.loads(r["config_json"])
    return result


# ── Auto-buy config ────────────────────────────────────────────────────────────

_AB_DEFAULTS = {
    "enabled": False,
    "sol_amount": 0.03,
    "max_sol_amount": 0.10,
    "min_confidence": 0.35,
    "confidence_scale_enabled": True,
    "min_score": 55,
    "max_mcap": 500_000,
    "min_mcap_usd": 0,
    "daily_limit_sol": 1.0,
    "spent_today": 0.0,
    "spent_date": None,
    "max_positions": 5,
    "max_narrative_exposure": 2,
    "max_archetype_exposure": 0,
    "buy_tier": "warm",
    "min_liquidity_usd": 0,
    "max_liquidity_usd": 0,
    "min_age_mins": 0,
    "max_age_mins": 0,
    "min_txns_5m": 0,
}


def get_auto_buy_config(uid: int) -> dict:
    """Return auto-buy config for uid, with defaults filled in."""
    row = _fetchone("SELECT * FROM auto_buy_config WHERE uid=?", (uid,))
    if not row:
        return {"uid": uid, **_AB_DEFAULTS}
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    d["confidence_scale_enabled"] = bool(d.get("confidence_scale_enabled", 1))
    return d


def get_enabled_auto_buy_uids() -> list[int]:
    rows = _fetchall("SELECT uid FROM auto_buy_config WHERE enabled=1")
    return [int(row["uid"]) for row in rows]


def set_auto_buy_config(uid: int, **fields):
    """Upsert individual fields of the auto-buy config."""
    # Ensure row exists first
    existing = get_auto_buy_config(uid)
    existing.update(fields)
    existing["uid"] = uid
    existing["enabled"] = int(bool(existing.get("enabled", False)))
    cols = [k for k in existing if k != "uid"]
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in cols)
    all_cols = ["uid"] + cols
    placeholders = ", ".join("?" * len(all_cols))
    _exec(
        f"INSERT INTO auto_buy_config({', '.join(all_cols)}) VALUES({placeholders}) "
        f"ON CONFLICT(uid) DO UPDATE SET {set_clause}",
        tuple(existing[c] for c in all_cols),
    )


def clear_auto_buy_history(uid: int) -> None:
    """Delete all auto-buy history for a user (called when paper wallet is reset)."""
    _exec("DELETE FROM auto_buy_history WHERE uid=?", (uid,))


def has_bought(uid: int, mint: str, since_ts: float | None = None) -> bool:
    """Return True if uid bought mint since since_ts. Defaults to current UTC day."""
    if since_ts is None:
        since_ts = _utc_day_start_ts()
    row = _fetchone(
        "SELECT 1 FROM auto_buy_history WHERE uid=? AND mint=? AND bought_at>=?",
        (uid, mint, since_ts),
    )
    return row is not None


def record_buy(uid: int, mint: str, sol_spent: float, bought_at: float | None = None) -> bool:
    """Record a buy once. Returns True when a new history row was inserted."""
    ts = bought_at or time.time()
    existing = _fetchone(
        "SELECT bought_at FROM auto_buy_history WHERE uid=? AND mint=?",
        (uid, mint),
    )
    if existing:
        if existing["bought_at"] >= _utc_day_start_ts(ts):
            return False
        _exec(
            "UPDATE auto_buy_history SET bought_at=?, sol_spent=? WHERE uid=? AND mint=?",
            (ts, sol_spent, uid, mint),
        )
    else:
        _exec(
            "INSERT INTO auto_buy_history(uid, mint, bought_at, sol_spent) VALUES(?,?,?,?)",
            (uid, mint, ts, sol_spent),
        )
    add_spent_today(uid, sol_spent)
    return True


def record_auto_buy_activity(
    uid: int,
    *,
    mint: str = "",
    symbol: str = "",
    name: str = "",
    score: int | None = None,
    effective_score: int | None = None,
    mcap: float | None = None,
    strategy_profile: str = "",
    confidence: float | None = None,
    sol_amount: float | None = None,
    size_multiplier: float | None = None,
    mode: str = "",
    status: str = "blocked",
    block_reason: str = "",
    block_category: str = "",
    source: str = "",
    narrative: str = "",
    archetype: str = "",
    fresh_vol_m5: float | None = None,
    fresh_price_h1: float | None = None,
    ts: float | None = None,
) -> int:
    _exec(
        "INSERT INTO auto_buy_activity("
        "uid, ts, mint, symbol, name, score, effective_score, mcap, "
        "strategy_profile, confidence, sol_amount, size_multiplier, mode, status, "
        "block_reason, block_category, source, narrative, archetype, fresh_vol_m5, fresh_price_h1"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            uid,
            float(ts or time.time()),
            mint or "",
            symbol or "",
            name or "",
            score,
            effective_score,
            mcap,
            strategy_profile or "",
            confidence,
            sol_amount,
            size_multiplier,
            mode or "",
            status or "blocked",
            block_reason or "",
            block_category or "",
            source or "",
            narrative or "",
            archetype or "",
            fresh_vol_m5,
            fresh_price_h1,
        ),
    )
    row = _fetchone("SELECT last_insert_rowid() AS id")
    return int(row["id"]) if row else 0


def get_auto_buy_activity(uid: int, limit: int = 20) -> list[dict]:
    rows = _fetchall(
        "SELECT * FROM auto_buy_activity WHERE uid=? ORDER BY ts DESC LIMIT ?",
        (uid, max(1, min(int(limit), 200))),
    )
    return [dict(row) for row in rows]


def get_auto_buy_activity_summary(uid: int, window_hours: int = 24) -> dict:
    since_ts = time.time() - (max(1, int(window_hours)) * 3600)
    rows = _fetchall(
        "SELECT status, block_category, confidence, sol_amount FROM auto_buy_activity "
        "WHERE uid=? AND ts>=?",
        (uid, since_ts),
    )
    status_counts: defaultdict[str, int] = defaultdict(int)
    block_counts: defaultdict[str, int] = defaultdict(int)
    confidence_values: list[float] = []
    size_values: list[float] = []

    for row in rows:
        status = str(row["status"] or "")
        block_category = str(row["block_category"] or "")
        status_counts[status] += 1
        if block_category:
            block_counts[block_category] += 1
        conf = row["confidence"]
        if conf is not None:
            confidence_values.append(float(conf))
        sol_amount = row["sol_amount"]
        if sol_amount is not None:
            size_values.append(float(sol_amount))

    top_blocked = sorted(block_counts.items(), key=lambda item: (-item[1], item[0]))
    return {
        "window_hours": max(1, int(window_hours)),
        "total": len(rows),
        "status_counts": dict(status_counts),
        "blocked_by_category": dict(block_counts),
        "top_block_category": top_blocked[0][0] if top_blocked else "",
        "avg_confidence": (sum(confidence_values) / len(confidence_values)) if confidence_values else 0.0,
        "avg_size_sol": (sum(size_values) / len(size_values)) if size_values else 0.0,
    }


def get_bought_list(uid: int, since_ts: float | None = None) -> list[str]:
    """Return mints bought since since_ts. Defaults to the current UTC day."""
    if since_ts is None:
        since_ts = _utc_day_start_ts()
    rows = _fetchall(
        "SELECT mint FROM auto_buy_history WHERE uid=? AND bought_at>=?",
        (uid, since_ts),
    )
    return [r["mint"] for r in rows]


def get_open_position_count(uid: int) -> int:
    """Count actual non-SOL holdings with non-zero balances."""
    row = _fetchone(
        "SELECT COUNT(*) as n FROM portfolios WHERE uid=? AND asset!='SOL' AND amount>0",
        (uid,),
    )
    return row["n"] if row else 0


def get_open_position_exposure(uid: int) -> dict:
    """
    Count open positions grouped by narrative and archetype.

    Uses auto-sell config metadata when available, then falls back to the most
    recent buy trade for that mint.
    """
    rows = _fetchall(
        "SELECT asset FROM portfolios WHERE uid=? AND asset!='SOL' AND amount>0",
        (uid,),
    )
    narrative_counts: defaultdict[str, int] = defaultdict(int)
    archetype_counts: defaultdict[str, int] = defaultdict(int)

    for row in rows:
        mint = row["asset"]
        narrative = ""
        archetype = ""

        cfg_row = _fetchone(
            "SELECT config_json FROM auto_sell WHERE uid=? AND mint=?",
            (uid, mint),
        )
        if cfg_row:
            try:
                cfg = json.loads(cfg_row["config_json"] or "{}")
            except Exception:
                cfg = {}
            narrative = str(cfg.get("narrative") or "")
            archetype = str(cfg.get("entry_archetype") or cfg.get("archetype") or "")

        if not narrative or not archetype:
            trade_row = _fetchone(
                "SELECT narrative, entry_archetype FROM trades "
                "WHERE uid=? AND mint=? AND action='buy' "
                "ORDER BY ts DESC LIMIT 1",
                (uid, mint),
            )
            if trade_row:
                if not narrative:
                    narrative = str(trade_row["narrative"] or "")
                if not archetype:
                    archetype = str(trade_row["entry_archetype"] or "")

        if narrative and narrative != "Other":
            narrative_counts[narrative] += 1
        if archetype:
            archetype_counts[archetype] += 1

    return {
        "narrative": dict(narrative_counts),
        "archetype": dict(archetype_counts),
    }


def get_spent_today(uid: int) -> float:
    cfg = get_auto_buy_config(uid)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if cfg.get("spent_date") != today:
        return 0.0
    return cfg.get("spent_today", 0.0)


def add_spent_today(uid: int, sol: float):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cfg = get_auto_buy_config(uid)
    if cfg.get("spent_date") != today:
        cfg["spent_today"] = 0.0
        cfg["spent_date"] = today
    cfg["spent_today"] = cfg.get("spent_today", 0.0) + sol
    set_auto_buy_config(uid, spent_today=cfg["spent_today"], spent_date=today)


def reset_day_if_needed(uid: int):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cfg = get_auto_buy_config(uid)
    if cfg.get("spent_date") != today:
        set_auto_buy_config(uid, spent_today=0.0, spent_date=today)


# ── Scanner: seen tokens ───────────────────────────────────────────────────────

def has_seen_token(mint: str) -> bool:
    return mint in _session_seen_tokens


def mark_seen_token(mint: str):
    _session_seen_tokens.add(mint)


def clear_seen_tokens():
    _session_seen_tokens.clear()


# ── Scanner: watchlist ─────────────────────────────────────────────────────────

def get_watchlist() -> dict:
    rows = _fetchall("SELECT mint, data_json FROM scanner_watchlist")
    return {r["mint"]: json.loads(r["data_json"]) for r in rows}


def add_to_watchlist(mint: str, data: dict):
    _exec(
        "INSERT INTO scanner_watchlist(mint, data_json, added_at) VALUES(?,?,?) "
        "ON CONFLICT(mint) DO UPDATE SET data_json=excluded.data_json, added_at=excluded.added_at",
        (mint, json.dumps(data), time.time()),
    )


def remove_from_watchlist(mint: str):
    _exec("DELETE FROM scanner_watchlist WHERE mint=?", (mint,))


# ── Scanner: config (scanning flag, scan_targets) ─────────────────────────────

def get_scanner_config(key: str, default=None):
    row = _fetchone("SELECT value FROM scanner_config WHERE key=?", (key,))
    return json.loads(row["value"]) if row else default


def set_scanner_config(key: str, value):
    _exec(
        "INSERT INTO scanner_config(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def is_scanning() -> bool:
    return bool(get_scanner_config("scanning", True))


def set_scanning(val: bool):
    set_scanner_config("scanning", val)


def get_scan_targets() -> list[int]:
    return get_scanner_config("scan_targets", [])


def set_scan_targets(targets: list[int]):
    set_scanner_config("scan_targets", targets)


def add_scan_target(uid: int):
    targets = get_scan_targets()
    if uid not in targets:
        targets.append(uid)
        set_scan_targets(targets)


def remove_scan_target(uid: int):
    targets = [t for t in get_scan_targets() if t != uid]
    set_scan_targets(targets)
    if not targets:
        clear_seen_tokens()


def get_user_min_score(uid: int) -> int:
    scores = get_scanner_config("user_min_score", {})
    return scores.get(str(uid), 55)


def set_user_min_score(uid: int, score: int):
    scores = get_scanner_config("user_min_score", {})
    scores[str(uid)] = max(1, min(100, score))
    set_scanner_config("user_min_score", scores)


def get_alert_channel() -> str | None:
    return get_scanner_config("alert_channel", None)


def set_alert_channel(channel: str | None):
    if channel:
        set_scanner_config("alert_channel", channel)
    else:
        _exec("DELETE FROM scanner_config WHERE key='alert_channel'")


# ── Scanner: log ──────────────────────────────────────────────────────────────

def append_scan_log(entry: dict):
    """Insert a scanner log entry. Trims table to last 500 rows after insert."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _conn() as c:
        c.execute(
            "INSERT INTO scanner_log(date, ts, mint, name, symbol, score, mcap, "
            "narrative, archetype, alerted, dq) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                entry.get("date", today),
                entry.get("timestamp", time.time()),
                entry.get("mint", ""),
                entry.get("name"),
                entry.get("symbol"),
                entry.get("score"),
                entry.get("mcap"),
                entry.get("narrative"),
                entry.get("archetype"),
                int(bool(entry.get("alerted", False))),
                entry.get("dq"),
            ),
        )
        # Keep last 500 — delete oldest beyond that
        c.execute(
            "DELETE FROM scanner_log WHERE id NOT IN "
            "(SELECT id FROM scanner_log ORDER BY id DESC LIMIT 500)"
        )
        c.commit()


def get_todays_alerts() -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = _fetchall(
        "SELECT * FROM scanner_log WHERE date=? AND alerted=1 ORDER BY ts DESC",
        (today,),
    )
    return [dict(r) for r in rows]


def get_scan_log(limit: int = 500) -> list[dict]:
    rows = _fetchall(
        "SELECT * FROM scanner_log ORDER BY id DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


def get_latest_scan_log_for_mint(mint: str) -> dict | None:
    row = _fetchone(
        "SELECT * FROM scanner_log WHERE mint=? ORDER BY id DESC LIMIT 1",
        (mint,),
    )
    return dict(row) if row else None


def mark_scan_log_alerted(mint: str):
    """Mark the most recent scanner_log row for this mint as alerted=1."""
    _exec(
        "UPDATE scanner_log SET alerted=1 WHERE id=("
        "SELECT MAX(id) FROM scanner_log WHERE mint=?)",
        (mint,),
    )


# ── Token lifecycle spine ────────────────────────────────────────────────────

_TOKEN_LIFECYCLE_DEFAULTS = {
    "symbol": None,
    "name": None,
    "state": "launched",
    "launch_ts": None,
    "last_trade_ts": None,
    "migration_ts": None,
    "raydium_pool": None,
    "dex_pair": None,
    "dev_wallet": None,
    "source_primary": None,
    "source_rank": None,
    "narrative": None,
    "archetype": None,
    "strategy_profile": None,
    "last_score": None,
    "last_effective_score": None,
    "last_confidence": None,
    "last_updated_ts": 0.0,
}

_TOKEN_METRIC_DEFAULTS = {
    "buys_1m": 0,
    "sells_1m": 0,
    "buys_5m": 0,
    "sells_5m": 0,
    "volume_usd_1m": 0.0,
    "volume_usd_5m": 0.0,
    "buy_ratio_5m": 0.0,
    "unique_buyers_5m": 0,
    "holder_concentration": 0.0,
    "dev_activity_score": 0.0,
    "liquidity_usd": 0.0,
    "liquidity_delta_pct": 0.0,
    "bonding_curve_fill_pct": 0.0,
    "score_slope": 0.0,
    "score_acceleration": 0.0,
    "peak_score": 0.0,
    "time_since_peak_s": 0.0,
    "updated_ts": 0.0,
}


def upsert_token_lifecycle(mint: str, **fields):
    mint = str(mint or "").strip()
    if not mint:
        raise ValueError("mint is required")
    row = {k: v for k, v in fields.items() if k in _TOKEN_LIFECYCLE_DEFAULTS}
    row.setdefault("last_updated_ts", time.time())
    cols = ["mint"] + list(row.keys())
    values = [mint] + [row[col] for col in row]
    update_cols = list(row.keys())
    _exec(
        f"INSERT INTO token_lifecycle({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(mint) DO UPDATE SET "
        + ", ".join(f"{col}=excluded.{col}" for col in update_cols),
        tuple(values),
    )


def upsert_token_trade_metrics(mint: str, **fields):
    mint = str(mint or "").strip()
    if not mint:
        raise ValueError("mint is required")
    row = {k: v for k, v in fields.items() if k in _TOKEN_METRIC_DEFAULTS}
    row.setdefault("updated_ts", time.time())
    cols = ["mint"] + list(row.keys())
    values = [mint] + [row[col] for col in row]
    _exec(
        f"INSERT INTO token_trade_metrics({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(mint) DO UPDATE SET "
        + ", ".join(f"{col}=excluded.{col}" for col in row.keys()),
        tuple(values),
    )


def append_token_event(mint: str, event_type: str, payload: dict | None = None, ts: float | None = None):
    mint = str(mint or "").strip()
    if not mint:
        raise ValueError("mint is required")
    _exec(
        "INSERT INTO token_events(mint, event_type, ts, payload_json) VALUES(?,?,?,?)",
        (mint, str(event_type or "").strip(), ts or time.time(), json.dumps(payload or {})),
    )


def upsert_token_enrichment(mint: str, **fields):
    mint = str(mint or "").strip()
    if not mint:
        raise ValueError("mint is required")
    row = {
        "rugcheck_json": json.dumps(fields.get("rugcheck", {})),
        "dex_json": json.dumps(fields.get("dex", {})),
        "pump_json": json.dumps(fields.get("pump", {})),
        "wallet_json": json.dumps(fields.get("wallet", {})),
        "updated_ts": fields.get("updated_ts", time.time()),
    }
    cols = ["mint"] + list(row.keys())
    values = [mint] + [row[col] for col in row]
    _exec(
        f"INSERT INTO token_enrichment({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(mint) DO UPDATE SET "
        + ", ".join(f"{col}=excluded.{col}" for col in row.keys()),
        tuple(values),
    )


def get_token_lifecycle(mint: str) -> dict | None:
    row = _fetchone("SELECT * FROM token_lifecycle WHERE mint=?", (mint,))
    return dict(row) if row else None


def get_token_trade_metrics(mint: str) -> dict | None:
    row = _fetchone("SELECT * FROM token_trade_metrics WHERE mint=?", (mint,))
    return dict(row) if row else None


def get_token_enrichment(mint: str) -> dict | None:
    row = _fetchone("SELECT * FROM token_enrichment WHERE mint=?", (mint,))
    if not row:
        return None
    data = dict(row)
    return {
        "mint": data["mint"],
        "rugcheck": _decode_json_value(data.get("rugcheck_json"), {}),
        "dex": _decode_json_value(data.get("dex_json"), {}),
        "pump": _decode_json_value(data.get("pump_json"), {}),
        "wallet": _decode_json_value(data.get("wallet_json"), {}),
        "updated_ts": data.get("updated_ts"),
    }


def get_token_events(mint: str, limit: int = 50) -> list[dict]:
    rows = _fetchall(
        "SELECT * FROM token_events WHERE mint=? ORDER BY ts DESC, id DESC LIMIT ?",
        (mint, max(1, min(limit, 500))),
    )
    items = []
    for row in rows:
        data = dict(row)
        data["payload"] = _decode_json_value(data.pop("payload_json", "{}"), {})
        items.append(data)
    return items


def get_token_snapshot(mint: str) -> dict | None:
    lifecycle = get_token_lifecycle(mint)
    if not lifecycle:
        return None
    return {
        "mint": mint,
        "lifecycle": lifecycle,
        "metrics": get_token_trade_metrics(mint) or {},
        "enrichment": get_token_enrichment(mint) or {
            "mint": mint,
            "rugcheck": {},
            "dex": {},
            "pump": {},
            "wallet": {},
            "updated_ts": None,
        },
        "events": get_token_events(mint, limit=25),
    }


def list_token_snapshots(limit: int = 50, states: list[str] | None = None) -> list[dict]:
    where = ""
    params: list = []
    if states:
        placeholders = ", ".join("?" * len(states))
        where = f"WHERE state IN ({placeholders})"
        params.extend(states)
    rows = _fetchall(
        "SELECT mint FROM token_lifecycle "
        f"{where} "
        "ORDER BY COALESCE(last_trade_ts, launch_ts, last_updated_ts) DESC LIMIT ?",
        tuple(params + [max(1, min(limit, 500))]),
    )
    return [snapshot for row in rows if (snapshot := get_token_snapshot(row["mint"]))]


# ── Settings (global_settings.json replacement) ───────────────────────────────

def get_setting(key: str, default=None):
    """Get a setting value (JSON-decoded). Returns default if not found."""
    row = _fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value):
    """Upsert a setting (value is JSON-encoded)."""
    _exec(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def delete_setting(key: str):
    _exec("DELETE FROM settings WHERE key=?", (key,))


def get_all_settings() -> dict:
    """Return all settings as {key: decoded_value}."""
    rows = _fetchall("SELECT key, value FROM settings")
    return {r["key"]: json.loads(r["value"]) for r in rows}


# ── Wallet alerts ─────────────────────────────────────────────────────────────

def get_wallet_alerts(uid: int) -> list[dict]:
    rows = _fetchall(
        "SELECT wallet, label FROM wallet_alerts WHERE uid=?", (uid,)
    )
    return [dict(r) for r in rows]


def add_wallet_alert(uid: int, wallet: str, label: str = ""):
    wallet = str(wallet or "").strip()
    if not is_valid_solana_address(wallet):
        raise ValueError(f"Invalid Solana wallet address: {wallet}")
    _exec(
        "INSERT INTO wallet_alerts(uid, wallet, label) VALUES(?,?,?) "
        "ON CONFLICT(uid, wallet) DO UPDATE SET label=excluded.label",
        (uid, wallet, (label or wallet[:8]).strip()),
    )


def remove_wallet_alert(uid: int, wallet: str):
    _exec("DELETE FROM wallet_alerts WHERE uid=? AND wallet=?", (uid, wallet))


def get_all_wallet_alerts() -> dict:
    """Return {uid: [{wallet, label}]} for all users."""
    rows = _fetchall("SELECT uid, wallet, label FROM wallet_alerts")
    result: dict = {}
    for r in rows:
        result.setdefault(r["uid"], []).append({"wallet": r["wallet"], "label": r["label"]})
    return result


def cleanup_invalid_wallet_alerts(uid: int | None = None) -> int:
    """Delete malformed wallet alert rows and return the number removed."""
    if uid is None:
        rows = _fetchall("SELECT uid, wallet FROM wallet_alerts")
    else:
        rows = _fetchall("SELECT uid, wallet FROM wallet_alerts WHERE uid=?", (uid,))

    invalid_rows = [
        (int(row["uid"]), str(row["wallet"]))
        for row in rows
        if not is_valid_solana_address(row["wallet"])
    ]
    for row_uid, wallet in invalid_rows:
        _exec("DELETE FROM wallet_alerts WHERE uid=? AND wallet=?", (row_uid, wallet))
    return len(invalid_rows)
