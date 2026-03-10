"""
migrate_json_to_sqlite.py — One-shot JSON → SQLite migration.

Safe to re-run: uses INSERT OR IGNORE / ON CONFLICT DO NOTHING everywhere.
Source JSON files are NOT deleted — kept as backups.
Run with: python3 migrate_json_to_sqlite.py
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import db

DATA = Path(__file__).parent / "data"
BACKUP = DATA / "backup_pre_sqlite"


def _load(filename: str) -> dict | list | None:
    path = DATA / filename
    if not path.exists():
        print(f"  [SKIP] {filename} not found")
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"  [ERROR] {filename} is malformed: {e}")
        return None


def _backup():
    BACKUP.mkdir(exist_ok=True)
    for f in DATA.glob("*.json"):
        dest = BACKUP / f.name
        if not dest.exists():
            shutil.copy2(f, dest)
    print(f"  JSON backups written to {BACKUP}/")


def migrate_portfolios() -> int:
    data = _load("portfolios.json")
    if not data:
        return 0
    count = 0
    for uid_str, holdings in data.items():
        uid = int(uid_str)
        for asset, amount in holdings.items():
            if amount and amount > 0:
                db.set_asset(uid, asset, float(amount))
                count += 1
    return count


def migrate_trade_log() -> int:
    data = _load("trade_log.json")
    if not data:
        return 0
    trades = data.get("trades", data) if isinstance(data, dict) else data
    if not isinstance(trades, list):
        print("  [WARN] trade_log.json has unexpected format")
        return 0
    count = 0
    for t in trades:
        try:
            db.log_trade(
                uid=int(t.get("uid", 0)),
                mode=t.get("mode", "paper"),
                action=t.get("action", "buy"),
                mint=t.get("mint", ""),
                symbol=t.get("symbol", ""),
                ts=t.get("ts", time.time()),
                date=t.get("date", ""),
                name=t.get("name"),
                narrative=t.get("narrative"),
                heat_score=t.get("heat_score"),
                sol_amount=t.get("sol_amount"),
                sol_received=t.get("sol_received"),
                token_amount=t.get("token_amount"),
                price_usd=t.get("price_usd"),
                buy_price_usd=t.get("buy_price_usd"),
                mcap=t.get("mcap"),
                pnl_pct=t.get("pnl_pct"),
                tx_sig=t.get("tx_sig"),
            )
            count += 1
        except Exception as e:
            print(f"  [WARN] skipped trade entry: {e}")
    return count


def migrate_auto_sell() -> int:
    data = _load("auto_sell.json")
    if not data:
        return 0
    count = 0
    for uid_str, positions in data.items():
        uid = int(uid_str)
        for mint, config in positions.items():
            symbol = config.get("symbol", "")
            db.set_auto_sell(uid, mint, config, symbol)
            count += 1
    return count


def migrate_auto_buy() -> int:
    data = _load("auto_buy.json")
    if not data:
        return 0
    count = 0
    for uid_str, cfg in data.items():
        uid = int(uid_str)
        db.set_auto_buy_config(
            uid,
            enabled=bool(cfg.get("enabled", False)),
            sol_amount=float(cfg.get("sol_amount", 0.03)),
            min_score=int(cfg.get("min_score", 55)),
            max_mcap=float(cfg.get("max_mcap", 500_000)),
            daily_limit_sol=float(cfg.get("daily_limit_sol", 1.0)),
            spent_today=float(cfg.get("spent_today", 0.0)),
            spent_date=cfg.get("spent_date"),
            max_positions=int(cfg.get("max_positions", 5)),
            buy_tier=cfg.get("buy_tier", "warm"),
        )
        count += 1
        for mint in cfg.get("bought", []):
            try:
                from db import _exec
                _exec(
                    "INSERT INTO auto_buy_history(uid, mint, bought_at, sol_spent) "
                    "VALUES(?,?,?,?) ON CONFLICT(uid, mint) DO NOTHING",
                    (uid, mint, time.time(), 0.0),
                )
                count += 1
            except Exception as e:
                print(f"  [WARN] auto_buy history entry failed: {e}")
    return count


def migrate_scanner_state() -> int:
    data = _load("scanner_state.json")
    if not data:
        return 0
    count = 0

    db.set_scanning(bool(data.get("scanning", True)))
    count += 1

    targets = data.get("scan_targets", [])
    db.set_scan_targets([int(t) for t in targets])
    count += len(targets)

    # seen_tokens: {mint: timestamp}
    now = time.time()
    for mint, ts in data.get("seen_tokens", {}).items():
        if isinstance(ts, (int, float)) and (now - ts) < db.SEEN_TOKEN_TTL:
            from db import _exec
            _exec(
                "INSERT INTO scanner_seen(mint, seen_at) VALUES(?,?) "
                "ON CONFLICT(mint) DO UPDATE SET seen_at=excluded.seen_at",
                (mint, float(ts)),
            )
            count += 1

    # watchlist: {mint: {...}}
    for mint, wdata in data.get("watchlist", {}).items():
        db.add_to_watchlist(mint, wdata)
        count += 1

    # user_min_score
    for uid_str, score in data.get("user_min_score", {}).items():
        db.set_user_min_score(int(uid_str), int(score))
        count += 1

    # alert_channel
    ch = data.get("alert_channel")
    if ch:
        db.set_alert_channel(ch)
        count += 1

    return count


def migrate_scanner_log() -> int:
    data = _load("scanner_log.json")
    if not isinstance(data, list) or not data:
        return 0
    count = 0
    for entry in data[-500:]:  # keep last 500 only
        try:
            db.append_scan_log(entry)
            count += 1
        except Exception as e:
            print(f"  [WARN] scanner_log entry skipped: {e}")
    return count


def migrate_global_settings() -> int:
    data = _load("global_settings.json")
    if not data:
        return 0
    count = 0
    for key, value in data.items():
        db.set_setting(key, value)
        count += 1
    return count


def migrate_wallet_alerts() -> int:
    data = _load("user_wallet_alerts.json")
    if not data:
        return 0
    count = 0
    # Format: {uid_str: {wallet: label}} or {uid_str: [wallet, ...]}
    for uid_str, entries in data.items():
        uid = int(uid_str)
        if isinstance(entries, dict):
            for wallet, label in entries.items():
                db.add_wallet_alert(uid, wallet, label or "")
                count += 1
        elif isinstance(entries, list):
            for wallet in entries:
                db.add_wallet_alert(uid, wallet, "")
                count += 1
    return count


def verify(counts: dict):
    print("\n── Verification ─────────────────────────────────────────────────")
    from db import _fetchone
    checks = {
        "portfolios rows":      "SELECT COUNT(*) as n FROM portfolios",
        "trades rows":          "SELECT COUNT(*) as n FROM trades",
        "auto_sell rows":       "SELECT COUNT(*) as n FROM auto_sell",
        "auto_buy_config rows": "SELECT COUNT(*) as n FROM auto_buy_config",
        "auto_buy_history rows":"SELECT COUNT(*) as n FROM auto_buy_history",
        "scanner_seen rows":    "SELECT COUNT(*) as n FROM scanner_seen",
        "scanner_watchlist rows":"SELECT COUNT(*) as n FROM scanner_watchlist",
        "scanner_log rows":     "SELECT COUNT(*) as n FROM scanner_log",
        "settings rows":        "SELECT COUNT(*) as n FROM settings",
        "wallet_alerts rows":   "SELECT COUNT(*) as n FROM wallet_alerts",
    }
    for label, sql in checks.items():
        row = _fetchone(sql)
        n = row["n"] if row else 0
        print(f"  {label}: {n}")


def main():
    print("── meme-trade-bot: JSON → SQLite migration ─────────────────────────")
    print(f"  Database: {db.DB_PATH}")

    print("\n[1] Initialising schema …")
    db.init()

    print("\n[2] Backing up JSON files …")
    _backup()

    steps = [
        ("portfolios.json",      migrate_portfolios),
        ("trade_log.json",       migrate_trade_log),
        ("auto_sell.json",       migrate_auto_sell),
        ("auto_buy.json",        migrate_auto_buy),
        ("scanner_state.json",   migrate_scanner_state),
        ("scanner_log.json",     migrate_scanner_log),
        ("global_settings.json", migrate_global_settings),
        ("user_wallet_alerts.json", migrate_wallet_alerts),
    ]

    counts: dict = {}
    print("\n[3] Migrating …")
    for name, fn in steps:
        n = fn()
        counts[name] = n
        print(f"  {name}: {n} records")

    verify(counts)
    print("\nMigration complete. Original JSON files preserved in data/backup_pre_sqlite/")
    print("You can now start the bot — it will use SQLite for all state.")


if __name__ == "__main__":
    main()
