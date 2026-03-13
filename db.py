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
                entry_source_rank INTEGER,
                entry_confidence REAL,
                exit_reason TEXT,
                exit_trigger TEXT,
                exit_score_effective INTEGER,
                exit_mcap REAL,
                hold_seconds REAL
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
                exit_reason           TEXT
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
                min_score          INTEGER NOT NULL DEFAULT 55,
                max_mcap           REAL    NOT NULL DEFAULT 500000,
                min_mcap_usd       REAL    NOT NULL DEFAULT 0,
                daily_limit_sol    REAL    NOT NULL DEFAULT 1.0,
                spent_today        REAL    NOT NULL DEFAULT 0.0,
                spent_date         TEXT,
                max_positions      INTEGER NOT NULL DEFAULT 5,
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
        """)
        c.commit()

    # Migrate existing auto_buy_config tables that predate the new filter columns
    _new_ab_cols = [
        ("min_mcap_usd",      "REAL    NOT NULL DEFAULT 0"),
        ("min_liquidity_usd", "REAL    NOT NULL DEFAULT 0"),
        ("max_liquidity_usd", "REAL    NOT NULL DEFAULT 0"),
        ("min_age_mins",      "INTEGER NOT NULL DEFAULT 0"),
        ("max_age_mins",      "INTEGER NOT NULL DEFAULT 0"),
        ("min_txns_5m",       "INTEGER NOT NULL DEFAULT 0"),
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
        ("entry_source_rank", "INTEGER"),
        ("entry_confidence", "REAL"),
        ("exit_reason", "TEXT"),
        ("exit_trigger", "TEXT"),
        ("exit_score_effective", "INTEGER"),
        ("exit_mcap", "REAL"),
        ("hold_seconds", "REAL"),
    ]
    for col, defn in _new_trade_cols:
        try:
            with _conn() as c:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} {defn}")
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
    """Delete all holdings for uid and set SOL to starting_sol."""
    _exec("DELETE FROM portfolios WHERE uid=?", (uid,))
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
        "entry_source_rank": kwargs.get("entry_source_rank"),
        "entry_confidence": kwargs.get("entry_confidence"),
        "exit_reason": kwargs.get("exit_reason"),
        "exit_trigger": kwargs.get("exit_trigger"),
        "exit_score_effective": kwargs.get("exit_score_effective"),
        "exit_mcap": kwargs.get("exit_mcap"),
        "hold_seconds": kwargs.get("hold_seconds"),
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
    where = "uid=?"
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
                        entry_confidence, entry_archetype, exit_reason
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                        trade.get("exit_reason") or trade.get("exit_trigger"),
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
    "min_score": 55,
    "max_mcap": 500_000,
    "min_mcap_usd": 0,
    "daily_limit_sol": 1.0,
    "spent_today": 0.0,
    "spent_date": None,
    "max_positions": 5,
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
    return d


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


def mark_scan_log_alerted(mint: str):
    """Mark the most recent scanner_log row for this mint as alerted=1."""
    _exec(
        "UPDATE scanner_log SET alerted=1 WHERE id=("
        "SELECT MAX(id) FROM scanner_log WHERE mint=?)",
        (mint,),
    )


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
    _exec(
        "INSERT INTO wallet_alerts(uid, wallet, label) VALUES(?,?,?) "
        "ON CONFLICT(uid, wallet) DO UPDATE SET label=excluded.label",
        (uid, wallet, label),
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
