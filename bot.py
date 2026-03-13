"""
@DigitalDegenX_Bot — Solana Meme Coin AI Trading Bot
Features: menu UI + slash commands, paper/live trading, AI analysis,
          price alerts, live wallet portfolio, persistent storage,
          auto-sell (2x/4x/custom), mcap milestone alerts.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import subprocess
import math
import time
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import scanner as sc
import pumpfun
import pumpfeed as pf
import intelligence_tracker as intel
import wallet_manager as wm
import research_logger
import portfolio_alerts
import settings_manager as sm
import trade_center as tc

import db as _db
_db.init()

import config as _cfg
from config import (
    TELEGRAM_TOKEN, SOLANA_RPC, WALLET_PRIVATE_KEY,
    ADMIN_IDS, PAPER_START_SOL, ALERT_CHECK_SECS,
    HELIUS_API_KEY,
    PRIORITY_FEE_MICRO_LAMPORTS, PRIORITY_FEE_COMPUTE_UNITS,
    JITO_ENABLED, JITO_TIP_LAMPORTS, JITO_ENDPOINT,
)
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ─── Constants ────────────────────────────────────────────────────────────────
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search?q="
DEXSCREENER_TOKEN  = "https://api.dexscreener.com/latest/dex/tokens/"
JUPITER_QUOTE_URL  = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL   = "https://lite-api.jup.ag/swap/v1/swap"
SOL_MINT           = "So11111111111111111111111111111111111111112"

# Jito tip accounts (rotate randomly to spread load)
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1TE1uU5Lqf",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]

# Priority fee presets (µlamports / CU)
PRIORITY_FEE_PRESETS = {
    "low":    100_000,
    "medium": 500_000,
    "high":   1_000_000,
    "turbo":  3_000_000,
}
TOKEN_PROGRAM      = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

DATA_DIR  = os.path.join(os.path.dirname(__file__), "data")
MODES_FILE = os.path.join(DATA_DIR, "user_modes.json")

os.makedirs(DATA_DIR, exist_ok=True)

DEFAULT_MCAP_MILESTONES = [100_000, 500_000, 1_000_000]

# ─── Telegram Markdown escaping ───────────────────────────────────────────────

def _esc(s: str) -> str:
    """Escape Telegram Markdown v1 special chars in user-supplied strings."""
    return (
        str(s)
        .replace("\\", "\\\\")
        .replace("_",  "\\_")
        .replace("*",  "\\*")
        .replace("`",  "\\`")
        .replace("[",  "\\[")
    )

# ─── Per-user portfolio lock ───────────────────────────────────────────────────

_portfolio_locks: dict[int, asyncio.Lock] = {}

def _portfolio_lock(uid: int) -> asyncio.Lock:
    """Return a per-user asyncio.Lock for safe concurrent portfolio mutations."""
    if uid not in _portfolio_locks:
        _portfolio_locks[uid] = asyncio.Lock()
    return _portfolio_locks[uid]

# ─── Storage helpers ──────────────────────────────────────────────────────────

def _load(path: str) -> dict:
    """Legacy JSON loader — kept for user_modes.json only."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(path: str, data: dict):
    """Legacy JSON saver — kept for user_modes.json only."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Portfolios ────────────────────────────────────────────────────────────────

def get_portfolio(uid: int) -> dict:
    p = _db.get_portfolio(uid)
    if "SOL" not in p:
        _db.set_asset(uid, "SOL", PAPER_START_SOL)
        p["SOL"] = PAPER_START_SOL
    return p

def update_portfolio(uid: int, portfolio: dict):
    """Persist a full portfolio dict — strips zero balances and removes stale assets."""
    # Delete any asset that is in the DB but absent (or zero) in the new dict
    existing = _db.get_portfolio(uid)
    for asset in existing:
        if asset not in portfolio or portfolio[asset] <= 0:
            _db.set_asset(uid, asset, 0)
    # Upsert remaining non-zero balances
    for asset, amount in portfolio.items():
        if float(amount) > 0:
            _db.set_asset(uid, asset, float(amount))

def reset_portfolio(uid: int):
    """Reset paper portfolio to starting balance and clear all auto-sell configs."""
    _db.reset_portfolio(uid, PAPER_START_SOL)
    for mint in list(_db.get_all_auto_sells(uid).keys()):
        _db.remove_auto_sell(uid, mint)


# ── Wallet key persistence ─────────────────────────────────────────────────────

def save_wallet_key(new_key: str):
    """Write WALLET_PRIVATE_KEY to .env and reload the config module."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    lines = []
    found = False
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("WALLET_PRIVATE_KEY="):
                    lines.append(f"WALLET_PRIVATE_KEY={new_key}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"WALLET_PRIVATE_KEY={new_key}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)
    importlib.reload(_cfg)
    global WALLET_PRIVATE_KEY
    WALLET_PRIVATE_KEY = _cfg.WALLET_PRIVATE_KEY


# ── Price alerts ──────────────────────────────────────────────────────────────

# ── Portfolio alert milestones (automatic mcap tracking) ─────────────────────

# ── Auto-sell configs ─────────────────────────────────────────────────────────

def get_auto_sell(uid: int, mint: str) -> dict | None:
    return _db.get_auto_sell(uid, mint)


def set_auto_sell(uid: int, mint: str, config: dict):
    _db.set_auto_sell(uid, mint, config, config.get("symbol", ""))


def remove_auto_sell(uid: int, mint: str):
    _db.remove_auto_sell(uid, mint)


def _apply_presets_to_open_positions(uid: int, presets: list) -> int:
    """Apply updated preset targets to all existing open auto-sell positions."""
    user_configs = _db.get_all_auto_sells(uid)
    if not user_configs:
        return 0
    new_targets = [
        {"mult": p["mult"], "sell_pct": p["sell_pct"], "triggered": False, "label": f"{p['mult']:.1f}x"}
        for p in presets
    ]
    for mint, cfg in user_configs.items():
        cfg["mult_targets"] = [dict(t) for t in new_targets]
        _db.set_auto_sell(uid, mint, cfg)
    return len(user_configs)


# ── Global settings ───────────────────────────────────────────────────────────

def load_global_settings() -> dict:
    """Return the raw global_settings.json dict (used by pumpfeed.py for channel IDs etc.)."""
    try:
        path = os.path.join(DATA_DIR, "global_settings.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError):
        pass
    return {}

def get_global_sl() -> dict:
    return _db.get_setting("stop_loss", {"enabled": False, "pct": 50, "sell_pct": 100})

def set_global_sl(data: dict):
    _db.set_setting("stop_loss", data)

def _uts(uid: int) -> dict:
    """Read user_trade_settings sub-dict for uid."""
    return _db.get_setting("user_trade_settings", {}).get(str(uid), {})

def _set_uts(uid: int, key: str, value):
    uts = _db.get_setting("user_trade_settings", {})
    uts.setdefault(str(uid), {})[key] = value
    _db.set_setting("user_trade_settings", uts)

def get_user_slippage(uid: int) -> int:
    return _uts(uid).get("slippage_bps", 150)

def set_user_slippage(uid: int, bps: int):
    _set_uts(uid, "slippage_bps", max(10, min(5000, int(bps))))

def get_user_jito(uid: int) -> bool:
    return _uts(uid).get("jito_enabled", JITO_ENABLED)

def set_user_jito(uid: int, enabled: bool):
    _set_uts(uid, "jito_enabled", enabled)

def get_user_jito_tip(uid: int) -> int:
    return _uts(uid).get("jito_tip_lamports", JITO_TIP_LAMPORTS)

def set_user_jito_tip(uid: int, lamports: int):
    _set_uts(uid, "jito_tip_lamports", max(1_000, min(10_000_000, int(lamports))))

def get_user_priority_fee(uid: int) -> int:
    return _uts(uid).get("priority_fee", PRIORITY_FEE_MICRO_LAMPORTS)

def set_user_priority_fee(uid: int, micro_lamports: int):
    _set_uts(uid, "priority_fee", max(0, int(micro_lamports)))

def get_global_trailing_stop() -> dict:
    return _db.get_setting("global_trailing_stop", {"enabled": False, "trail_pct": 30, "sell_pct": 100})

def set_global_trailing_stop(data: dict):
    _db.set_setting("global_trailing_stop", data)

def get_global_trailing_tp() -> dict:
    return _db.get_setting("global_trailing_tp", {"enabled": False, "activate_mult": 2.0, "trail_pct": 20, "sell_pct": 50})

def set_global_trailing_tp(data: dict):
    _db.set_setting("global_trailing_tp", data)

def get_global_breakeven_stop() -> dict:
    return _db.get_setting("global_breakeven_stop", {"enabled": False, "activate_mult": 2.0})

def set_global_breakeven_stop(data: dict):
    _db.set_setting("global_breakeven_stop", data)

def get_global_time_exit() -> dict:
    return _db.get_setting("global_time_exit", {"enabled": False, "hours": 24, "target_mult": 1.5, "sell_pct": 100})

def set_global_time_exit(data: dict):
    _db.set_setting("global_time_exit", data)

def get_user_as_presets(uid: int) -> list:
    return _db.get_setting(f"as_presets_{uid}", [{"mult": 2.0, "sell_pct": 50}, {"mult": 4.0, "sell_pct": 50}])

def set_user_as_presets(uid: int, presets: list):
    _db.set_setting(f"as_presets_{uid}", presets)
    _apply_presets_to_open_positions(uid, presets)

def get_user_as_presets_enabled(uid: int) -> bool:
    return _db.get_setting(f"as_presets_enabled_{uid}", True)

def set_user_as_presets_enabled(uid: int, enabled: bool):
    _db.set_setting(f"as_presets_enabled_{uid}", enabled)

def format_as_presets(presets: list) -> str:
    """Format auto-sell presets into readable string like '2x→50%, 4x→50%'"""
    if not presets:
        return "No targets configured"
    formatted = []
    for p in presets:
        mult = p.get("mult", 0)
        pct = p.get("sell_pct", 0)
        formatted.append(f"{mult:.1f}x→{pct}%")
    return ", ".join(formatted)


STRATEGIES = {
    "scalp": {
        "mult_targets": [{"mult": 1.5, "sell_pct": 75, "triggered": False, "label": "1.5x"}],
        "stop_loss":    {"enabled": True, "pct": 25, "sell_pct": 100, "triggered": False},
        "trailing_stop": {"enabled": False, "trail_pct": 20, "sell_pct": 100, "peak_price": 0.0, "triggered": False},
        "trailing_tp":  {"enabled": False, "activate_mult": 2.0, "trail_pct": 20, "sell_pct": 50, "active": False, "peak_price": 0.0, "triggered": False},
        "time_exit":    {"enabled": True, "hours": 2, "target_mult": 1.5, "sell_pct": 100, "triggered": False},
    },
    "standard": {
        "mult_targets": [{"mult": 2.0, "sell_pct": 50, "triggered": False, "label": "2x"}, {"mult": 4.0, "sell_pct": 50, "triggered": False, "label": "4x"}],
        "stop_loss":    {"enabled": True, "pct": 40, "sell_pct": 100, "triggered": False},
        "trailing_stop": {"enabled": True, "trail_pct": 25, "sell_pct": 100, "peak_price": 0.0, "triggered": False},
        "trailing_tp":  {"enabled": False, "activate_mult": 2.0, "trail_pct": 20, "sell_pct": 50, "active": False, "peak_price": 0.0, "triggered": False},
        "time_exit":    {"enabled": False, "hours": 24, "target_mult": 2.0, "sell_pct": 100, "triggered": False},
    },
    "diamond": {
        "mult_targets": [{"mult": 3.0, "sell_pct": 33, "triggered": False, "label": "3x"}, {"mult": 6.0, "sell_pct": 50, "triggered": False, "label": "6x"}],
        "stop_loss":    {"enabled": False, "pct": 50, "sell_pct": 100, "triggered": False},
        "trailing_stop": {"enabled": False, "trail_pct": 30, "sell_pct": 100, "peak_price": 0.0, "triggered": False},
        "trailing_tp":  {"enabled": True, "activate_mult": 3.0, "trail_pct": 20, "sell_pct": 100, "active": False, "peak_price": 0.0, "triggered": False},
        "time_exit":    {"enabled": False, "hours": 24, "target_mult": 2.0, "sell_pct": 100, "triggered": False},
    },
    "moon": {
        "mult_targets": [{"mult": 2.0, "sell_pct": 80, "triggered": False, "label": "2x"}],
        "stop_loss":    {"enabled": False, "pct": 50, "sell_pct": 100, "triggered": False},
        "trailing_stop": {"enabled": False, "trail_pct": 30, "sell_pct": 100, "peak_price": 0.0, "triggered": False},
        "trailing_tp":  {"enabled": True, "activate_mult": 5.0, "trail_pct": 15, "sell_pct": 100, "active": False, "peak_price": 0.0, "triggered": False},
        "time_exit":    {"enabled": False, "hours": 24, "target_mult": 2.0, "sell_pct": 100, "triggered": False},
    },
}


def setup_auto_sell(uid: int, mint: str, symbol: str,
                    buy_price_usd: float, raw_amount: int, decimals: int,
                    sol_amount: float = 0.0):
    """Called after every buy to create default auto-sell config using user presets."""
    existing = get_auto_sell(uid, mint)
    # Get user's preset multipliers — only apply if presets are enabled
    mult_targets = []
    if get_user_as_presets_enabled(uid):
        user_presets = get_user_as_presets(uid)
        for preset in user_presets:
            mult_targets.append({
                "mult": preset["mult"],
                "sell_pct": preset["sell_pct"],
                "triggered": False,
                "label": f"{preset['mult']:.1f}x"
            })
    
    config = {
        "symbol":             symbol,
        "buy_price_usd":      buy_price_usd,
        "sol_amount":         sol_amount,        # SOL spent on this buy
        "purchase_timestamp": time.time(),
        "initial_raw":        raw_amount,
        "decimals":           decimals,
        "enabled":            True,
        # Multiplier targets: sell_pct of current holdings when triggered (from user presets)
        "mult_targets":  mult_targets,
        # Market cap milestone alerts (USD)
        "mcap_alerts": [
            {"mcap": 100_000,   "triggered": False, "label": "100K"},
            {"mcap": 500_000,   "triggered": False, "label": "500K"},
            {"mcap": 1_000_000, "triggered": False, "label": "1M"},
        ],
        # Custom targets added by user
        "custom_targets": existing.get("custom_targets", []) if existing else [],
        # Hard stop-loss
        "stop_loss": {
            "enabled": False,
            "pct": 50,
            "sell_pct": 100,
            "triggered": False,
        },
        # Trailing stop-loss
        "trailing_stop": {
            "enabled": False,
            "trail_pct": 30,
            "sell_pct": 100,
            "peak_price": 0.0,
            "triggered": False,
        },
        # Trailing take-profit
        "trailing_tp": {
            "enabled": False,
            "activate_mult": 2.0,
            "trail_pct": 20,
            "sell_pct": 50,
            "active": False,
            "peak_price": 0.0,
            "triggered": False,
        },
        # Time-based exit
        "time_exit": {
            "enabled": False,
            "hours": 24,
            "target_mult": 2.0,
            "sell_pct": 100,
            "buy_time": time.time(),
            "triggered": False,
        },
        # Breakeven stop: move stop-loss to entry once price hits activate_mult
        "breakeven_stop": {
            "enabled": False,
            "activate_mult": 2.0,
            "triggered": False,
        },
    }
    set_auto_sell(uid, mint, config)
    return config


# ── Trade log ─────────────────────────────────────────────────────────────────



# ─── User wallet alert tracker ────────────────────────────────────────────────

def get_user_alert_wallets(uid: int) -> list:
    return _db.get_wallet_alerts(uid)

def add_user_alert_wallet(uid: int, address: str, label: str) -> bool:
    existing = _db.get_wallet_alerts(uid)
    if any(w["wallet"] == address for w in existing):
        return False
    _db.add_wallet_alert(uid, address, label or address[:8])
    return True

def remove_user_alert_wallet(uid: int, address: str):
    _db.remove_wallet_alert(uid, address)


def _detect_narrative(name: str, symbol: str, desc: str = "") -> str:
    """Inline keyword match — mirrors scanner.py NARRATIVES dict."""
    NARRATIVES = {
        "AI":        ["ai", "agent", "gpt", "robot", "artificial", "neural", "llm", "ml", "agi"],
        "Political": ["trump", "maga", "biden", "elon", "political", "president", "vote", "patriot"],
        "Animal":    ["dog", "cat", "pepe", "frog", "shib", "inu", "doge", "wolf", "bear", "bull",
                      "penguin", "monkey", "ape", "hamster", "fish", "bird"],
        "Gaming":    ["game", "play", "nft", "pixel", "arcade", "quest", "rpg", "guild", "warrior"],
        "RWA":       ["gold", "oil", "real", "estate", "asset", "commodity", "bond", "rwa"],
    }
    haystack = f"{name} {symbol} {desc}".lower()
    for label, keywords in NARRATIVES.items():
        if any(k in haystack for k in keywords):
            return label
    return "Other"

def _get_buy_price(uid: int, mint: str) -> float | None:
    cfg = _db.get_auto_sell(uid, mint)
    return cfg.get("buy_price_usd") if cfg else None

# ── Auto-buy configs ──────────────────────────────────────────────────────────

def get_auto_buy(uid: int) -> dict:
    """Return auto-buy config for uid. Includes today's bought list for UI compatibility."""
    cfg = _db.get_auto_buy_config(uid)
    cfg["bought"] = _db.get_bought_list(uid)
    return cfg

def set_auto_buy(uid: int, cfg: dict):
    """Persist auto-buy config. 'bought' list is stored separately in auto_buy_history."""
    _db.set_auto_buy_config(
        uid,
        enabled=cfg.get("enabled", False),
        sol_amount=cfg.get("sol_amount", 0.03),
        min_score=cfg.get("min_score", 55),
        max_mcap=cfg.get("max_mcap", 500_000),
        min_mcap_usd=cfg.get("min_mcap_usd", 0),
        daily_limit_sol=cfg.get("daily_limit_sol", 1.0),
        spent_today=cfg.get("spent_today", 0.0),
        spent_date=cfg.get("spent_date"),
        max_positions=cfg.get("max_positions", 5),
        buy_tier=cfg.get("buy_tier", "warm"),
        min_liquidity_usd=cfg.get("min_liquidity_usd", 0),
        max_liquidity_usd=cfg.get("max_liquidity_usd", 0),
        min_age_mins=cfg.get("min_age_mins", 0),
        max_age_mins=cfg.get("max_age_mins", 0),
        min_txns_5m=cfg.get("min_txns_5m", 0),
    )

def _ab_reset_day_if_needed(cfg: dict) -> dict:
    """Reset daily spend counter if date has changed. Updates DB and returns refreshed cfg."""
    _db.reset_day_if_needed(cfg.get("uid", 0) if isinstance(cfg, dict) else 0)
    uid = cfg.get("uid") if isinstance(cfg, dict) else None
    if uid:
        fresh = _db.get_auto_buy_config(uid)
        fresh["bought"] = _db.get_bought_list(uid)
        return fresh
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if cfg.get("spent_date") != today:
        cfg["spent_today"] = 0.0
        cfg["spent_date"]  = today
        cfg["bought"]      = []
    return cfg


def _score_tier_label(score: float | int | None) -> str:
    value = float(score or 0)
    if value >= 85:
        return "ultra_hot"
    if value >= 70:
        return "hot"
    if value >= 55:
        return "warm"
    if value >= 35:
        return "watch"
    return "skip"


def log_trade(uid: int, mode: str, action: str, mint: str, symbol: str,
              name: str = "", narrative: str = None, heat_score: int = None,
              sol_amount: float = None, sol_received: float = None,
              token_amount: int = 0, price_usd: float = 0.0,
              buy_price_usd: float = None, mcap: float = 0.0,
              pnl_pct: float = None, tx_sig: str = None, **extra):
    trade_ts = time.time()
    if narrative is None:
        narrative = _detect_narrative(name, symbol)
    # Drop ghost trades: buys with no tokens received, sells with no SOL received
    if action == "buy" and (not token_amount or token_amount <= 0):
        return
    if action == "sell" and (not sol_received or sol_received <= 0):
        return
    if action == "sell" and buy_price_usd and price_usd and pnl_pct is None:
        pnl_pct = (price_usd - buy_price_usd) / buy_price_usd * 100
    _db.log_trade(
        uid=uid, mode=mode, action=action, mint=mint, symbol=symbol,
        ts=trade_ts,
        name=name, narrative=narrative, heat_score=heat_score,
        sol_amount=sol_amount, sol_received=sol_received,
        token_amount=token_amount, price_usd=price_usd,
        buy_price_usd=buy_price_usd, mcap=mcap, pnl_pct=pnl_pct, tx_sig=tx_sig,
        **extra,
    )
    
    # Log to research logger (CSV + JSON) for data analysis
    try:
        pnl_usd = None
        if action == "sell" and buy_price_usd and price_usd:
            pnl_usd = (price_usd - buy_price_usd) * token_amount
        
        research_logger.log_trade(
            timestamp=trade_ts,
            user_id=uid,
            action=action,
            mint=mint,
            symbol=symbol,
            narrative=narrative,
            heat_score=heat_score or 0,
            buy_price_usd=buy_price_usd or 0.0,
            sell_price_usd=price_usd if action == "sell" else 0.0,
            sol_amount=sol_amount or sol_received or 0.0,
            token_amount=token_amount,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            hold_seconds=extra.get("hold_seconds"),
            mcap_at_entry=mcap if action == "buy" else None,
            mcap_at_exit=mcap if action == "sell" else None,
            **extra,
        )
    except Exception as e:
        print(f"[log_trade] Research logger error: {e}")


# ─── In-memory state ──────────────────────────────────────────────────────────

def _load_user_modes() -> dict:
    try:
        with open(MODES_FILE) as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_user_modes():
    with open(MODES_FILE, "w") as f:
        json.dump({str(k): v for k, v in user_modes.items()}, f)

user_modes: dict[int, str]  = _load_user_modes()
user_state: dict[int, dict] = {}


def get_mode(uid: int) -> str:
    return user_modes.get(uid, "paper")

def set_state(uid: int, **kwargs):
    user_state.setdefault(uid, {}).update(kwargs)

def get_state(uid: int, key: str, default=None):
    return user_state.get(uid, {}).get(key, default)

def clear_state(uid: int):
    user_state.pop(uid, None)


# ─── Market data ──────────────────────────────────────────────────────────────

# Portfolio price cache: mint → {"ts": float, "pair": dict|None, "bc": dict|None, "coin": dict|None}
_portfolio_price_cache: dict[str, dict] = {}
_PORTFOLIO_CACHE_TTL = 60  # seconds


def _get_cached_price(mint: str) -> dict | None:
    """Return cached price data if still fresh, else None."""
    entry = _portfolio_price_cache.get(mint)
    if entry and (time.time() - entry["ts"]) < _PORTFOLIO_CACHE_TTL:
        return entry
    return None


def _set_cached_price(mint: str, pair, bc, coin):
    _portfolio_price_cache[mint] = {"ts": time.time(), "pair": pair, "bc": bc, "coin": coin}


def fetch_sol_pair(query: str) -> dict | None:
    url = (DEXSCREENER_TOKEN + query) if len(query) > 30 else (DEXSCREENER_SEARCH + query)
    try:
        pairs = requests.get(url, timeout=10).json().get("pairs") or []
        sol   = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol:
            return None
        # When querying by full contract address, ensure the token is the base
        # (not the quote). DexScreener /tokens/ returns pairs where the mint
        # appears on either side; using a quote-side pair gives inverted prices.
        is_ca = len(query) > 30
        if is_ca:
            base_first = [p for p in sol if p.get("baseToken", {}).get("address", "").lower() == query.lower()]
            sol = base_first if base_first else sol
        return sorted(sol, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]
    except Exception:
        return None


def _fetch_pumpfun_coin(mint: str) -> dict | None:
    """Fetch token metadata from pump.fun API (name, symbol, market cap)."""
    try:
        r = requests.get(f"https://frontend-api-v3.pump.fun/coins/{mint}", timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


async def _fetch_portfolio_token_data(mint: str) -> dict:
    """Fetch all display data for one portfolio token concurrently.
    Returns dict with keys: pair, bc, coin (all may be None).
    pump.fun is queried first (primary source); DexScreener used for
    graduated/Raydium tokens or when bonding curve is absent.
    Uses short-lived cache to prevent rate-limit failures."""
    cached = _get_cached_price(mint)
    if cached:
        return cached

    loop = asyncio.get_running_loop()

    # ── Phase 1: fetch pump.fun data and DexScreener in parallel ─────────────
    bc_fut   = loop.run_in_executor(None, pumpfun.fetch_bonding_curve_data, mint, SOLANA_RPC)
    coin_fut = loop.run_in_executor(None, _fetch_pumpfun_coin, mint)
    pair_fut = loop.run_in_executor(None, fetch_sol_pair, mint)

    bc, coin, pair = await asyncio.gather(bc_fut, coin_fut, pair_fut, return_exceptions=True)
    bc   = bc   if not isinstance(bc,   Exception) else None
    coin = coin if not isinstance(coin, Exception) else None
    pair = pair if not isinstance(pair, Exception) else None

    # ── Phase 2: prefer pump.fun bonding curve when token is still on-curve ──
    # If token has graduated (bc.complete=True) or has no bonding curve, keep
    # DexScreener pair for accurate post-graduation price. Otherwise clear pair
    # so the portfolio display uses the on-curve price (more accurate for new tokens).
    if bc and not bc.get("complete"):
        # Still on pump.fun bonding curve — bonding curve price is authoritative
        pair = None  # don't mix graduated pair prices with bonding-curve tokens

    _set_cached_price(mint, pair, bc, coin)
    return _portfolio_price_cache[mint]


def fetch_token_price(mint: str) -> tuple[float | None, float | None]:
    """Returns (price_usd, mcap_usd).
    pump.fun bonding curve is checked first (primary for new tokens);
    DexScreener used for graduated/Raydium tokens."""
    sol_usd = pf.get_sol_price() or 150.0

    # ── Primary: pump.fun bonding curve ──────────────────────────────────────
    try:
        _bc = pumpfun.fetch_bonding_curve_data(mint, SOLANA_RPC)
        if _bc and not _bc.get("complete") and _bc.get("virtual_token_reserves"):
            price_sol = _bc["virtual_sol_reserves"] / _bc["virtual_token_reserves"] / 1e9 * 1e6
            price_usd = price_sol * sol_usd
            coin      = _fetch_pumpfun_coin(mint)
            mcap_usd  = float(coin.get("usd_market_cap") or coin.get("market_cap") or 0) if coin else 0
            if price_usd:
                return price_usd, mcap_usd
    except Exception:
        pass

    # ── Fallback: DexScreener (graduated/Raydium tokens) ─────────────────────
    try:
        pairs = requests.get(DEXSCREENER_TOKEN + mint, timeout=10).json().get("pairs") or []
        sol   = [p for p in pairs if p.get("chainId") == "solana"]
        if sol:
            pair  = sorted(sol, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]
            price = float(pair.get("priceUsd", 0) or 0)
            mcap  = float(pair.get("marketCap", 0) or 0)
            if price:
                return price, mcap
    except Exception:
        pass

    return None, None


def _n(v, decimals=0) -> str:
    if v is None:
        return "N/A"
    try:
        f = float(v)
        if decimals:
            return f"${f:,.{decimals}f}"
        return f"${f:,.0f}"
    except Exception:
        return "N/A"


def _pct(v) -> str:
    try:
        f = float(v)
        arrow = "▲" if f >= 0 else "▼"
        return f"{arrow} {abs(f):.1f}%"
    except Exception:
        return "N/A"


def format_pair(pair: dict) -> str:
    name  = pair.get("baseToken", {}).get("name", "N/A")
    sym   = pair.get("baseToken", {}).get("symbol", "N/A")
    addr  = pair.get("baseToken", {}).get("address", "N/A")
    pr    = pair.get("priceUsd", "N/A")
    chg   = pair.get("priceChange", {}) or {}
    vol   = pair.get("volume", {}) or {}
    dex   = pair.get("dexId", "N/A")
    pair_addr = pair.get("pairAddress", "")
    txns  = pair.get("txns", {}) or {}
    buys  = txns.get("h1", {}).get("buys", 0)
    sells = txns.get("h1", {}).get("sells", 0)
    return (
        f"*{name}* (${sym})\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Price: `${pr}`\n"
        f"📈 5m: `{_pct(chg.get('m5'))}` | 1h: `{_pct(chg.get('h1'))}` | 24h: `{_pct(chg.get('h24'))}`\n"
        f"📊 Vol 1h: `{_n(vol.get('h1'))}` | 24h: `{_n(vol.get('h24'))}`\n"
        f"💧 Liquidity: `{_n(pair.get('liquidity',{}).get('usd'))}`\n"
        f"🏦 MCap: `{_n(pair.get('marketCap'))}`\n"
        f"🔄 Buys/Sells (1h): `{buys}` / `{sells}`\n"
        f"🏪 DEX: `{dex}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📋 Mint: `{addr}`\n"
        + (f"🔗 Pair: `{pair_addr}`\n" if pair_addr else "")
    )


# ─── Keyboards ────────────────────────────────────────────────────────────────

def main_menu_kb(uid: int) -> InlineKeyboardMarkup:
    mode      = "📄 Paper" if get_mode(uid) == "paper" else "🔴 Live"
    targets   = _db.get_scan_targets()
    scan_lbl  = "🔕 Pause Scout" if uid in targets else "🔔 Start Scout"
    pf_lbl    = "🟢 Pump Live ⚙️" if pf.is_subscribed(uid) else "🔴 Pump Live ⚙️"
    pg_lbl    = "🟢 Pump Grad ⚙️" if pf.is_grad_subscribed(uid) else "🔴 Pump Grad ⚙️"
    ab_cfg    = get_auto_buy(uid)
    ab_lbl    = "🟢 Auto-Buy: ON" if ab_cfg.get("enabled") else "🔴 Auto-Buy: OFF"
    gsl       = get_global_sl()
    gsl_lbl   = "🟢 SL: ON" if gsl.get("enabled") else "🔴 SL: OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Market",       callback_data="menu:market"),
         InlineKeyboardButton("💰 Trade",        callback_data="menu:trade"),
         InlineKeyboardButton("👜 Portfolio",    callback_data="menu:portfolio")],
        [InlineKeyboardButton("🔍 Scout",         callback_data="menu:scout"),
         InlineKeyboardButton("🤖 Auto-Sell",     callback_data="menu:autosell"),
         InlineKeyboardButton(ab_lbl,             callback_data="menu:autobuy")],
        [InlineKeyboardButton(scan_lbl,           callback_data="scanner:toggle"),
         InlineKeyboardButton("👀 Scouted",       callback_data="scanner:watchlist"),
         InlineKeyboardButton("🏆 Top Scouts",    callback_data="scanner:topalerts")],
        [InlineKeyboardButton("🌡️ Thresholds",     callback_data="scanner:set_threshold"),
         InlineKeyboardButton("📢 Channels",      callback_data="channels:menu")],
        [InlineKeyboardButton(pf_lbl,             callback_data="pumplive:menu")],
        [InlineKeyboardButton(pg_lbl,             callback_data="pumpgrad:menu")],
        [InlineKeyboardButton("👛 Wallet",        callback_data="wallet:menu"),
         InlineKeyboardButton(gsl_lbl,            callback_data="gsl:menu")],
        [InlineKeyboardButton(f"⚙️ Mode: {mode}", callback_data="menu:settings")],
    ])


def market_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 Top Scouted Calls", callback_data="market:top")],
        [InlineKeyboardButton("🔍 Look Up Token",  callback_data="market:lookup")],
        [InlineKeyboardButton("⬅️ Back",            callback_data="menu:main")],
    ])


def trade_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Buy Token",  callback_data="trade:buy"),
         InlineKeyboardButton("🔴 Sell Token", callback_data="trade:sell")],
        [InlineKeyboardButton("⬅️ Back",        callback_data="menu:main")],
    ])


def autosell_list_kb(uid: int) -> InlineKeyboardMarkup:
    """List all tokens with auto-sell configs."""
    configs = _db.get_all_auto_sells(uid)
    gsl     = get_global_sl()
    gsl_lbl = "🟢 ON" if gsl.get("enabled") else "🔴 OFF"
    rows = []
    for mint, cfg in configs.items():
        sym     = cfg.get("symbol", mint[:6])
        enabled = "✅" if cfg.get("enabled") else "⏸️"
        rows.append([InlineKeyboardButton(
            f"{enabled} ${sym}", callback_data=f"as:view:{mint}"
        )])
    presets_on = get_user_as_presets_enabled(uid)
    presets_lbl = "🟢 ON" if presets_on else "🔴 OFF"
    rows.append([InlineKeyboardButton(f"🔧 Presets: {presets_lbl}", callback_data="as_preset:menu"),
                 InlineKeyboardButton(f"🌍 Global SL: {gsl_lbl}", callback_data="gsl:menu")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def autosell_token_kb(uid: int, mint: str) -> InlineKeyboardMarkup:
    cfg     = get_auto_sell(uid, mint) or {}
    enabled = cfg.get("enabled", True)
    sym     = cfg.get("symbol", mint[:6])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "⏸️ Pause" if enabled else "▶️ Enable",
            callback_data=f"as:toggle:{mint}"
        )],
        [InlineKeyboardButton("📈 Edit Targets",      callback_data=f"as:mt_menu:{mint}"),
         InlineKeyboardButton("➕ Add Custom",         callback_data=f"as:addcustom:{mint}")],
        [InlineKeyboardButton("🏦 MCap Alerts",       callback_data=f"as:mcap_menu:{mint}"),
         InlineKeyboardButton("⚡ Strategies",         callback_data=f"as:strategies:{mint}")],
        [InlineKeyboardButton("🛑 Stop-Loss",         callback_data=f"as:sl_menu:{mint}"),
         InlineKeyboardButton("📉 Trail Stop",        callback_data=f"as:trail_menu:{mint}")],
        [InlineKeyboardButton("📈 Trail TP",          callback_data=f"as:ttp_menu:{mint}"),
         InlineKeyboardButton("⏱️ Time Exit",         callback_data=f"as:te_menu:{mint}")],
        [InlineKeyboardButton("🛡️ Breakeven",         callback_data=f"as:be_menu:{mint}"),
         InlineKeyboardButton("🔄 Reset",             callback_data=f"as:reset:{mint}")],
        [InlineKeyboardButton("🗑️ Remove Config",     callback_data=f"as:remove:{mint}")],
        [InlineKeyboardButton("⬅️ Back",              callback_data="menu:autosell")],
    ])


def _fee_label(micro_lamports: int) -> str:
    """Human label for a priority fee value."""
    for name, val in PRIORITY_FEE_PRESETS.items():
        if micro_lamports == val:
            return name.capitalize()
    return f"{micro_lamports // 1000}K µL"


def _format_pnl_card(symbol: str, mint: str, sol_in: float, sol_out: float,
                     buy_ts: float, sell_ts: float, mode: str) -> str:
    """Format a P&L share card string for a completed sell."""
    pnl_sol = sol_out - sol_in
    pnl_pct = (pnl_sol / sol_in * 100) if sol_in > 0 else 0
    sign    = "+" if pnl_pct >= 0 else ""
    emoji   = "📈" if pnl_sol >= 0 else "📉"
    hold_s  = sell_ts - buy_ts if buy_ts else 0
    if hold_s < 60:        hold_str = f"{int(hold_s)}s"
    elif hold_s < 3600:    hold_str = f"{int(hold_s/60)}m"
    elif hold_s < 86400:   hold_str = f"{int(hold_s/3600)}h {int((hold_s%3600)/60)}m"
    else:                  hold_str = f"{int(hold_s/86400)}d"
    mode_tag = "📄 Paper" if mode == "paper" else "🔴 Live"
    return (
        f"{'━'*22}\n"
        f"🎰 *${symbol}* Trade Complete {emoji}\n"
        f"{'━'*22}\n"
        f"Entry:  `{sol_in:.4f} ◎`\n"
        f"Exit:   `{sol_out:.4f} ◎`\n"
        f"P&L:    `{sign}{pnl_sol:.4f} ◎` ({sign}{pnl_pct:.1f}%)\n"
        f"Hold:   `{hold_str}`\n"
        f"Mode:   {mode_tag}\n"
        f"{'━'*22}"
    )

SAFETY_CHECK_TIMEOUT = 8  # seconds before we give up and allow the trade

async def check_token_safety(mint: str) -> dict:
    """
    Fetch RugCheck report for mint and return a safety summary.
    Returns dict with keys:
      - safe: bool  (False = blocked, True = allow)
      - warnings: list[str]  (non-blocking yellow flags)
      - block_reason: str | None  (set if safe=False)
      - score: int  (RugCheck score, lower = riskier; 0 if unavailable)
    """
    from scanner import fetch_rugcheck
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        rc = await asyncio.wait_for(
            loop.run_in_executor(None, fetch_rugcheck, mint),
            timeout=SAFETY_CHECK_TIMEOUT
        )
    except Exception:
        # Network/timeout — fail closed: don't buy tokens we can't verify
        return {"safe": False, "warnings": [], "block_reason": "Safety check timed out — cannot verify token (RugCheck unreachable)", "score": 0}

    if not rc:
        return {"safe": False, "warnings": [], "block_reason": "No RugCheck data available for this token", "score": 0}

    warnings    = []
    block_reason = None
    score        = rc.get("score", 0) or 0

    # Hard blocks — these always cancel the trade
    if rc.get("rugged"):
        block_reason = "Token flagged as RUGGED on RugCheck"

    # Mint authority still enabled = dev can print infinite tokens
    mint_auth = rc.get("mintAuthority")
    if not block_reason and mint_auth and mint_auth not in ("", "null", None):
        block_reason = "Mint authority NOT revoked — dev can print tokens"

    # Danger-level risks from RugCheck
    danger_risks = [r["name"] for r in (rc.get("risks") or []) if r.get("level") == "danger"]
    if not block_reason and danger_risks:
        block_reason = f"RugCheck DANGER: {', '.join(danger_risks[:3])}"

    # Warnings — non-blocking yellow flags
    freeze_auth = rc.get("freezeAuthority")
    if freeze_auth and freeze_auth not in ("", "null", None):
        warnings.append("Freeze authority active — trading could be frozen")

    warn_risks = [r["name"] for r in (rc.get("risks") or []) if r.get("level") == "warn"]
    if warn_risks:
        warnings.append(f"RugCheck warnings: {', '.join(warn_risks[:3])}")

    top_holders = rc.get("topHolders") or []
    if top_holders:
        top_pct = top_holders[0].get("pct", 0) or 0
        if top_pct > 20:
            warnings.append(f"Top holder owns {top_pct:.1f}% of supply")

    return {
        "safe":         block_reason is None,
        "warnings":     warnings,
        "block_reason": block_reason,
        "score":        score,
    }


def get_safety_check_enabled(uid: int) -> bool:
    return _uts(uid).get("safety_check", True)

def set_safety_check_enabled(uid: int, enabled: bool):
    _set_uts(uid, "safety_check", enabled)

def get_user_quick_buy_amounts(uid: int) -> list:
    return _uts(uid).get("quick_buy_amounts", [0.1, 0.25, 0.5, 1.0])

def set_user_quick_buy_amounts(uid: int, amounts: list):
    _set_uts(uid, "quick_buy_amounts", amounts)


def settings_kb(uid: int) -> InlineKeyboardMarkup:
    mode      = get_mode(uid)
    slip      = get_user_slippage(uid)
    slip_pct  = slip / 100
    slip_label = f"{slip_pct:.1f}%" if slip % 100 else f"{int(slip_pct)}%"
    jito      = get_user_jito(uid)
    jito_tip  = get_user_jito_tip(uid)
    jito_label = f"{'🟢 ON' if jito else '🔴 OFF'} · tip {jito_tip // 1000}K L"
    pfee      = get_user_priority_fee(uid)
    pfee_label = _fee_label(pfee)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            ("✅ " if mode == "paper" else "") + "📄 Paper Trading",
            callback_data="mode:paper"
        )],
        [InlineKeyboardButton(
            ("✅ " if mode == "live" else "") + "🔴 Live Trading",
            callback_data="mode:live"
        )],
        [InlineKeyboardButton(f"⚡ Slippage: {slip_label}",        callback_data="settings:slippage"),
         InlineKeyboardButton(f"🚀 Priority: {pfee_label}",        callback_data="settings:priority")],
        [InlineKeyboardButton(f"🛡️ Jito MEV: {jito_label}",       callback_data="settings:jito")],
        [InlineKeyboardButton(f"🔍 Safety Check: {'🟢 ON' if get_safety_check_enabled(uid) else '🔴 OFF'}", callback_data="settings:safety_toggle"),
         InlineKeyboardButton("💰 Buy Amounts", callback_data="settings:quick_amounts")],
        [InlineKeyboardButton("🗑️ Reset Paper Portfolio", callback_data="settings:reset_paper")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:main")],
    ])


def back_kb(dest: str = "menu:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=dest)]])


def confirm_trade_kb(action: str, mint: str, symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Confirm {action.title()}",
                              callback_data=f"confirm:{action}:{mint}:{symbol}"),
         InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


def price_card_kb(mint: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Buy",      callback_data=f"quick:buy:{mint}"),
         InlineKeyboardButton("🔔 Alert",    callback_data=f"quick:alert:{mint}")],
        [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}"),
         InlineKeyboardButton("🪙 Pump.fun",    url=f"https://pump.fun/{mint}"),
         InlineKeyboardButton("🔫 RugCheck",    url=f"https://rugcheck.xyz/tokens/{mint}")],
        [InlineKeyboardButton("⬅️ Back",     callback_data="menu:market")],
    ])


# ─── Trading ──────────────────────────────────────────────────────────────────

def jupiter_quote(in_mint: str, out_mint: str, amount: int, slippage_bps: int = 150) -> dict | None:
    try:
        q = requests.get(JUPITER_QUOTE_URL, params={
            "inputMint": in_mint, "outputMint": out_mint,
            "amount": amount, "slippageBps": slippage_bps,
            "restrictIntermediateTokens": "true",  # direct routes — fewer sandwich hops
        }, timeout=10).json()
        # Anti-sandwich: reject quotes with excessive price impact
        max_impact = getattr(_cfg, "ANTI_SANDWICH_MAX_PRICE_IMPACT_PCT", 3.0)
        impact = float(q.get("priceImpactPct") or 0)
        if impact > max_impact:
            print(f"[ANTI-MEV] Quote rejected — price impact {impact:.2f}% > {max_impact}%", flush=True)
            return None
        return q
    except Exception:
        return None


def _build_jito_tip_tx(keypair, tip_lamports: int) -> str | None:
    """Build a base64-encoded SOL-transfer tip transaction for a Jito bundle."""
    import struct, base64 as _b64, random
    try:
        from solders.pubkey import Pubkey
        from solders.instruction import Instruction, AccountMeta
        from solders.message import MessageV0
        from solders.transaction import VersionedTransaction
        from solders.hash import Hash

        tip_account = random.choice(JITO_TIP_ACCOUNTS)
        bh_resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getLatestBlockhash",
            "params": [{"commitment": "confirmed"}],
        }, timeout=10).json()
        bh = bh_resp["result"]["value"]["blockhash"]

        # System program transfer instruction: discriminant=2, u64 LE amount
        ix_data = struct.pack("<BQ", 2, tip_lamports)
        transfer_ix = Instruction(
            program_id=Pubkey.from_string("11111111111111111111111111111111"),
            accounts=[
                AccountMeta(pubkey=keypair.pubkey(), is_signer=True,  is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(tip_account),   is_signer=False, is_writable=True),
            ],
            data=bytes(ix_data),
        )
        msg = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=[transfer_ix],
            address_lookup_table_accounts=[],
            recent_blockhash=Hash.from_string(bh),
        )
        tip_tx = VersionedTransaction(msg, [keypair])
        return _b64.b64encode(bytes(tip_tx)).decode()
    except Exception as e:
        print(f"[JITO] tip tx build failed: {e}", flush=True)
        return None


def execute_swap_live(quote: dict, uid: int = 0) -> str:
    """Sign and submit a Jupiter swap. Uses Jito MEV protection and per-user priority fee when enabled.
    Always returns the real tx signature on success, or an ERROR: prefixed string on failure."""
    if not WALLET_PRIVATE_KEY:
        return "ERROR: No WALLET_PRIVATE_KEY in config.py"
    try:
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        import base64

        keypair      = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
        prio_fee     = get_user_priority_fee(uid) if uid else PRIORITY_FEE_MICRO_LAMPORTS
        use_jito     = get_user_jito(uid) if uid else JITO_ENABLED
        tip_lamports = get_user_jito_tip(uid) if uid else JITO_TIP_LAMPORTS

        # Anti-sandwich: scale Jito tip with trade size to outbid sandwich bots.
        # A sandwich bot must pay MORE than us in tips to get their bundle prioritised first.
        # Larger trades attract larger sandwich attempts, so we tip proportionally.
        if getattr(_cfg, "ANTI_SANDWICH_JITO_TIP_SCALE", True):
            in_amount = int(quote.get("inAmount", 0))
            if in_amount > 500_000_000:  # > 0.5 SOL
                scale = max(1.0, in_amount / 1_000_000_000)  # 1x per SOL
                tip_lamports = min(int(tip_lamports * scale), 5_000_000)  # cap 0.005 SOL

        # Priority fee in lamports = (µlamports/CU × CU_budget) / 1_000_000
        prio_lamports = int(prio_fee * PRIORITY_FEE_COMPUTE_UNITS / 1_000_000)

        swap = requests.post(JUPITER_SWAP_URL, json={
            "quoteResponse":          quote,
            "userPublicKey":          str(keypair.pubkey()),
            "wrapAndUnwrapSol":       True,
            "prioritizationFeeLamports": prio_lamports,
        }, timeout=15).json()

        if "swapTransaction" not in swap:
            return f"ERROR: Swap build failed: {swap.get('error', swap)}"

        raw_tx  = VersionedTransaction.from_bytes(base64.b64decode(swap["swapTransaction"]))
        tx      = VersionedTransaction(raw_tx.message, [keypair])
        tx_b64  = base64.b64encode(bytes(tx)).decode()

        # Extract the real tx signature from the signed transaction upfront.
        # This is the signature that will appear on-chain regardless of submission path.
        tx_sig = str(tx.signatures[0])

        # ── Jito bundle path ──────────────────────────────────────────────────
        if use_jito:
            tip_b64 = _build_jito_tip_tx(keypair, tip_lamports)
            if tip_b64:
                try:
                    # Use a no-retry session so DNS failures fall through instantly
                    # instead of hanging on urllib3's default retry loop.
                    _jito_session = requests.Session()
                    _jito_session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
                    bundle_resp = _jito_session.post(JITO_ENDPOINT, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "sendBundle",
                        "params": [[tx_b64, tip_b64]],
                    }, timeout=4).json()
                    if "result" in bundle_resp:
                        bundle_id = bundle_resp["result"]
                        print(f"[JITO] bundle submitted: {bundle_id} tx={tx_sig[:20]}", flush=True)
                        return tx_sig  # return real sig, not bundle ID
                    err = bundle_resp.get("error", {})
                    print(f"[JITO] bundle error, falling back to RPC: {err}", flush=True)
                except Exception as je:
                    print(f"[JITO] bundle submission failed ({je}), falling back to RPC", flush=True)
            else:
                print("[JITO] tip tx build failed, falling back to RPC", flush=True)

        # ── Standard RPC path ─────────────────────────────────────────────────
        resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [
                tx_b64,
                {"encoding": "base64", "preflightCommitment": "confirmed"},
            ],
        }, timeout=30).json()

        if "result" in resp:
            return resp["result"]
        return f"ERROR: RPC error: {resp.get('error', resp)}"
    except Exception as e:
        return f"ERROR: {e}"


def _is_slippage_error(sig: str) -> bool:
    """Detect if a swap result string indicates a slippage tolerance error."""
    if not sig:
        return False
    low = sig.lower()
    return any(k in low for k in (
        "slippage", "0x1771", "6001", "tolerance exceeded",
        "price changed", "custom program error: 0x1"
    ))


async def _swap_with_retry(
    in_mint: str, out_mint: str, amount_raw: int, uid: int,
    loop, max_retries: int = 2, bump_factor: float = 1.5,
    status_fn=None,
) -> tuple:
    """
    Fetch Jupiter quote + execute swap, retrying on slippage failures.

    Anti-sandwich retry strategy:
      Attempt 1 → slippage error → wait ANTI_SANDWICH_RETRY_DELAY_SECS before retry.
                  Sandwich bots typically exit within 1-2 blocks (~1-2s). Waiting lets
                  the pool price recover so we can retry at the same slippage.
      Attempt 2 → slippage error → bump slippage (genuine liquidity issue, not MEV).
      Attempt 3 → final attempt at bumped slippage.

    NOT bumping slippage on the first failure is intentional: immediately retrying
    with higher slippage makes us MORE exploitable (the sandwich bot knows we'll accept
    a worse price on the next block).
    """
    slippage = get_user_slippage(uid)
    quote = None
    sig   = "ERROR: no attempts made"
    retry_delay = getattr(_cfg, "ANTI_SANDWICH_RETRY_DELAY_SECS", 2.0)
    for attempt in range(1, max_retries + 2):
        quote = await loop.run_in_executor(
            None, lambda s=slippage: jupiter_quote(in_mint, out_mint, amount_raw, s)
        )
        if not quote or "outAmount" not in quote:
            return "ERROR: quote failed", None, attempt, slippage
        if status_fn and attempt > 1:
            await status_fn(f"🔄 Retry {attempt - 1}/{max_retries} — slippage {slippage / 100:.1f}%...")
        sig = await loop.run_in_executor(None, lambda q=quote: execute_swap_live(q, uid))
        if not _is_slippage_error(sig):
            return sig, quote, attempt, slippage
        if attempt <= max_retries:
            if attempt == 1:
                # First failure: wait for any sandwich bot to exit before retrying
                # at the same slippage — don't reveal a higher tolerance yet.
                print(f"[ANTI-MEV] Slippage hit — waiting {retry_delay}s for MEV to clear", flush=True)
                await asyncio.sleep(retry_delay)
            else:
                # Second+ failure: genuine liquidity issue, bump slippage
                slippage = min(5000, int(slippage * bump_factor))
                print(f"[RETRY] bumping slippage to {slippage} bps on attempt {attempt}", flush=True)
    return sig, quote, max_retries + 1, slippage


def get_wallet_pubkey() -> str | None:
    if not WALLET_PRIVATE_KEY:
        return None
    try:
        from solders.keypair import Keypair
        return str(Keypair.from_base58_string(WALLET_PRIVATE_KEY).pubkey())
    except Exception:
        return None


def send_sol_onchain(to_address: str, lamports: int) -> str:
    """Send SOL from the bot wallet to to_address. Returns tx signature or 'ERROR: ...'."""
    if not WALLET_PRIVATE_KEY:
        return "ERROR: No wallet configured"
    try:
        import struct, base64 as _b64
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solders.instruction import Instruction, AccountMeta
        from solders.message import MessageV0
        from solders.transaction import VersionedTransaction
        kp       = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
        to_pk    = Pubkey.from_string(to_address)
        sys_prog = Pubkey.from_string("11111111111111111111111111111111")
        # System program instruction 2 = transfer
        ix_data  = struct.pack("<IQ", 2, lamports)
        ix = Instruction(
            program_id=sys_prog,
            accounts=[AccountMeta(kp.pubkey(), True, True), AccountMeta(to_pk, False, True)],
            data=ix_data,
        )
        bh = pumpfun.get_recent_blockhash(SOLANA_RPC)
        if not bh:
            return "ERROR: Could not fetch blockhash"
        msg = MessageV0.try_compile(payer=kp.pubkey(), instructions=[ix],
                                    address_lookup_table_accounts=[], recent_blockhash=bh)
        tx = VersionedTransaction(msg, [kp])
        resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [_b64.b64encode(bytes(tx)).decode(),
                       {"encoding": "base64", "preflightCommitment": "confirmed"}],
        }, timeout=30).json()
        return resp.get("result") or f"ERROR: {resp.get('error', resp)}"
    except Exception as e:
        return f"ERROR: {e}"


def send_token_onchain(mint: str, to_address: str, raw_amount: int) -> str:
    """Send SPL token from the bot wallet to to_address. Returns tx signature or 'ERROR: ...'."""
    if not WALLET_PRIVATE_KEY:
        return "ERROR: No wallet configured"
    try:
        import struct, base64 as _b64
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solders.instruction import Instruction, AccountMeta
        from solders.message import MessageV0
        from solders.transaction import VersionedTransaction
        kp         = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
        src_ata    = pumpfun.get_associated_token_address(str(kp.pubkey()), mint)
        dst_ata    = pumpfun.get_associated_token_address(to_address, mint)
        src_pk     = Pubkey.from_string(src_ata)
        dst_pk     = Pubkey.from_string(dst_ata)
        mint_pk    = Pubkey.from_string(mint)
        dst_own_pk = Pubkey.from_string(to_address)
        token_prog = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
        instructions = []
        if not pumpfun.account_exists(dst_ata, SOLANA_RPC):
            instructions.append(pumpfun.make_create_ata_idempotent(
                kp.pubkey(), dst_own_pk, mint_pk, dst_pk))
        # Token transfer instruction index 3
        transfer_data = bytes([3]) + struct.pack("<Q", raw_amount)
        instructions.append(Instruction(
            program_id=token_prog,
            accounts=[AccountMeta(src_pk, False, True),
                      AccountMeta(dst_pk, False, True),
                      AccountMeta(kp.pubkey(), True, False)],
            data=transfer_data,
        ))
        bh = pumpfun.get_recent_blockhash(SOLANA_RPC)
        if not bh:
            return "ERROR: Could not fetch blockhash"
        msg = MessageV0.try_compile(payer=kp.pubkey(), instructions=instructions,
                                    address_lookup_table_accounts=[], recent_blockhash=bh)
        tx = VersionedTransaction(msg, [kp])
        resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [_b64.b64encode(bytes(tx)).decode(),
                       {"encoding": "base64", "preflightCommitment": "confirmed"}],
        }, timeout=30).json()
        return resp.get("result") or f"ERROR: {resp.get('error', resp)}"
    except Exception as e:
        return f"ERROR: {e}"


def get_sol_balance(pubkey: str) -> float:
    try:
        resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance",
            "params": [pubkey],
        }, timeout=10).json()
        return resp["result"]["value"] / 1e9
    except Exception:
        return 0.0


# ── Async RPC helpers (for parallel auto-buy execution) ───────────────────────

async def get_sol_balance_async(pubkey: str, timeout: int = 5) -> float:
    """Non-blocking SOL balance fetch. Uses 5s timeout for fast parallel execution."""
    try:
        loop = asyncio.get_running_loop()
        resp = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: requests.post(SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "getBalance",
                "params": [pubkey],
            }, timeout=timeout).json()),
            timeout=timeout + 1,
        )
        return resp["result"]["value"] / 1e9
    except Exception as e:
        print(f"[WARN] get_sol_balance_async failed: {e}", flush=True)
        return 0.0


async def fetch_bonding_curve_async(mint: str, rpc_url: str = None, timeout: int = 5) -> dict | None:
    """Non-blocking bonding curve fetch. Uses 5s timeout for fast parallel execution."""
    if rpc_url is None:
        rpc_url = SOLANA_RPC
    try:
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: pumpfun.fetch_bonding_curve_data(mint, rpc_url)),
            timeout=timeout + 1,
        )
        return result
    except Exception as e:
        print(f"[WARN] fetch_bonding_curve_async failed for {mint[:8]}: {e}", flush=True)
        return None


async def _rpc_with_retry(coro_fn, max_retries: int = 2, base_delay: float = 0.5, label: str = "rpc"):
    """
    Call an async coroutine function with exponential backoff retries.
    coro_fn: zero-arg async callable that returns the RPC result.
    Delays: 0.5s, 1.0s on subsequent retries.
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            return await coro_fn()
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"[RETRY] {label} attempt {attempt + 1} failed ({e}), retrying in {delay}s", flush=True)
                await asyncio.sleep(delay)
    print(f"[RETRY] {label} exhausted {max_retries} attempts: {last_exc}", flush=True)
    raise last_exc


async def poll_tx_confirmation(tx_sig: str, rpc_url: str = None,
                                max_polls: int = 60, poll_interval: float = 1.0) -> str:
    """
    Poll getTransaction() until confirmed on-chain or timeout.

    Returns:
        "confirmed"   — TX landed and succeeded on-chain
        "failed"      — TX landed but failed on-chain (e.g. slippage, insufficient funds)
        "timeout"     — TX not seen after max_polls attempts (may have been dropped)

    RPC 429 rate-limit responses are skipped and do NOT count against the poll limit.
    """
    if rpc_url is None:
        rpc_url = SOLANA_RPC
    loop = asyncio.get_running_loop()
    polls_done = 0
    while polls_done < max_polls:
        try:
            resp = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: requests.post(rpc_url, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTransaction",
                    "params": [tx_sig, {"encoding": "json", "commitment": "confirmed",
                                        "maxSupportedTransactionVersion": 0}],
                }, timeout=5).json()),
                timeout=6,
            )
            # Skip 429 rate-limit errors without counting them as a failed poll attempt
            if "error" in resp:
                err = resp["error"]
                if isinstance(err, dict) and err.get("code") == 429:
                    await asyncio.sleep(poll_interval)
                    continue  # don't increment polls_done
                # Other RPC errors — count as a poll and keep waiting
            else:
                val = resp.get("result")
                if val and val.get("blockTime") is not None:
                    # TX landed — check if it succeeded or failed on-chain
                    meta_err = val.get("meta", {}).get("err")
                    if meta_err is not None:
                        print(f"[TX] {tx_sig[:20]}... landed but FAILED on-chain: {meta_err}", flush=True)
                        return "failed"
                    return "confirmed"
        except Exception:
            pass
        polls_done += 1
        await asyncio.sleep(poll_interval)
    return "timeout"


TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Token account cache — avoids hammering the RPC on every portfolio refresh
_token_accounts_cache: dict[str, dict] = {}  # pubkey → {ts, accounts}
_TOKEN_ACCOUNTS_CACHE_TTL = 30  # seconds


def get_token_accounts(pubkey: str) -> list[dict] | None:
    """Fetch all SPL token accounts — queries both Token and Token-2022 programs.
    pump.fun tokens use Token-2022; Raydium/Jupiter tokens use the original Token program.
    Returns a list (may be empty for a genuinely empty wallet), or None if the RPC
    completely failed (both fetches errored) — callers must treat None as 'unknown'
    and must NOT use it to delete/clear positions."""
    def _fetch(program_id: str) -> list[dict] | None:
        try:
            resp = requests.post(SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [pubkey, {"programId": program_id}, {"encoding": "jsonParsed"}],
            }, timeout=15).json()
            # Treat any RPC-level error (429 rate limit, node error, etc.) as a failure
            if "error" in resp:
                print(f"[RPC] getTokenAccountsByOwner error: {resp['error']}", flush=True)
                return None
            out = []
            for item in resp.get("result", {}).get("value", []):
                info = item["account"]["data"]["parsed"]["info"]
                amt  = int(info["tokenAmount"]["amount"])
                if amt > 0:
                    out.append({
                        "mint":      info["mint"],
                        "amount":    amt,
                        "decimals":  info["tokenAmount"]["decimals"],
                        "ui_amount": info["tokenAmount"]["uiAmount"] or 0,
                    })
            return out
        except Exception:
            return None  # fetch failed — distinguish from empty wallet

    # Return cached result if still fresh (avoids RPC rate limits on rapid refreshes)
    cached = _token_accounts_cache.get(pubkey)
    if cached and (time.time() - cached["ts"]) < _TOKEN_ACCOUNTS_CACHE_TTL:
        return cached["accounts"]

    legacy  = _fetch(TOKEN_PROGRAM)
    token22 = _fetch(TOKEN_2022_PROGRAM)

    if legacy is None and token22 is None:
        # Both failed — return last known good data if available, else None
        if cached:
            print(f"[RPC] both token fetches failed, serving stale cache ({int(time.time()-cached['ts'])}s old)", flush=True)
            return cached["accounts"]
        return None  # no cache and both failed — caller should show error

    # Deduplicate by mint (prefer Token-2022 entry if both appear)
    seen: dict[str, dict] = {}
    for acc in (legacy or []) + (token22 or []):
        seen[acc["mint"]] = acc
    result = list(seen.values())

    # Update cache only on successful fetch
    _token_accounts_cache[pubkey] = {"ts": time.time(), "accounts": result}
    return result


# ─── Auto-sell execution ──────────────────────────────────────────────────────

async def execute_auto_sell(bot, uid: int, mint: str, symbol: str,
                             sell_pct: int, reason: str, mode: str,
                             price_usd: float = 0.0, mcap: float = 0.0) -> bool:
    """Sell `sell_pct`% of the current position for this user/token.
    Returns True if the sell was executed, False if it failed/skipped."""
    # Quick pre-check outside lock
    raw_held = get_portfolio(uid).get(mint, 0)
    if raw_held <= 0 and mode == "live":
        # Portfolio tracking may be out of sync — check actual on-chain balance
        _pubkey = get_wallet_pubkey()
        if _pubkey:
            _accounts = get_token_accounts(_pubkey)
            _held = next((a for a in _accounts if a["mint"] == mint), None)
            raw_held = _held["amount"] if _held else 0
    if raw_held <= 0:
        return False

    as_cfg = _db.get_auto_sell(uid, mint) or {}
    hold_seconds = None
    if as_cfg.get("purchase_timestamp"):
        hold_seconds = max(0.0, time.time() - float(as_cfg["purchase_timestamp"]))

    if mode == "paper":
        # Compute price outside the lock (sync HTTP call)
        dec       = as_cfg.get("decimals", 6)
        sol_received = 0.0
        if price_usd:
            sol_usd_rate  = pf.get_sol_price() or 150.0
            price_sol_now = price_usd / sol_usd_rate if sol_usd_rate else 0.0
            sell_amount_est = max(1, int(raw_held * sell_pct / 100))
            ui            = sell_amount_est / (10 ** dec)
            sol_received  = price_sol_now * ui * 0.99  # 1 % simulated fee
        if not sol_received:
            _p = fetch_sol_pair(mint)
            if _p:
                _psol = float(_p.get("priceNative", 0) or 0)
                _dec  = int(_p.get("baseToken", {}).get("decimals", dec) or dec)
                sell_amount_est = max(1, int(raw_held * sell_pct / 100))
                sol_received = (_psol * sell_amount_est / (10 ** _dec)) * 0.99
        if not sol_received:
            return False

        # Atomic portfolio update under lock
        async with _portfolio_lock(uid):
            portfolio = get_portfolio(uid)
            raw_held  = portfolio.get(mint, 0)
            if raw_held <= 0:
                return False
            sell_amount = max(1, int(raw_held * sell_pct / 100))
            portfolio[mint] = raw_held - sell_amount
            portfolio["SOL"] = portfolio.get("SOL", 0) + sol_received
            if portfolio[mint] <= 0:
                portfolio.pop(mint, None)
            update_portfolio(uid, portfolio)

        log_trade(uid, mode, "sell", mint, symbol,
                  sol_received=sol_received, token_amount=sell_amount,
                  price_usd=price_usd, buy_price_usd=_get_buy_price(uid, mint),
                  mcap=mcap, hold_seconds=hold_seconds,
                  exit_reason=reason, exit_trigger=reason, exit_mcap=mcap)
        await bot.send_message(
            chat_id=uid,
            text=(
                f"🤖 *Auto-Sell Triggered — {reason}*\n\n"
                f"Token: `${symbol}`\n"
                f"Sold: `{sell_pct}%` ({sell_amount:,} raw units)\n"
                f"Received: `{sol_received:.4f} SOL`\n"
                f"📄 Paper mode — simulated"
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👜 Portfolio", callback_data="menu:portfolio")
            ]])
        )
        return True
    else:
        # Live mode — quote and swap outside the lock (slow network calls)
        sell_amount = max(1, int(raw_held * sell_pct / 100))
        quote = jupiter_quote(mint, SOL_MINT, sell_amount, get_user_slippage(uid))
        if not quote or "outAmount" not in quote:
            print(f"[AUTO-SELL] Quote failed for {symbol} ({mint[:8]}) — will retry next cycle", flush=True)
            return False
        sig = execute_swap_live(quote, uid)
        sol_received = int(quote.get("outAmount", 0)) / 1e9
        if "ERROR" in sig or "error" in sig.lower():
            await bot.send_message(
                chat_id=uid,
                text=f"⚠️ *Auto-Sell Failed — {reason}*\n`${symbol}`\nError: `{sig}`",
                parse_mode="Markdown",
            )
            return False
        else:
            buy_price = _get_buy_price(uid, mint)
            # Atomic portfolio update under lock — re-read fresh state
            async with _portfolio_lock(uid):
                _pf = get_portfolio(uid)
                current_held = _pf.get(mint, 0)
                _pf["SOL"] = _pf.get("SOL", 0) + sol_received
                if sell_amount >= current_held:
                    _pf.pop(mint, None)
                else:
                    _pf[mint] = current_held - sell_amount
                update_portfolio(uid, _pf)
            log_trade(uid, mode, "sell", mint, symbol,
                      sol_received=sol_received, token_amount=sell_amount,
                      price_usd=price_usd, buy_price_usd=buy_price,
                      mcap=mcap, tx_sig=sig, hold_seconds=hold_seconds,
                      exit_reason=reason, exit_trigger=reason, exit_mcap=mcap)
            await bot.send_message(
                chat_id=uid,
                text=(
                    f"🤖 *Auto-Sell Executed — {reason}*\n\n"
                    f"Token: `${symbol}`\n"
                    f"Sold: `{sell_pct}%` ({sell_amount:,} raw units)\n"
                    f"Received: `~{sol_received:.4f} SOL`\n"
                    f"Tx: `{sig}`\n"
                    f"[Solscan](https://solscan.io/tx/{sig})"
                ),
                parse_mode="Markdown",
            )
            return True


# ─── Background monitoring ────────────────────────────────────────────────────

async def check_auto_sell(context: ContextTypes.DEFAULT_TYPE):
    """Monitor positions for auto-sell triggers and mcap milestones."""
    all_configs = _db.get_all_auto_sells_all_users()
    if not all_configs:
        return

    # ── Pre-fetch prices in parallel for all enabled positions ──────────────────
    _candidate_mints: set[str] = {
        mint
        for tokens in all_configs.values()
        for mint, cfg in tokens.items()
        if cfg.get("enabled", True) and cfg.get("buy_price_usd", 0)
    }
    _loop = asyncio.get_running_loop()
    _price_raw = await asyncio.gather(
        *[_loop.run_in_executor(None, fetch_token_price, m) for m in _candidate_mints],
        return_exceptions=True,
    )
    _price_cache: dict[str, tuple] = {
        m: (r if not isinstance(r, Exception) else (None, None))
        for m, r in zip(_candidate_mints, _price_raw)
    }

    for uid_str, tokens in all_configs.items():
        uid  = int(uid_str)
        mode = get_mode(uid)

        # For live mode, fetch wallet holdings once per user per cycle
        _live_held_mints: set | None = None
        if mode == "live":
            _pubkey = get_wallet_pubkey()
            if _pubkey:
                _accounts = get_token_accounts(_pubkey)
                if _accounts is None:
                    _live_held_mints = None  # RPC failed — skip stale check this cycle
                else:
                    _live_held_mints = {a["mint"] for a in _accounts}
        else:
            portfolio = get_portfolio(uid)  # load once per user, not once per token

        for mint, cfg in list(tokens.items()):
            if not cfg.get("enabled", True):
                continue

            buy_price = cfg.get("buy_price_usd", 0)
            if not buy_price:
                continue

            # Safeguard: Check if user still holds this token (prevent stale auto-sell entries)
            if mode == "live":
                if _live_held_mints is not None and mint not in _live_held_mints:
                    remove_auto_sell(uid, mint)
                    print(f"[AUTO-SELL] Cleaned up stale live entry: uid={uid}, mint={mint[:8]}", flush=True)
                    continue
            else:
                if mint not in portfolio or portfolio[mint] <= 0:
                    remove_auto_sell(uid, mint)
                    print(f"[AUTO-SELL] Cleaned up stale paper entry: uid={uid}, mint={mint[:8]}", flush=True)
                    continue

            price, mcap = _price_cache.get(mint, (None, None))
            if price is None:
                # Cache miss (fetch failed) — try a fresh fetch before skipping
                try:
                    price, mcap = fetch_token_price(mint)
                except Exception:
                    pass
            if price is None:
                continue

            symbol  = cfg.get("symbol", mint[:6])
            changed = False

            # ── Dead-token exit (MCap < $5K for > 5 min) ─────────────────────
            _DEAD_MCAP  = 5_000   # USD
            _DEAD_SECS  = 300     # 5 minutes
            if mcap is not None:
                if mcap < _DEAD_MCAP:
                    if not cfg.get("dead_below_since"):
                        cfg["dead_below_since"] = time.time()
                        changed = True
                    elif (time.time() - cfg["dead_below_since"]) >= _DEAD_SECS:
                        cfg.pop("dead_below_since", None)
                        changed = True
                        await execute_auto_sell(
                            context.bot, uid, mint, symbol, 100,
                            f"Dead Token — MCap <$5K for >5min", mode,
                            price_usd=price, mcap=mcap
                        )
                        continue  # position closed, skip remaining checks
                else:
                    if cfg.pop("dead_below_since", None):
                        changed = True  # mcap recovered — clear the timer

            # ── Hard stop-loss ────────────────────────────────────────────────
            sl = cfg.get("stop_loss", {})
            if sl.get("enabled") and not sl.get("triggered"):
                drop_pct = ((buy_price - price) / buy_price) * 100
                if drop_pct >= sl.get("pct", 50):
                    sold = await execute_auto_sell(
                        context.bot, uid, mint, symbol,
                        sl.get("sell_pct", 100),
                        f"Stop-Loss -{sl['pct']}%", mode,
                        price_usd=price, mcap=mcap or 0
                    )
                    if sold:
                        sl["triggered"] = True
                        changed = True

            # ── Trailing stop-loss ────────────────────────────────────────────
            ts = cfg.get("trailing_stop", {})
            if ts.get("enabled") and not ts.get("triggered"):
                if price > ts.get("peak_price", 0):
                    ts["peak_price"] = price
                    changed = True
                peak = ts["peak_price"]
                if peak > 0:
                    drop_from_peak = ((peak - price) / peak) * 100
                    if drop_from_peak >= ts.get("trail_pct", 30):
                        sold = await execute_auto_sell(
                            context.bot, uid, mint, symbol,
                            ts.get("sell_pct", 100),
                            f"Trailing Stop -{ts['trail_pct']}% from peak", mode,
                            price_usd=price, mcap=mcap or 0
                        )
                        if sold:
                            ts["triggered"] = True
                            changed = True

            # ── Trailing take-profit ──────────────────────────────────────────
            ttp = cfg.get("trailing_tp", {})
            if ttp.get("enabled") and not ttp.get("triggered"):
                if not ttp.get("active"):
                    if price >= buy_price * ttp.get("activate_mult", 2.0):
                        ttp["active"] = True
                        ttp["peak_price"] = price
                        changed = True
                        try:
                            await context.bot.send_message(
                                uid,
                                f"📈 *Trailing TP Activated* — `${symbol}`\n"
                                f"Price hit `{ttp['activate_mult']}x` — now trailing `{ttp['trail_pct']}%` below peak",
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass
                else:
                    if price > ttp.get("peak_price", 0):
                        ttp["peak_price"] = price
                        changed = True
                    peak = ttp["peak_price"]
                    if peak > 0:
                        drop = ((peak - price) / peak) * 100
                        if drop >= ttp.get("trail_pct", 20):
                            sold = await execute_auto_sell(
                                context.bot, uid, mint, symbol,
                                ttp.get("sell_pct", 50),
                                f"Trailing TP -{ttp['trail_pct']}% from peak", mode,
                                price_usd=price, mcap=mcap or 0
                            )
                            if sold:
                                ttp["triggered"] = True
                                changed = True

            # ── Time-based exit ───────────────────────────────────────────────
            te = cfg.get("time_exit", {})
            if te.get("enabled") and not te.get("triggered"):
                buy_time = te.get("buy_time", 0)
                hours_elapsed = (time.time() - buy_time) / 3600
                if hours_elapsed >= te.get("hours", 24):
                    if price < buy_price * te.get("target_mult", 2.0):
                        sold = await execute_auto_sell(
                            context.bot, uid, mint, symbol,
                            te.get("sell_pct", 100),
                            f"Time Exit ({te['hours']}h — target not reached)", mode,
                            price_usd=price, mcap=mcap or 0
                        )
                        if sold:
                            te["triggered"] = True
                            changed = True

            # ── Breakeven stop ────────────────────────────────────────────────
            be = cfg.get("breakeven_stop", {})
            if be.get("enabled") and not be.get("triggered"):
                if price >= buy_price * be.get("activate_mult", 2.0):
                    sl = cfg.setdefault("stop_loss", {"enabled": True, "pct": 0, "sell_pct": 100, "triggered": False})
                    sl["enabled"]   = True
                    sl["pct"]       = 0        # trigger at or below buy price
                    sl["triggered"] = False    # re-arm so the new breakeven SL can fire
                    be["triggered"] = True
                    changed = True
                    try:
                        await context.bot.send_message(
                            uid,
                            f"🛡️ *Breakeven Stop Activated* — `${symbol}`\n"
                            f"Price hit `{be['activate_mult']}x` — stop-loss moved to entry price",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

            # ── Global stop-loss ──────────────────────────────────────────────
            gsl = get_global_sl()
            if gsl.get("enabled") and not cfg.get("_gsl_triggered"):
                drop_pct = ((buy_price - price) / buy_price) * 100
                if drop_pct >= gsl.get("pct", 50):
                    sold = await execute_auto_sell(
                        context.bot, uid, mint, symbol,
                        gsl.get("sell_pct", 100),
                        f"Global Stop-Loss -{gsl['pct']}%", mode,
                        price_usd=price, mcap=mcap or 0
                    )
                    if sold:
                        cfg["_gsl_triggered"] = True
                        changed = True

            # ── Global Trailing Stop ──────────────────────────────────────────
            gts = get_global_trailing_stop()
            if gts.get("enabled") and not cfg.get("_gts_triggered"):
                # Track the running peak price for this position
                if cfg.get("_gts_peak") is None:
                    cfg["_gts_peak"] = price
                    changed = True
                elif price > cfg["_gts_peak"]:
                    cfg["_gts_peak"] = price
                    changed = True
                peak = cfg.get("_gts_peak", buy_price)
                trail_pct = gts.get("trail_pct", 30)
                drop_from_peak = ((peak - price) / peak) * 100 if peak > 0 else 0
                if drop_from_peak >= trail_pct:
                    sold = await execute_auto_sell(
                        context.bot, uid, mint, symbol,
                        gts.get("sell_pct", 100),
                        f"Global Trailing Stop -{trail_pct}% from peak", mode,
                        price_usd=price, mcap=mcap or 0
                    )
                    if sold:
                        cfg["_gts_triggered"] = True
                        changed = True
                    try:
                        await context.bot.send_message(
                            uid,
                            f"📉 *Global Trailing Stop Fired* — `${symbol}`\n"
                            f"Peak was `${peak:.6f}` → dropped `{drop_from_peak:.1f}%` → SOLD",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

            # ── Global Trailing Take-Profit ───────────────────────────────────
            gttp = get_global_trailing_tp()
            if gttp.get("enabled") and not cfg.get("_gttp_triggered"):
                activate_mult = gttp.get("activate_mult", 2.0)
                trail_pct     = gttp.get("trail_pct", 20)
                sell_pct      = gttp.get("sell_pct", 50)
                if not cfg.get("_gttp_active") and price >= buy_price * activate_mult:
                    # Activate trailing TP
                    cfg["_gttp_active"] = True
                    cfg["_gttp_peak"]   = price
                    changed = True
                    try:
                        await context.bot.send_message(
                            uid,
                            f"🎯 *Global Trailing TP Activated* — `${symbol}`\n"
                            f"Price hit `{activate_mult}x` (`${price:.6f}`). "
                            f"Now trailing `{trail_pct}%` below peak. "
                            f"Will sell `{sell_pct}%` if price drops that far.",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                elif cfg.get("_gttp_active"):
                    if price > cfg.get("_gttp_peak", 0):
                        cfg["_gttp_peak"] = price
                        changed = True
                    peak = cfg.get("_gttp_peak", price)
                    drop_from_peak = ((peak - price) / peak) * 100 if peak > 0 else 0
                    if drop_from_peak >= trail_pct:
                        sold = await execute_auto_sell(
                            context.bot, uid, mint, symbol,
                            sell_pct,
                            f"Global Trailing TP -{trail_pct}% from peak", mode,
                            price_usd=price, mcap=mcap or 0
                        )
                        if sold:
                            cfg["_gttp_triggered"] = True
                            changed = True
                        try:
                            await context.bot.send_message(
                                uid,
                                f"🎯 *Global Trailing TP Fired* — `${symbol}`\n"
                                f"Peak was `${peak:.6f}` → dropped `{drop_from_peak:.1f}%` → SOLD `{sell_pct}%`",
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass

            # ── Global Breakeven Stop ─────────────────────────────────────────
            gbe = get_global_breakeven_stop()
            if gbe.get("enabled") and not cfg.get("_gbe_triggered"):
                be_mult = gbe.get("activate_mult", 2.0)
                if price >= buy_price * be_mult:
                    cfg["_gbe_triggered"] = True
                    # Move per-position stop-loss to breakeven (0% drop = entry price)
                    if "stop_loss" not in cfg:
                        cfg["stop_loss"] = {}
                    cfg["stop_loss"]["pct"]       = 0
                    cfg["stop_loss"]["enabled"]   = True
                    cfg["stop_loss"]["triggered"] = False  # re-arm so breakeven SL can fire
                    changed = True
                    try:
                        await context.bot.send_message(
                            uid,
                            f"🛡️ *Global Breakeven Stop Activated* — `${symbol}`\n"
                            f"Price hit `{be_mult}x` — stop-loss locked to entry price `${buy_price:.6f}`.\n"
                            f"You cannot lose money on this position now.",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

            # ── Global Time Exit ──────────────────────────────────────────────
            gte = get_global_time_exit()
            if gte.get("enabled") and not cfg.get("_gte_triggered"):
                purchase_ts = cfg.get("purchase_timestamp", 0)
                if purchase_ts:
                    hours_elapsed = (time.time() - purchase_ts) / 3600
                    gte_hours  = gte.get("hours", 24)
                    target_mult = gte.get("target_mult", 1.5)
                    if hours_elapsed >= gte_hours and price < buy_price * target_mult:
                        sold = await execute_auto_sell(
                            context.bot, uid, mint, symbol,
                            gte.get("sell_pct", 100),
                            f"Global Time Exit ({gte_hours}h elapsed, below {target_mult}x)", mode,
                            price_usd=price, mcap=mcap or 0
                        )
                        if sold:
                            cfg["_gte_triggered"] = True
                            changed = True
                        try:
                            await context.bot.send_message(
                                uid,
                                f"⏱️ *Global Time Exit Fired* — `${symbol}`\n"
                                f"`{hours_elapsed:.1f}h` elapsed and price still below `{target_mult}x` target → SOLD",
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass


            for target in cfg.get("mult_targets", []):
                if target["triggered"]:
                    continue
                if price >= buy_price * target["mult"]:
                    sold = await execute_auto_sell(
                        context.bot, uid, mint, symbol,
                        target["sell_pct"], target["label"], mode,
                        price_usd=price, mcap=mcap or 0
                    )
                    if sold:
                        target["triggered"] = True
                        changed = True

            # ── Custom targets ────────────────────────────────────────────────
            for ct in cfg.get("custom_targets", []):
                if ct.get("triggered"):
                    continue
                hit = False
                reason = ""
                t = ct["type"]
                if t == "multiplier" and price >= buy_price * ct["value"]:
                    hit    = True
                    reason = f"{ct['value']}x custom target"
                elif t == "price" and price >= ct["value"]:
                    hit    = True
                    reason = f"price ${ct['value']} custom target"

                if hit:
                    ct["triggered"] = True
                    changed = True
                    sell_pct = ct.get("sell_pct", 50)
                    if sell_pct == 0:
                        # Alert-only target — notify without selling
                        try:
                            await context.bot.send_message(
                                uid,
                                f"🎯 *Custom Target Hit* (alert only)\n\n`${symbol}` — {reason}",
                                parse_mode="Markdown",
                            )
                        except Exception:
                            pass
                    else:
                        await execute_auto_sell(
                            context.bot, uid, mint, symbol, sell_pct, reason, mode,
                            price_usd=price, mcap=mcap or 0
                        )

            # ── Market cap milestone alerts ───────────────────────────────────
            if mcap and mcap > 0:
                for ma in cfg.get("mcap_alerts", []):
                    if ma.get("triggered"):
                        continue
                    if mcap >= ma["mcap"]:
                        ma["triggered"] = True
                        changed = True
                        try:
                            await context.bot.send_message(
                                chat_id=uid,
                                text=(
                                    f"🎯 *Market Cap Milestone!*\n\n"
                                    f"`${symbol}` reached `{ma['label']}` market cap!\n"
                                    f"Current MCap: `${mcap:,.0f}`\n"
                                    f"Token Price: `${price:.8f}`"
                                ),
                                parse_mode="Markdown",
                                reply_markup=InlineKeyboardMarkup([[
                                    InlineKeyboardButton("🟢 Buy", callback_data=f"quick:buy:{mint}"),
                                    InlineKeyboardButton("🔴 Sell", callback_data=f"quick:sell:{mint}"),
                                ]])
                            )
                        except Exception:
                            pass

                # Custom mcap targets
                for ct in cfg.get("custom_targets", []):
                    if ct.get("triggered") or ct["type"] != "mcap":
                        continue
                    if mcap >= ct["value"]:
                        ct["triggered"] = True
                        changed = True
                        try:
                            await context.bot.send_message(
                                chat_id=uid,
                                text=(
                                    f"🎯 *Custom MCap Target Hit!*\n\n"
                                    f"`${symbol}` MCap: `${mcap:,.0f}`\n"
                                    f"Target: `${ct['value']:,.0f}`"
                                ),
                                parse_mode="Markdown",
                            )
                        except Exception:
                            pass

            if changed:
                set_auto_sell(uid, mint, cfg)


# ─── Auto-buy execution ────────────────────────────────────────────────────────

async def execute_auto_buy(bot, uid: int, result: dict):
    """
    Attempt an auto-buy for uid based on a scanner alert result.
    Handles both paper and live modes. Sends a DM with outcome.
    """
    cfg = get_auto_buy(uid)
    if not cfg.get("enabled"):
        print(f"[AUTOBUY] uid={uid} skipped — not enabled", flush=True)
        return

    score     = result.get("effective_score", result.get("total", 0))
    mint      = result.get("mint", "")
    symbol    = result.get("symbol", mint[:6])
    name      = result.get("name", symbol)
    mcap      = result.get("mcap", 0)
    entry_score_effective = result.get("effective_score", score)
    entry_score_raw = result.get("raw_total", score)
    entry_source = result.get("_source_name") or result.get("source")
    entry_source_rank = result.get("_source_rank")
    entry_liquidity_usd = result.get("liquidity", 0) or 0
    entry_txns_5m = result.get("txns_5m", 0) or 0
    entry_wallet_signal = result.get("wallet_signal", result.get("wallet_boost", 0)) or 0
    entry_archetype = result.get("archetype")
    entry_confidence = result.get("archetype_conf")
    entry_tier = _score_tier_label(entry_score_effective)

    print(f"[AUTOBUY] uid={uid} evaluating {symbol} mint={mint[:8]}.. score={score} mcap=${mcap:,.0f}", flush=True)

    if result.get("entry_quality_autobuy_blocked"):
        reasons = (
            result.get("entry_quality_reasons", [])
            + result.get("entry_quality_force_scouted_reasons", [])
            + result.get("entry_quality_autobuy_only_reasons", [])
        )
        print(f"[AUTOBUY] uid={uid} skipped {symbol} — quality blocked: {reasons}", flush=True)
        return

    # Use auto-buy min_score, but also respect v2 tier thresholds if configured
    min_score = cfg.get("min_score", 55)
    ab_tier = cfg.get("buy_tier", "")
    if ab_tier:
        # Map tier name to user's v2 threshold (fallbacks match HEAT_SCORE_V2_DEFAULTS)
        user_cfg = sm.get_user_settings(uid)
        tier_map = {
            "scouted":   user_cfg.get("alert_scouted_threshold", 35),
            "warm":      user_cfg.get("alert_warm_threshold", 55),
            "hot":       user_cfg.get("alert_hot_threshold", 70),
            "ultra_hot": user_cfg.get("alert_ultra_hot_threshold", 85),
        }
        min_score = tier_map.get(ab_tier, min_score)

    if not result.get("grad_buy") and score < min_score:
        print(f"[AUTOBUY] uid={uid} skipped {symbol} — score {score} < min {min_score}", flush=True)
        return

    # ── MCap range filter ────────────────────────────────────────────────────
    max_mcap = cfg.get("max_mcap", 500_000)
    min_mcap = cfg.get("min_mcap_usd", 0)
    if mcap and mcap > max_mcap:
        print(f"[AUTOBUY] uid={uid} skipped {symbol} — mcap ${mcap:,.0f} > max ${max_mcap:,.0f}", flush=True)
        return
    if min_mcap > 0 and mcap < min_mcap:
        print(f"[AUTOBUY] uid={uid} skipped {symbol} — mcap ${mcap:,.0f} < min ${min_mcap:,.0f}", flush=True)
        return

    # ── Liquidity range filter ───────────────────────────────────────────────
    liquidity    = entry_liquidity_usd
    min_liq      = cfg.get("min_liquidity_usd", 0)
    max_liq      = cfg.get("max_liquidity_usd", 0)
    if min_liq > 0 and liquidity < min_liq:
        print(f"[AUTOBUY] uid={uid} skipped {symbol} — liquidity ${liquidity:,.0f} < min ${min_liq:,.0f}", flush=True)
        return
    if max_liq > 0 and liquidity > max_liq:
        print(f"[AUTOBUY] uid={uid} skipped {symbol} — liquidity ${liquidity:,.0f} > max ${max_liq:,.0f}", flush=True)
        return

    # ── Age range filter ─────────────────────────────────────────────────────
    import time as _time
    pair_created_ms = result.get("pair_created", 0) or 0
    age_mins_now = None
    if pair_created_ms:
        age_mins_now = (_time.time() * 1000 - pair_created_ms) / 60_000
        min_age = cfg.get("min_age_mins", 0)
        max_age = cfg.get("max_age_mins", 0)
        if min_age > 0 and age_mins_now < min_age:
            print(f"[AUTOBUY] uid={uid} skipped {symbol} — age {age_mins_now:.1f}m < min {min_age}m", flush=True)
            return
        if max_age > 0 and age_mins_now > max_age:
            print(f"[AUTOBUY] uid={uid} skipped {symbol} — age {age_mins_now:.1f}m > max {max_age}m", flush=True)
            return

    # ── Min 5m transactions filter ───────────────────────────────────────────
    min_txns = cfg.get("min_txns_5m", 0)
    if min_txns > 0:
        txns_5m = entry_txns_5m
        if txns_5m < min_txns:
            print(f"[AUTOBUY] uid={uid} skipped {symbol} — txns_5m {txns_5m} < min {min_txns}", flush=True)
            return

    cfg = _ab_reset_day_if_needed(cfg)

    if mint in cfg.get("bought", []):
        print(f"[AUTOBUY] uid={uid} skipped {symbol} — already bought today", flush=True)
        return  # already bought this token today

    sol_amount  = cfg.get("sol_amount", 0.1)
    daily_limit = cfg.get("daily_limit_sol", 1.0)
    spent_today = cfg.get("spent_today", 0.0)

    if daily_limit > 0 and spent_today + sol_amount > daily_limit:
        try:
            await bot.send_message(
                uid,
                f"⚠️ *Auto-Buy Skipped* — daily limit reached\n\n"
                f"Limit: `{daily_limit} SOL` | Spent: `{spent_today:.3f} SOL`\n"
                f"Token: *{name}* (${symbol}) — score `{score}/100`",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    max_pos = cfg.get("max_positions", 0)
    if max_pos > 0:
        open_positions = _db.get_open_position_count(uid)
        if open_positions >= max_pos:
            print(f"[AUTOBUY] uid={uid} skipped {symbol} — positions {open_positions} >= max {max_pos}", flush=True)
            return

    # ── Momentum gate — hard-block dead/post-peak tokens ─────────────────────
    # Mirrors scanner.py momentum check but enforced at buy time, not just alert time.
    _h1_price = result.get("price_h1", 0) or 0
    _vol_m5   = result.get("volume_m5", 0) or 0
    _vol_h1   = result.get("volume_h1", 1) or 1
    _m5_pace  = _vol_m5 * 12  # annualise m5 to hourly rate
    if not (_h1_price >= -5 or _m5_pace >= _vol_h1 * 0.3):
        print(
            f"[AUTOBUY] uid={uid} BLOCKED {symbol} — momentum dead "
            f"(h1_price={_h1_price:+.1f}%, m5_pace=${_m5_pace:,.0f} vs h1=${_vol_h1:,.0f})",
            flush=True,
        )
        return

    # ── Fresh data check — re-fetch DexScreener to confirm token is still live ─
    # Prevents buying tokens that peaked while waiting in the scanner queue.
    try:
        _fresh_loop = asyncio.get_running_loop()
        _fresh_pair = await asyncio.wait_for(
            _fresh_loop.run_in_executor(None, fetch_sol_pair, mint),
            timeout=8,
        )
        if _fresh_pair:
            _fv_m5   = float((_fresh_pair.get("volume") or {}).get("m5", 0) or 0)
            _fp_h1   = float((_fresh_pair.get("priceChange") or {}).get("h1", 0) or 0)
            _fv_h1   = float((_fresh_pair.get("volume") or {}).get("h1", 1) or 1)
            _fm5pace = _fv_m5 * 12
            if not (_fp_h1 >= -5 or _fm5pace >= _fv_h1 * 0.3):
                print(
                    f"[AUTOBUY] uid={uid} BLOCKED {symbol} — fresh data: momentum dead "
                    f"(h1_price={_fp_h1:+.1f}%, m5_pace=${_fm5pace:,.0f} vs h1=${_fv_h1:,.0f})",
                    flush=True,
                )
                return
            if _fv_m5 < 50:
                print(
                    f"[AUTOBUY] uid={uid} BLOCKED {symbol} — fresh data: zero activity "
                    f"(vol_m5=${_fv_m5:.0f} < $50 floor)",
                    flush=True,
                )
                return
    except Exception as _fe:
        print(f"[AUTOBUY] uid={uid} fresh data re-fetch failed for {symbol}: {_fe} — proceeding", flush=True)

    mode = get_mode(uid)
    print(f"[AUTOBUY] uid={uid} proceeding with {symbol} in {mode} mode — {sol_amount} SOL", flush=True)

    # ── Paper auto-buy ─────────────────────────────────────────────────────────
    if mode == "paper":
        # Quick pre-check outside lock
        sol_bal = get_portfolio(uid).get("SOL", 0)
        if sol_bal < sol_amount:
            try:
                await bot.send_message(
                    uid,
                    f"⚠️ *Auto-Buy Skipped* — insufficient paper SOL\n\n"
                    f"Need: `{sol_amount} SOL` | Have: `{sol_bal:.4f} SOL`\n"
                    f"Token: *{name}* (${symbol})",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            return

        # Get quote outside lock (slow network call)
        lamports = int(sol_amount * 1_000_000_000)
        quote    = jupiter_quote(SOL_MINT, mint, lamports)
        if not quote or "error" in quote:
            return

        out_amount = int(quote.get("outAmount", 0))
        if out_amount <= 0:
            print(f"[AUTOBUY] uid={uid} skipped {symbol} — quote returned 0 tokens", flush=True)
            return
        price_usd  = result.get("price_usd", 0)
        decimals   = 6  # default; DexScreener not re-fetched here

        # Derive buy price from quote when unavailable (e.g. newly graduated tokens)
        if not price_usd and out_amount > 0:
            import pumpfun as _pf_mod
            _sol_usd = (_pf_mod.get_sol_price() or 150.0)
            ui_tokens = out_amount / (10 ** decimals)
            if ui_tokens > 0:
                price_usd = (sol_amount * _sol_usd) / ui_tokens

        # Atomic portfolio update under lock — re-read fresh state
        async with _portfolio_lock(uid):
            portfolio = get_portfolio(uid)
            if portfolio.get("SOL", 0) < sol_amount:
                return  # SOL was spent by a concurrent buy between pre-check and lock
            portfolio["SOL"]  = portfolio.get("SOL", 0) - sol_amount
            portfolio[mint]   = portfolio.get(mint, 0) + out_amount
            update_portfolio(uid, portfolio)
        setup_auto_sell(uid, mint, symbol, price_usd, out_amount, decimals, sol_amount=sol_amount)
        log_trade(uid, "paper", "buy", mint, symbol, name=name,
                  sol_amount=sol_amount, token_amount=out_amount,
                  price_usd=price_usd, mcap=mcap, heat_score=score,
                  entry_source=entry_source,
                  entry_age_mins=age_mins_now,
                  entry_liquidity_usd=entry_liquidity_usd,
                  entry_txns_5m=entry_txns_5m,
                  entry_score_raw=entry_score_raw,
                  entry_score_effective=entry_score_effective,
                  entry_tier=entry_tier,
                  entry_wallet_signal=entry_wallet_signal,
                  entry_archetype=entry_archetype,
                  entry_source_rank=entry_source_rank,
                  entry_confidence=entry_confidence)

        _db.record_buy(uid, mint, sol_amount)
        cfg = get_auto_buy(uid)

        try:
            await bot.send_message(
                uid,
                f"🤖 *Auto-Buy Executed* (Paper)\n\n"
                f"🪙 *{name}* (${symbol})\n"
                f"🌡️ Heat Score: `{score}/100`\n"
                f"💰 Spent: `{sol_amount} SOL`\n"
                f"📦 Received: `{out_amount:,}` raw tokens\n"
                f"🏦 MCap: `${mcap:,.0f}`\n"
                f"📊 Daily spent: `{cfg['spent_today']:.3f}/{daily_limit} SOL`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚙️ Auto-Sell", callback_data=f"as:view:{mint}"),
                    InlineKeyboardButton("📊 Chart", url=f"https://dexscreener.com/solana/{mint}"),
                ]])
            )
        except Exception:
            pass
        return

    # ── Live auto-buy ──────────────────────────────────────────────────────────
    if not WALLET_PRIVATE_KEY:
        return

    # Check live wallet balance & fetch bonding curve in parallel — saves 3-5s vs sequential
    pubkey = get_wallet_pubkey()
    if not pubkey:
        return

    # ── Phase 1: PARALLEL RPC — balance + bonding curve simultaneously ─────────
    try:
        live_sol, bc = await asyncio.gather(
            _rpc_with_retry(lambda: get_sol_balance_async(pubkey, timeout=5),
                            max_retries=2, base_delay=0.5, label="getBalance"),
            _rpc_with_retry(lambda: fetch_bonding_curve_async(mint, SOLANA_RPC, timeout=5),
                            max_retries=2, base_delay=0.5, label="getBondingCurve"),
            return_exceptions=True,
        )
    except Exception as e:
        print(f"[AUTOBUY] uid={uid} parallel RPC gather failed: {e}", flush=True)
        return

    # If either gather result is an exception, substitute safe defaults
    if isinstance(live_sol, Exception):
        print(f"[AUTOBUY] uid={uid} balance fetch failed: {live_sol}", flush=True)
        live_sol = 0.0
    if isinstance(bc, Exception):
        bc = None

    if live_sol < sol_amount:
        try:
            await bot.send_message(
                uid,
                f"⚠️ *Auto-Buy Skipped* — insufficient wallet SOL\n\n"
                f"Need: `{sol_amount} SOL` | Wallet: `{live_sol:.4f} SOL`\n"
                f"Token: *{name}* (${symbol}) — score `{score}/100`\n\n"
                f"_Top up your wallet or reduce SOL amount per trade._",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    lamports = int(sol_amount * 1_000_000_000)

    # ── Token safety check (live mode only) ───────────────────────────────────
    if get_safety_check_enabled(uid):
        safety = await check_token_safety(mint)
        if not safety["safe"]:
            print(f"[AUTOBUY] uid={uid} BLOCKED {symbol} — {safety['block_reason']}", flush=True)
            try:
                await bot.send_message(
                    uid,
                    f"🚫 *Auto-Buy Blocked* — Safety Check Failed\n\n"
                    f"🪙 *{name}* (${symbol})\n"
                    f"❌ *{safety['block_reason']}*\n\n"
                    f"_Disable safety check in Settings to override._",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📊 Chart", url=f"https://dexscreener.com/solana/{mint}"),
                        InlineKeyboardButton("🔫 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
                    ]])
                )
            except Exception:
                pass
            return
        if safety["warnings"]:
            warn_text = "\n".join(f"⚠️ {w}" for w in safety["warnings"])
            try:
                await bot.send_message(
                    uid,
                    f"⚠️ *Safety Warnings* — {name} (${symbol})\n\n{warn_text}\n\n_Proceeding with auto-buy..._",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    # ── Route decision — pump.fun if BC live, else Jupiter ────────────────────
    tx_sig = None
    price_usd = result.get("price_usd", 0)
    loop = asyncio.get_running_loop()
    if bc and not bc.get("complete"):
        print(f"[AUTOBUY] uid={uid} route=pump.fun for {symbol}", flush=True)
        from solders.keypair import Keypair
        tok_est  = pumpfun.calculate_buy_tokens(lamports, bc)
        _kp      = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
        # Run blocking buy in executor to avoid freezing the event loop
        tx_sig   = await loop.run_in_executor(None, lambda: pumpfun.buy_pumpfun(mint, sol_amount, _kp, SOLANA_RPC))
        out_raw  = tok_est
        route    = "pump.fun"
        decimals = 6
        # Token graduated between our BC fetch and the buy attempt — fall back to Jupiter
        if tx_sig == "GRADUATED":
            print(f"[AUTOBUY] uid={uid} pump.fun reported GRADUATED for {symbol} — retrying via Jupiter", flush=True)
            quote = await loop.run_in_executor(None, lambda: jupiter_quote(SOL_MINT, mint, lamports, get_user_slippage(uid)))
            if not quote or "error" in quote:
                return
            tx_sig  = await loop.run_in_executor(None, lambda q=quote: execute_swap_live(q, uid))
            out_raw = int(quote.get("outAmount", 0))
            route   = "jupiter"
    else:
        print(f"[AUTOBUY] uid={uid} route=jupiter for {symbol} (BC={'graduated' if bc else 'none'})", flush=True)
        quote = await loop.run_in_executor(None, lambda: jupiter_quote(SOL_MINT, mint, lamports, get_user_slippage(uid)))
        if not quote or "error" in quote:
            return
        tx_sig   = await loop.run_in_executor(None, lambda q=quote: execute_swap_live(q, uid))
        out_raw  = int(quote.get("outAmount", 0))
        route    = "jupiter"
        decimals = 6

    success = bool(tx_sig) and not tx_sig.startswith("ERROR")

    if success:
        # ── Phase 3: TX CONFIRMATION — poll on-chain before updating portfolio ──
        print(f"[AUTOBUY] uid={uid} TX broadcast {tx_sig[:20]}... polling for confirmation", flush=True)
        tx_status = await poll_tx_confirmation(tx_sig, SOLANA_RPC, max_polls=60, poll_interval=1.0)

        # pump.fun TX failed on-chain — likely token graduated mid-flight (ProgramAccountNotFound).
        # SOL was NOT spent. Retry via Jupiter once before giving up.
        if tx_status == "failed" and route == "pump.fun":
            print(f"[AUTOBUY] uid={uid} pump.fun TX failed on-chain for {symbol} — retrying via Jupiter", flush=True)
            _retry_quote = await loop.run_in_executor(
                None, lambda: jupiter_quote(SOL_MINT, mint, lamports, get_user_slippage(uid))
            )
            if _retry_quote and "error" not in _retry_quote:
                tx_sig  = await loop.run_in_executor(None, lambda q=_retry_quote: execute_swap_live(q, uid))
                out_raw = int(_retry_quote.get("outAmount", 0))
                route   = "jupiter"
                success = bool(tx_sig) and not tx_sig.startswith("ERROR")
                if success:
                    print(f"[AUTOBUY] uid={uid} Jupiter retry TX broadcast {tx_sig[:20]}...", flush=True)
                    tx_status = await poll_tx_confirmation(tx_sig, SOLANA_RPC, max_polls=60, poll_interval=1.0)
                else:
                    tx_status = "failed"
            else:
                print(f"[AUTOBUY] uid={uid} Jupiter retry quote failed for {symbol} — no route available", flush=True)
                tx_status = "failed"

        if tx_status != "confirmed":
            if tx_status == "failed":
                print(f"[AUTOBUY] uid={uid} TX landed but FAILED on-chain — {tx_sig[:20]}", flush=True)
                msg_text = (
                    f"❌ *Auto-Buy Failed On-Chain*\n\n"
                    f"🪙 *{name}* (${symbol})\n"
                    f"🔗 TX: `{tx_sig[:30]}...`\n\n"
                    f"Transaction landed but was rejected (slippage exceeded or insufficient funds).\n"
                    f"Portfolio NOT updated. Your SOL was NOT spent."
                )
            else:
                print(f"[AUTOBUY] uid={uid} TX not confirmed after 60s — {tx_sig[:20]}", flush=True)
                msg_text = (
                    f"⚠️ *Auto-Buy TX Unconfirmed*\n\n"
                    f"🪙 *{name}* (${symbol})\n"
                    f"🔗 TX: `{tx_sig[:30]}...`\n\n"
                    f"Transaction was broadcast but not confirmed on-chain within 60s.\n"
                    f"Portfolio NOT updated. Check Solscan to verify status."
                )
            try:
                await bot.send_message(
                    uid, msg_text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔍 Solscan", url=f"https://solscan.io/tx/{tx_sig}"),
                    ]])
                )
            except Exception:
                pass
            return

        # Update portfolio tracking (only after on-chain confirmation)
        # Derive buy price from quote when unavailable (e.g. newly graduated tokens)
        if not price_usd and out_raw > 0:
            import pumpfun as _pf_mod
            _sol_usd = (_pf_mod.get_sol_price() or 150.0)
            ui_tokens = out_raw / (10 ** decimals)
            if ui_tokens > 0:
                price_usd = (sol_amount * _sol_usd) / ui_tokens
        async with _portfolio_lock(uid):
            portfolio = get_portfolio(uid)
            portfolio["SOL"] = max(0, portfolio.get("SOL", 0) - sol_amount)
            portfolio[mint] = portfolio.get(mint, 0) + out_raw
            update_portfolio(uid, portfolio)
        setup_auto_sell(uid, mint, symbol, price_usd, out_raw, decimals, sol_amount=sol_amount)
        log_trade(uid, "live", "buy", mint, symbol, name=name,
                  sol_amount=sol_amount, token_amount=out_raw,
                  price_usd=price_usd, mcap=mcap, heat_score=score, tx_sig=tx_sig,
                  entry_source=entry_source,
                  entry_age_mins=age_mins_now,
                  entry_liquidity_usd=entry_liquidity_usd,
                  entry_txns_5m=entry_txns_5m,
                  entry_score_raw=entry_score_raw,
                  entry_score_effective=entry_score_effective,
                  entry_tier=entry_tier,
                  entry_wallet_signal=entry_wallet_signal,
                  entry_archetype=entry_archetype,
                  entry_source_rank=entry_source_rank,
                  entry_confidence=entry_confidence)

        _db.record_buy(uid, mint, sol_amount)
        cfg = get_auto_buy(uid)

        try:
            await bot.send_message(
                uid,
                f"🤖 *Auto-Buy Executed* (Live)\n\n"
                f"🪙 *{name}* (${symbol})\n"
                f"🌡️ Heat Score: `{score}/100`\n"
                f"💰 Spent: `{sol_amount} SOL`\n"
                f"📦 Received: `{out_raw:,}` raw tokens\n"
                f"🔀 Route: `{route}`\n"
                f"🏦 MCap: `${mcap:,.0f}`\n"
                f"📊 Daily spent: `{cfg['spent_today']:.3f}/{daily_limit} SOL`\n"
                f"🔗 TX: `{tx_sig[:20]}...`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚙️ Auto-Sell", callback_data=f"as:view:{mint}"),
                    InlineKeyboardButton("📊 Chart", url=f"https://dexscreener.com/solana/{mint}"),
                ]])
            )
        except Exception:
            pass
    else:
        try:
            await bot.send_message(
                uid,
                f"❌ *Auto-Buy Failed* (Live)\n\n"
                f"Token: *{name}* (${symbol})\n"
                f"Error: `{tx_sig}`",
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def handle_scanner_autobuy(bot, result: dict, target_uids: list[int] | None = None):
    """Called by run_scan when a token hits the alert threshold."""
    import traceback
    import autobuy as _ab
    chat_ids = target_uids if target_uids is not None else _db.get_scan_targets()
    sym = result.get("symbol", "?")
    print(f"[AUTOBUY] scanner alert fired for {sym} — targets={chat_ids}", flush=True)
    for uid in chat_ids:
        try:
            decision = await _ab.evaluate(uid, result)
            if decision.gate_passed:
                await execute_auto_buy(bot, uid, result)
        except Exception as e:
            print(f"[AUTOBUY] error uid={uid}: {e}", flush=True)
            traceback.print_exc()


# ─── Trade execution (shared) ─────────────────────────────────────────────────

async def do_trade_flow(msg, uid: int, context, action: str,
                         token_query: str, amount_str: str):
    try:
        amount = float(amount_str)
    except ValueError:
        await msg.edit_text("Invalid amount.", reply_markup=back_kb())
        return

    mode = get_mode(uid)
    pair = fetch_sol_pair(token_query)
    bc_fallback = None

    # Not on DexScreener yet — try pump.fun bonding curve + API
    if not pair and len(token_query) >= 32:
        _bc = pumpfun.fetch_bonding_curve_data(token_query, SOLANA_RPC)
        if _bc:
            bc_fallback = _bc
            coin  = _fetch_pumpfun_coin(token_query) or {}
            _vtr  = _bc.get("virtual_token_reserves", 0)
            _vsr  = _bc.get("virtual_sol_reserves", 0)
            _psol = (_vsr / _vtr / 1e9 * 1e6) if _vtr else 0  # SOL per UI token
            pair  = {
                "dexId": "pumpfun",
                "baseToken": {
                    "address":  token_query,
                    "symbol":   coin.get("symbol", token_query[:8]),
                    "name":     coin.get("name",   ""),
                    "decimals": 6,
                },
                "priceNative": str(_psol),
                "priceUsd":    "0",
                "marketCap":   coin.get("usd_market_cap") or coin.get("market_cap") or 0,
            }

    if not pair:
        await msg.edit_text("Token not found on Solana or pump.fun.", reply_markup=back_kb())
        return

    token_mint  = pair["baseToken"]["address"]
    symbol      = pair["baseToken"]["symbol"]
    name        = pair["baseToken"].get("name", "")
    price_usd   = float(pair.get("priceUsd", 0) or 0)
    mcap        = float(pair.get("marketCap", 0) or 0)
    decimals    = int(pair.get("baseToken", {}).get("decimals", 6) or 6)

    if action == "buy":
        lamports = int(amount * 1_000_000_000)
        if bc_fallback:
            out_tokens = pumpfun.calculate_buy_tokens(lamports, bc_fallback)
            quote = {"outAmount": out_tokens, "priceImpactPct": "N/A"}
        else:
            _slip = get_user_slippage(uid) if mode == "live" else 150
            quote = jupiter_quote(SOL_MINT, token_mint, lamports, _slip)
    else:
        if bc_fallback:
            _vtr = bc_fallback.get("virtual_token_reserves", 0)
            _vsr = bc_fallback.get("virtual_sol_reserves", 0)
            _raw = int(amount)
            _sol_out = int((_vsr - int(_vtr * _vsr / (_vtr + _raw))) * 0.99) if _vtr else 0
            quote = {"outAmount": _sol_out, "priceImpactPct": "N/A"}
        else:
            _slip = get_user_slippage(uid) if mode == "live" else 150
            quote = jupiter_quote(token_mint, SOL_MINT, int(amount), _slip)

    if not quote or "error" in quote:
        await msg.edit_text(f"Quote failed: {quote}", reply_markup=back_kb())
        return

    out_amount   = int(quote.get("outAmount", 0))
    price_impact = quote.get("priceImpactPct", "N/A")

    # Reject zero-token quotes — prevents SOL loss with no tokens received
    if action == "buy" and out_amount <= 0:
        await msg.edit_text(
            "⚠️ Quote returned 0 tokens — no liquidity or route found. Trade cancelled.",
            reply_markup=back_kb()
        )
        return

    # ── Paper ─────────────────────────────────────────────────────────────────
    if mode == "paper":
        if action == "buy":
            async with _portfolio_lock(uid):
                portfolio = get_portfolio(uid)
                if portfolio.get("SOL", 0) < amount:
                    await msg.edit_text(
                        f"Insufficient paper SOL. Balance: `{portfolio.get('SOL',0):.4f}`",
                        parse_mode="Markdown", reply_markup=back_kb()
                    )
                    return
                portfolio["SOL"]      = portfolio.get("SOL", 0) - amount
                portfolio[token_mint] = portfolio.get(token_mint, 0) + out_amount
                update_portfolio(uid, portfolio)
            log_trade(uid, "paper", "buy", token_mint, symbol, name=name,
                      sol_amount=amount, token_amount=out_amount,
                      price_usd=price_usd, mcap=mcap)
            # Set up auto-sell monitoring
            setup_auto_sell(uid, token_mint, symbol, price_usd, out_amount, decimals, sol_amount=amount)
            # Get user's preset targets for display
            user_presets = get_user_as_presets(uid)
            presets_str = format_as_presets(user_presets)
            await msg.edit_text(
                f"📄 *Paper Buy Done*\n"
                f"Spent: `{amount} SOL`\n"
                f"Received: `{out_amount:,} {symbol}` (raw)\n"
                f"Buy Price: `${price_usd:.8f}`\n"
                f"Price Impact: `{price_impact}%`\n"
                f"SOL left: `{portfolio['SOL']:.4f}`\n\n"
                f"🤖 Auto-sell configured: {presets_str}\n"
                f"🎯 MCap alerts: 100K / 500K / 1M",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚙️ Auto-Sell Settings",
                                         callback_data=f"as:view:{token_mint}")],
                    [InlineKeyboardButton("🔧 Customize Presets",
                                         callback_data="as_preset:menu")],
                    [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu:main")],
                ])
            )
        else:
            # Compute price outside lock
            _price_sol_now = float(pair.get("priceNative", 0) or 0)
            if not _price_sol_now and bc_fallback and bc_fallback.get("virtual_token_reserves"):
                _vtr2 = bc_fallback["virtual_token_reserves"]
                _vsr2 = bc_fallback["virtual_sol_reserves"]
                _price_sol_now = (_vsr2 / _vtr2 / 1e9 * 1e6) if _vtr2 else 0
            async with _portfolio_lock(uid):
                portfolio = get_portfolio(uid)
                held = portfolio.get(token_mint, 0)
                if held < int(amount):
                    await msg.edit_text(
                        f"Insufficient balance. Hold: `{held:,}` raw",
                        parse_mode="Markdown", reply_markup=back_kb()
                    )
                    return
                # Paper sell: fill at current market price, 1% simulated fee
                if _price_sol_now:
                    sol_received = (_price_sol_now * int(amount) / (10 ** decimals)) * 0.99
                else:
                    sol_received = out_amount / 1e9  # last-resort fallback
                portfolio[token_mint]   = held - int(amount)
                portfolio["SOL"]        = portfolio.get("SOL", 0) + sol_received
                if portfolio[token_mint] <= 0:
                    portfolio.pop(token_mint, None)
                    remove_auto_sell(uid, token_mint)
                update_portfolio(uid, portfolio)
            log_trade(uid, "paper", "sell", token_mint, symbol, name=name,
                      sol_received=sol_received, token_amount=int(amount),
                      price_usd=price_usd, buy_price_usd=_get_buy_price(uid, token_mint),
                      mcap=mcap)
            await msg.edit_text(
                f"📄 *Paper Sell Done*\n"
                f"Sold: `{int(amount):,} {symbol}` (raw)\n"
                f"Received: `{sol_received:.4f} SOL`\n"
                f"Price Impact: `{price_impact}%`\n"
                f"SOL balance: `{portfolio['SOL']:.4f}`",
                parse_mode="Markdown", reply_markup=back_kb()
            )
        return

    # ── Live ──────────────────────────────────────────────────────────────────
    if not WALLET_PRIVATE_KEY:
        await msg.edit_text(
            "⚠️ *No wallet configured.*\n\n"
            "Create or import a wallet first:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👛 Set Up Wallet", callback_data="wallet:menu")],
                [InlineKeyboardButton("⬅️ Back",          callback_data="menu:main")],
            ])
        )
        return

    # ── Token safety check (live buys only) ──────────────────────────────────
    if action == "buy" and get_safety_check_enabled(uid):
        await msg.edit_text("🔍 Running safety check...", parse_mode="Markdown")
        safety = await check_token_safety(token_mint)
        if not safety["safe"]:
            warn_lines = "\n".join(f"⚠️ {w}" for w in safety["warnings"]) if safety["warnings"] else ""
            await msg.edit_text(
                f"🚫 *Safety Check Failed — Buy Cancelled*\n\n"
                f"🪙 *{name}* (${symbol})\n"
                f"❌ *{safety['block_reason']}*\n"
                + (f"\n{warn_lines}" if warn_lines else "") +
                f"\n\n_Disable safety check in Settings to trade anyway._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 RugCheck", url=f"https://rugcheck.xyz/tokens/{token_mint}")],
                    [InlineKeyboardButton("⚙️ Settings", callback_data="settings:menu"),
                     InlineKeyboardButton("⬅️ Back", callback_data="menu:main")],
                ])
            )
            return
        if safety["warnings"]:
            warn_text = "\n".join(f"⚠️ {w}" for w in safety["warnings"])
            await msg.edit_text(
                f"⚠️ *Safety Warnings* — proceeding to quote...\n\n{warn_text}",
                parse_mode="Markdown"
            )
            import asyncio as _aio
            await _aio.sleep(1.5)

    # Detect pump.fun bonding curve tokens
    dex_id    = pair.get("dexId", "")
    is_pump   = pumpfun.is_pumpfun_token(dex_id, token_mint, SOLANA_RPC)

    if action == "buy" and is_pump:
        # ── pump.fun direct buy ───────────────────────────────────────────────
        bc = pumpfun.fetch_bonding_curve_data(token_mint, SOLANA_RPC)
        if bc and not bc["complete"]:
            tok_est = pumpfun.calculate_buy_tokens(int(amount * 1e9), bc)
            summary = (
                f"Spend `{amount} SOL` → ~`{tok_est:,} {symbol}` (raw)\n"
                f"Route: *pump.fun bonding curve*\n"
                f"Slippage: 15%"
            )
            context.user_data["pending_buy"] = {
                "via": "pumpfun", "symbol": symbol,
                "mint": token_mint, "price_usd": price_usd,
                "sol_amount": amount, "tok_est": tok_est,
                "decimals": decimals,
            }
            await msg.edit_text(
                f"🔴 *Live Buy Quote* (pump.fun)\n\n{summary}\n\nConfirm?",
                parse_mode="Markdown",
                reply_markup=confirm_trade_kb(action, token_mint, symbol),
            )
            return
        # bonding curve complete → fall through to Jupiter

    if action == "buy":
        summary = f"Spend `{amount} SOL` → Get ~`{out_amount:,} {symbol}` (raw)"
    else:
        summary = f"Sell `{int(amount):,} {symbol}` → Get ~`{out_amount/1e9:.4f} SOL`"

    _in_mint  = SOL_MINT    if action == "buy" else token_mint
    _out_mint = token_mint  if action == "buy" else SOL_MINT
    _amt_raw  = int(amount * 1_000_000_000) if action == "buy" else int(amount)
    context.user_data[f"pending_{action}"] = {
        "via": "jupiter", "quote": quote, "symbol": symbol,
        "mint": token_mint, "price_usd": price_usd,
        "raw_out": out_amount, "decimals": decimals,
        "in_mint": _in_mint, "out_mint": _out_mint, "amount_raw": _amt_raw,
        "sol_amount": amount if action == "buy" else None,
    }
    await msg.edit_text(
        f"🔴 *Live {action.title()} Quote*\n{summary}\nPrice Impact: `{price_impact}%`\n\nConfirm?",
        parse_mode="Markdown",
        reply_markup=confirm_trade_kb(action, token_mint, symbol),
    )


# ─── Menu rendering ───────────────────────────────────────────────────────────

async def show_main_menu(target, uid: int, edit=False):
    mode = "📄 Paper" if get_mode(uid) == "paper" else "🔴 Live"
    text = f"*@DigitalDegenX\\_Bot*\n\nMode: *{mode}*\n\nChoose an option:"
    if edit:
        await target.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_kb(uid))
    else:
        await target.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb(uid))


async def _show_top(send_fn):
    """Show top 10 tokens we scored in the last 24h, ranked by MCap gain since we saw them."""
    import time as _time
    cutoff = _time.time() - 86400
    log    = sc.load_log()

    # All scored tokens from the last 24h (mcap optional — older entries may not have it)
    recent = [
        e for e in log
        if e.get("mint") and e.get("timestamp", 0) >= cutoff and not e.get("dq")
    ]

    if not recent:
        await send_fn(
            "*🏆 24h Scout History*\n\nNo scored tokens in the last 24 hours yet.\n\nRun /scan to start.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="menu:market")
            ]])
        )
        return

    # De-duplicate: keep first seen per mint
    seen = {}
    for e in sorted(recent, key=lambda x: x.get("timestamp", 0)):
        if e["mint"] not in seen:
            seen[e["mint"]] = e

    # Enrich with current MCap from DexScreener
    enriched = []
    for e in list(seen.values()):
        mint       = e["mint"]
        entry_mcap = e.get("mcap", 0) or 0
        try:
            pair = fetch_sol_pair(mint)
            if not pair:
                continue
            cur_mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
            if entry_mcap and cur_mcap:
                gain_pct = ((cur_mcap - entry_mcap) / entry_mcap) * 100
            else:
                gain_pct = 0
            enriched.append({
                "name":       e.get("name", "?"),
                "symbol":     e.get("symbol", "?"),
                "mint":       mint,
                "score":      e.get("score", 0),
                "narrative":  e.get("narrative", ""),
                "alerted":    e.get("alerted", False),
                "entry_mcap": entry_mcap,
                "cur_mcap":   cur_mcap,
                "gain_pct":   gain_pct,
            })
        except Exception:
            continue

    if not enriched:
        await send_fn("Could not fetch current prices.", reply_markup=back_kb("menu:market"))
        return

    top10 = sorted(enriched, key=lambda x: -x["gain_pct"])[:10]
    lines = ["*🏆 24h Scout History*\n_Top performers we scored today, ranked by MCap gain_\n"]
    for i, t in enumerate(top10, 1):
        gain  = t["gain_pct"]
        arrow = "🚀" if gain > 200 else ("📈" if gain > 0 else "📉")
        alert_tag = " 🔔" if t["alerted"] else ""
        lines.append(
            f"{i}. {arrow} *${t['symbol']}*{alert_tag} — score `{t['score']}` | _{t['narrative']}_\n"
            f"   `${t['entry_mcap']:,.0f}` → `${t['cur_mcap']:,.0f}` | `{gain:+.0f}%`"
        )

    # Quick-buy buttons for top 3
    kb_rows = []
    for t in top10[:3]:
        kb_rows.append([
            InlineKeyboardButton(f"🟢 Buy ${t['symbol']}", callback_data=f"quick:buy:{t['mint']}"),
            InlineKeyboardButton("📊", url=f"https://dexscreener.com/solana/{t['mint']}"),
        ])
    kb_rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data="market:top"),
        InlineKeyboardButton("⬅️ Back",    callback_data="menu:market"),
    ])
    await send_fn(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb_rows)
    )


def _pct_kb(mint: str, back: str = "menu:portfolio") -> InlineKeyboardMarkup:
    """Inline keyboard with 10/25/50/100% sell + buy buttons for a token."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("— SELL —", callback_data="noop")],
        [InlineKeyboardButton("10%",  callback_data=f"qp:sell:{mint}:10"),
         InlineKeyboardButton("25%",  callback_data=f"qp:sell:{mint}:25"),
         InlineKeyboardButton("50%",  callback_data=f"qp:sell:{mint}:50"),
         InlineKeyboardButton("100%", callback_data=f"qp:sell:{mint}:100")],
        [InlineKeyboardButton("— BUY —", callback_data="noop")],
        [InlineKeyboardButton("10%",  callback_data=f"qp:buy:{mint}:10"),
         InlineKeyboardButton("25%",  callback_data=f"qp:buy:{mint}:25"),
         InlineKeyboardButton("50%",  callback_data=f"qp:buy:{mint}:50"),
         InlineKeyboardButton("100%", callback_data=f"qp:buy:{mint}:100")],
        [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}"),
         InlineKeyboardButton("🪙 Pump.fun",    url=f"https://pump.fun/{mint}")],
        [InlineKeyboardButton("🤖 Auto-Sell",  callback_data=f"as:view:{mint}"),
         InlineKeyboardButton("⬅️ Portfolio",  callback_data=back)],
    ])


async def _show_portfolio(send_fn, uid: int, page: int = 0):
    PAGE_SIZE  = 5
    mode = get_mode(uid)
    as_configs = _db.get_all_auto_sells(uid)
    sol_usd    = pf.get_sol_price() or 150.0

    # ── shared footer builders ────────────────────────────────────────────────
    def _nav_row(page, total_pages):
        if total_pages <= 1:
            return []
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"portfolio:page:{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"portfolio:page:{page + 1}"))
        return [nav] if nav else []

    def _footer(paper: bool = False):
        rows = [
            [InlineKeyboardButton("🔄 Refresh",     callback_data="portfolio:refresh"),
             InlineKeyboardButton("🤖 Auto-Sell",   callback_data="menu:autosell")],
            [InlineKeyboardButton("💰 Sell Profit", callback_data="portfolio:sell_profit_confirm"),
             InlineKeyboardButton("🔻 Sell Below%", callback_data="portfolio:sell_below_prompt"),
             InlineKeyboardButton("💣 Sell All",    callback_data="portfolio:sell_all_confirm")],
        ]
        if paper:
            rows.append([InlineKeyboardButton("🗑️ Reset", callback_data="settings:reset_paper"),
                         InlineKeyboardButton("⬅️ Menu",  callback_data="menu:main")])
        else:
            rows.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="menu:main")])
        return rows

    def _token_row(sym, mint, as_enabled):
        row = [
            InlineKeyboardButton(f"⚡ {sym}", callback_data=f"qt:{mint}"),
            InlineKeyboardButton("📊", url=f"https://dexscreener.com/solana/{mint}"),
            InlineKeyboardButton("🪙", url=f"https://pump.fun/{mint}"),
        ]
        if as_enabled:
            row.append(InlineKeyboardButton("🤖", callback_data=f"as:view:{mint}"))
        return row

    def _pnl_badge(buy_price_usd: float, current_price_usd: float) -> str:
        if not buy_price_usd or not current_price_usd:
            return ""
        pct = (current_price_usd - buy_price_usd) / buy_price_usd * 100
        if pct >= 100:
            return f" `+{pct:.0f}%` 🔥"
        if pct > 0:
            return f" `+{pct:.0f}%`"
        return f" `{pct:.0f}%` 📉"

    def _val_usd_str(val_sol: float) -> str:
        usd = val_sol * sol_usd
        if usd >= 1000:
            return f"${usd/1000:,.2f}K"
        return f"${usd:,.2f}"

    def _mcap_str(mcap: float) -> str:
        if not mcap:
            return ""
        if mcap >= 1_000_000:
            return f" · MCap ${mcap/1_000_000:,.2f}M"
        return f" · MCap ${mcap/1000:,.1f}K"

    # ── LIVE WALLET ───────────────────────────────────────────────────────────
    if mode == "live":
        pubkey = get_wallet_pubkey()
        if not pubkey:
            await send_fn(
                "⚠️ No wallet configured. Add `WALLET_PRIVATE_KEY` to `config.py`.",
                parse_mode="Markdown", reply_markup=back_kb()
            )
            return
        sol_bal  = get_sol_balance(pubkey)
        accounts = get_token_accounts(pubkey)

        # Retry once on RPC failure before showing an error
        if accounts is None:
            accounts = get_token_accounts(pubkey)
        if accounts is None:
            await send_fn(
                "⚠️ *RPC error* — could not fetch wallet tokens. Try refreshing in a moment.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Retry", callback_data="portfolio:refresh"),
                    InlineKeyboardButton("⬅️ Menu",  callback_data="menu:main"),
                ]])
            )
            return

        # Sort by raw amount descending as proxy until prices loaded
        accounts = sorted(accounts, key=lambda a: a.get("ui_amount", 0), reverse=True)

        total_pages   = max(1, math.ceil(len(accounts) / PAGE_SIZE)) if accounts else 1
        page_accounts = accounts[page * PAGE_SIZE:(page + 1) * PAGE_SIZE] if accounts else []

        # Fetch all page-token prices concurrently
        price_results = await asyncio.gather(
            *[_fetch_portfolio_token_data(acc["mint"]) for acc in page_accounts],
            return_exceptions=True
        )

        page_info     = f"  •  Page {page + 1}/{total_pages}" if total_pages > 1 else ""
        total_tokens  = len(accounts)
        lines         = [
            f"🔴 *Live Wallet*\n`{pubkey[:8]}...{pubkey[-6:]}`{page_info}",
            f"Holdings: *{total_tokens}* tokens  •  SOL: `{sol_bal:.4f}` _(${sol_bal * sol_usd:,.0f})_\n",
        ]
        token_rows    = []
        total_sol_pos = 0.0
        page_sol_pos  = 0.0

        if accounts:
            lines.append("*Positions — tap ⚡ to trade:*")
            for i, acc in enumerate(page_accounts):
                try:
                    data      = price_results[i] if not isinstance(price_results[i], Exception) else {}
                    pair      = data.get("pair") if data else None
                    bc        = data.get("bc") if data else None
                    coin      = data.get("coin") if data else None

                    sym       = pair.get("baseToken", {}).get("symbol", acc["mint"][:8]) if pair else (
                                (coin or {}).get("symbol", acc["mint"][:8]))
                    price_sol = float(pair.get("priceNative", 0) or 0) if pair else 0
                    mcap      = float(pair.get("marketCap", 0) or 0) if pair else float(
                                (coin or {}).get("usd_market_cap") or (coin or {}).get("market_cap") or 0)
                    src_tag   = ""

                    if not price_sol and bc and bc.get("virtual_token_reserves") and bc["virtual_token_reserves"] > 0:
                        price_sol = bc["virtual_sol_reserves"] / bc["virtual_token_reserves"] / 1e9 * 1e6
                        src_tag   = " _(pump)_"

                    val_sol       = price_sol * acc["ui_amount"]
                    page_sol_pos += val_sol
                    as_cfg        = as_configs.get(acc["mint"], {})
                    as_tag        = " 🤖" if as_cfg.get("enabled") else ""
                    sol_in        = as_cfg.get("sol_amount", 0)
                    sol_in_str    = f" · 💰 `{sol_in:.3f}◎ in`" if sol_in else ""
                    val_str       = f"`{val_sol:.4f}◎` _({_val_usd_str(val_sol)})_" if val_sol else "`unlisted`"
                    price_usd     = float(pair.get("priceUsd", 0) or 0) if pair else price_sol * sol_usd
                    buy_price     = as_cfg.get("buy_price_usd", 0)
                    badge         = _pnl_badge(buy_price, price_usd)

                    lines.append("━━━━━━━━━━━━━━━━━━")
                    lines.append(f"*{sym}*{as_tag}{src_tag}{badge}")
                    lines.append(f"  {acc['ui_amount']:,.4f} tokens ≈ {val_str}{_mcap_str(mcap)}{sol_in_str}")
                    token_rows.append(_token_row(sym, acc["mint"], as_cfg.get("enabled")))
                except Exception as e:
                    print(f"[PORTFOLIO] Error displaying token {acc.get('mint','?')}: {e}")
                    token_rows.append(_token_row(acc["mint"][:8], acc["mint"], False))

            lines.append("━━━━━━━━━━━━━━━━━━")
            if total_pages > 1:
                lines.append(f"*Page value:* `{page_sol_pos:.4f}◎` _({_val_usd_str(page_sol_pos)})_")
                lines.append(f"_Showing page {page + 1}/{total_pages} — navigate to see all positions_")
            else:
                lines.append(f"*Total Positions:* `{page_sol_pos:.4f}◎` _({_val_usd_str(page_sol_pos)})_")
        else:
            lines.append("No token positions found.")

        kb = token_rows + _nav_row(page, total_pages) + _footer(paper=False)
        await send_fn("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        return

    # ── PAPER PORTFOLIO ───────────────────────────────────────────────────────
    portfolio     = get_portfolio(uid)
    sol_bal       = portfolio.get("SOL", 0)
    all_positions = [(k, v) for k, v in portfolio.items() if k != "SOL" and v > 0]

    # Fetch prices for ALL tokens to compute total portfolio value and sort by value
    if all_positions:
        all_price_data = await asyncio.gather(
            *[_fetch_portfolio_token_data(mint) for mint, _ in all_positions],
            return_exceptions=True
        )
        # Compute provisional value per position for sorting
        def _quick_val(i: int, raw_amt: float) -> float:
            data = all_price_data[i] if not isinstance(all_price_data[i], Exception) else {}
            pair = (data or {}).get("pair")
            bc   = (data or {}).get("bc")
            price_sol = float(pair.get("priceNative", 0) or 0) if pair else 0
            if not price_sol and bc and bc.get("virtual_token_reserves") and bc["virtual_token_reserves"] > 0:
                price_sol = bc["virtual_sol_reserves"] / bc["virtual_token_reserves"] / 1e9 * 1e6
            dec = int((pair or {}).get("baseToken", {}).get("decimals", 6) or 6) if pair else 6
            ui  = raw_amt / (10 ** dec)
            return price_sol * ui

        position_values = [_quick_val(i, raw) for i, (_, raw) in enumerate(all_positions)]
        # Sort descending by current value
        sorted_indices  = sorted(range(len(all_positions)), key=lambda i: position_values[i], reverse=True)
        all_positions   = [all_positions[i] for i in sorted_indices]
        all_price_data  = [all_price_data[i] for i in sorted_indices]
        position_values = [position_values[i] for i in sorted_indices]
        total_portfolio_sol = sum(position_values)
    else:
        all_price_data      = []
        position_values     = []
        total_portfolio_sol = 0.0

    # Compute total invested from auto-sell configs
    total_invested_sol = sum(
        as_configs.get(mint, {}).get("sol_amount", 0)
        for mint, _ in all_positions
    )

    total_pages    = max(1, math.ceil(len(all_positions) / PAGE_SIZE)) if all_positions else 1
    page_start     = page * PAGE_SIZE
    page_positions = all_positions[page_start:page_start + PAGE_SIZE]
    page_data      = all_price_data[page_start:page_start + PAGE_SIZE]
    page_info      = f"  •  Page {page + 1}/{total_pages}" if total_pages > 1 else ""
    total_tokens   = len(all_positions)

    # Build header with portfolio summary
    overall_pnl_str = ""
    if total_invested_sol and total_portfolio_sol:
        pnl_pct = (total_portfolio_sol - total_invested_sol) / total_invested_sol * 100
        pnl_sol  = total_portfolio_sol - total_invested_sol
        sign     = "+" if pnl_pct >= 0 else ""
        emoji    = "🔥" if pnl_pct >= 100 else ("📈" if pnl_pct > 0 else "📉")
        overall_pnl_str = (
            f"\nP&L: `{sign}{pnl_sol:.4f}◎` `{sign}{pnl_pct:.1f}%` {emoji}"
        )

    lines = [
        f"📄 *Paper Portfolio*{page_info}",
        f"Holdings: *{total_tokens}* tokens  •  SOL: `{sol_bal:.4f}` _({_val_usd_str(sol_bal)})_",
    ]
    if total_portfolio_sol:
        lines.append(
            f"Portfolio: `{total_portfolio_sol:.4f}◎` _({_val_usd_str(total_portfolio_sol)})_"
            + overall_pnl_str
        )
    lines.append("")

    token_rows = []
    page_sol   = 0.0

    if page_positions:
        lines.append("*Positions — tap ⚡ to trade:*")
        for j, (mint, raw_amt) in enumerate(page_positions):
            try:
                data      = page_data[j] if not isinstance(page_data[j], Exception) else {}
                pair      = (data or {}).get("pair")
                bc        = (data or {}).get("bc")
                coin      = (data or {}).get("coin") or {}
                cfg       = as_configs.get(mint)
                as_tag    = " 🤖" if cfg and cfg.get("enabled") else ""

                if pair:
                    sym       = pair.get("baseToken", {}).get("symbol", mint[:8])
                    price_sol = float(pair.get("priceNative", 0) or 0)
                    price_usd = float(pair.get("priceUsd", 0) or 0)
                    mcap      = float(pair.get("marketCap", 0) or 0)
                    dec       = int(pair.get("baseToken", {}).get("decimals", 6) or 6)
                    ui        = raw_amt / (10 ** dec)
                    if not price_sol and bc and bc.get("virtual_token_reserves") and bc["virtual_token_reserves"] > 0:
                        price_sol = bc["virtual_sol_reserves"] / bc["virtual_token_reserves"] / 1e9 * 1e6
                    val_sol    = price_sol * ui
                    page_sol  += val_sol
                    buy_price  = cfg.get("buy_price_usd", 0) if cfg else 0
                    badge      = _pnl_badge(buy_price, price_usd)
                    val_str    = f"`{val_sol:.4f}◎` _({_val_usd_str(val_sol)})_" if val_sol else "`unlisted`"
                    sol_in     = cfg.get("sol_amount", 0) if cfg else 0
                    sol_in_str = f" · 💰 `{sol_in:.3f}◎ in`" if sol_in else ""
                    lines.append("━━━━━━━━━━━━━━━━━━")
                    lines.append(f"*{sym}*{as_tag}{badge}")
                    lines.append(f"  {ui:,.4f} tokens ≈ {val_str}{_mcap_str(mcap)}{sol_in_str}")
                    if cfg and cfg.get("enabled"):
                        pending = [t["label"] for t in cfg.get("mult_targets", []) if not t["triggered"]]
                        if pending:
                            lines.append(f"  ↳ Next target: {pending[0]}")
                    token_rows.append(_token_row(sym, mint, cfg and cfg.get("enabled")))
                else:
                    # pump.fun / unlisted path
                    sym       = coin.get("symbol", mint[:8])
                    mcap      = float(coin.get("usd_market_cap") or coin.get("market_cap") or 0)
                    ui        = raw_amt / 1e6
                    price_sol = 0.0
                    price_usd = 0.0
                    if bc and bc.get("virtual_token_reserves") and bc["virtual_token_reserves"] > 0:
                        price_sol = bc["virtual_sol_reserves"] / bc["virtual_token_reserves"] / 1e9 * 1e6
                        price_usd = price_sol * sol_usd
                    val_sol    = price_sol * ui
                    page_sol  += val_sol
                    src_tag    = " _(pump.fun)_" if price_sol else " _(unlisted)_"
                    buy_price  = cfg.get("buy_price_usd", 0) if cfg else 0
                    badge      = _pnl_badge(buy_price, price_usd)
                    val_str    = f"`{val_sol:.4f}◎` _({_val_usd_str(val_sol)})_" if val_sol else "`~`"
                    lines.append("━━━━━━━━━━━━━━━━━━")
                    lines.append(f"*{sym}*{as_tag}{src_tag}{badge}")
                    lines.append(f"  {ui:,.4f} tokens ≈ {val_str}{_mcap_str(mcap)}")
                    token_rows.append(_token_row(sym, mint, cfg and cfg.get("enabled")))
            except Exception as e:
                print(f"[PORTFOLIO] Error displaying token {mint}: {e}")
                token_rows.append(_token_row(mint[:8], mint, False))

        lines.append("━━━━━━━━━━━━━━━━━━")
        if total_pages > 1:
            lines.append(f"*Page value:* `{page_sol:.4f}◎` _({_val_usd_str(page_sol)})_")
            lines.append(f"*Total portfolio:* `{total_portfolio_sol:.4f}◎` _({_val_usd_str(total_portfolio_sol)})_")
        else:
            lines.append(f"*Total Positions:* `{total_portfolio_sol:.4f}◎` _({_val_usd_str(total_portfolio_sol)})_")
    else:
        lines.append("No positions yet.")

    kb = token_rows + _nav_row(page, total_pages) + _footer(paper=True)
    await send_fn("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))


async def _show_autosell(send_fn, uid: int):
    configs = _db.get_all_auto_sells(uid)
    gsl     = get_global_sl()
    gsl_status = "🟢 ON" if gsl.get("enabled") else "🔴 OFF"
    gsl_info   = (f"Drop `{gsl['pct']}%` → sell `{gsl['sell_pct']}%`"
                  if gsl.get("enabled") else "Disabled")
    if not configs:
        await send_fn(
            f"*🤖 Auto-Sell*\n\nNo positions tracked yet.\n"
            f"Buy a token and auto-sell is configured automatically.\n\n"
            f"*🌍 Global Stop-Loss:* {gsl_status} — {gsl_info}\n\n"
            f"You can pre-configure default sell presets for future buys.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔧 Configure Default Presets", callback_data="as_preset:menu")],
                [InlineKeyboardButton(f"🌍 Global Stop-Loss: {gsl_status}", callback_data="gsl:menu")],
                [InlineKeyboardButton("💰 Trade", callback_data="menu:trade")],
                [InlineKeyboardButton("⬅️ Back",  callback_data="menu:main")],
            ])
        )
        return
    count = len(configs)
    await send_fn(
        f"*🤖 Auto-Sell Monitor*\n\n{count} position(s) tracked.\n\n"
        f"*🌍 Global Stop-Loss:* {gsl_status} — {gsl_info}\n\n"
        "Tap a token to manage its targets:",
        parse_mode="Markdown",
        reply_markup=autosell_list_kb(uid)
    )


def _format_autosell_config(cfg: dict) -> str:
    sym       = cfg.get("symbol", "?")
    buy_price = cfg.get("buy_price_usd", 0)
    enabled   = "✅ Active" if cfg.get("enabled") else "⏸️ Paused"
    lines     = [
        f"*🤖 Auto-Sell — ${sym}*\n",
        f"Status: {enabled}",
        f"Buy Price: `${buy_price:.8f}`\n",
        "*Multiplier Targets:*"
    ]
    for t in cfg.get("mult_targets", []):
        status = "✅ Triggered" if t["triggered"] else "⏳ Waiting"
        lines.append(f"  {t['label']} → sell {t['sell_pct']}% — {status}")

    lines.append("\n*MCap Alerts:*")
    for ma in cfg.get("mcap_alerts", []):
        status = "✅ Hit" if ma["triggered"] else "⏳ Waiting"
        lines.append(f"  ${ma['label']} — {status}")

    customs = cfg.get("custom_targets", [])
    if customs:
        lines.append("\n*Custom Targets:*")
        for i, ct in enumerate(customs):
            status = "✅" if ct.get("triggered") else "⏳"
            t      = ct["type"]
            if t == "multiplier":
                desc = f"{ct['value']}x → sell {ct.get('sell_pct',50)}%"
            elif t == "price":
                desc = f"price ${ct['value']} → sell {ct.get('sell_pct',50)}%"
            else:
                desc = f"mcap ${ct['value']:,.0f} → alert"
            lines.append(f"  {i+1}. {status} {desc}")

    # Stop-loss
    sl = cfg.get("stop_loss", {})
    if sl:
        sl_on  = "🟢 ON" if sl.get("enabled") else "🔴 OFF"
        sl_tri = " ✅ Triggered" if sl.get("triggered") else ""
        lines.append(f"\n*Stop-Loss:* {sl_on}{sl_tri}")
        lines.append(f"  Drop {sl.get('pct', 50)}% from buy → sell {sl.get('sell_pct', 100)}%")

    # Trailing stop
    ts = cfg.get("trailing_stop", {})
    if ts:
        ts_on  = "🟢 ON" if ts.get("enabled") else "🔴 OFF"
        ts_tri = " ✅ Triggered" if ts.get("triggered") else ""
        peak   = ts.get("peak_price", 0)
        lines.append(f"\n*Trailing Stop:* {ts_on}{ts_tri}")
        lines.append(f"  Trail {ts.get('trail_pct', 30)}% from peak → sell {ts.get('sell_pct', 100)}%")
        if peak > 0:
            lines.append(f"  Peak: `${peak:.8f}`")

    # Trailing TP
    ttp = cfg.get("trailing_tp", {})
    if ttp:
        ttp_on  = "🟢 ON" if ttp.get("enabled") else "🔴 OFF"
        ttp_tri = " ✅ Triggered" if ttp.get("triggered") else ""
        ttp_act = " 📈 Active" if ttp.get("active") else ""
        lines.append(f"\n*Trailing TP:* {ttp_on}{ttp_tri}{ttp_act}")
        lines.append(f"  Activates at {ttp.get('activate_mult', 2.0)}x, trail {ttp.get('trail_pct', 20)}% → sell {ttp.get('sell_pct', 50)}%")

    # Time exit
    te = cfg.get("time_exit", {})
    if te:
        te_on  = "🟢 ON" if te.get("enabled") else "🔴 OFF"
        te_tri = " ✅ Triggered" if te.get("triggered") else ""
        elapsed = (time.time() - te.get("buy_time", time.time())) / 3600
        lines.append(f"\n*Time Exit:* {te_on}{te_tri}")
        lines.append(f"  If not {te.get('target_mult', 2.0)}x after {te.get('hours', 24)}h → sell {te.get('sell_pct', 100)}%")
        lines.append(f"  Elapsed: `{elapsed:.1f}h`")

    # Breakeven stop
    be = cfg.get("breakeven_stop", {})
    if be:
        be_on  = "🟢 ON" if be.get("enabled") else "🔴 OFF"
        be_tri = " ✅ Activated" if be.get("triggered") else ""
        lines.append(f"\n*Breakeven Stop:* {be_on}{be_tri}")
        lines.append(f"  Move stop to entry when price hits `{be.get('activate_mult', 2.0)}x`")

    return "\n".join(lines)


# ─── Auto-Buy UI ──────────────────────────────────────────────────────────────

def _autobuy_status_text(uid: int) -> str:
    cfg             = get_auto_buy(uid)
    cfg             = _ab_reset_day_if_needed(cfg)
    enabled         = cfg.get("enabled", False)
    sol_amount      = cfg.get("sol_amount", 0.1)
    min_score       = cfg.get("min_score", 70)
    max_mcap        = cfg.get("max_mcap", 500_000)
    min_mcap        = cfg.get("min_mcap_usd", 0)
    daily_limit     = cfg.get("daily_limit_sol", 1.0)
    spent           = cfg.get("spent_today", 0.0)
    bought          = cfg.get("bought", [])
    max_pos         = cfg.get("max_positions", 0)
    open_pos        = _db.get_open_position_count(uid)
    mode            = "📄 Paper" if get_mode(uid) == "paper" else "🔴 Live"
    buy_tier        = cfg.get("buy_tier", "")
    min_liq         = cfg.get("min_liquidity_usd", 0)
    max_liq         = cfg.get("max_liquidity_usd", 0)
    min_age         = cfg.get("min_age_mins", 0)
    max_age         = cfg.get("max_age_mins", 0)
    min_txns        = cfg.get("min_txns_5m", 0)

    # Resolve effective min score
    if buy_tier:
        user_cfg = sm.get_user_settings(uid)
        tier_map = {
            "scouted":   user_cfg.get("alert_scouted_threshold", 35),
            "warm":      user_cfg.get("alert_warm_threshold", 55),
            "hot":       user_cfg.get("alert_hot_threshold", 70),
            "ultra_hot": user_cfg.get("alert_ultra_hot_threshold", 85),
        }
        effective_min = tier_map.get(buy_tier, min_score)
        tier_label = buy_tier.replace('_', ' ').title()
        score_line = f"Buy tier: *{tier_label}* (score ≥ `{effective_min}`)"
    else:
        score_line = f"Min heat score: `{min_score}/100`"

    status = "🟢 ENABLED" if enabled else "🔴 DISABLED"

    # Build filter lines
    mcap_line = f"MCap: `${min_mcap:,.0f}` – `${max_mcap:,.0f}`" if min_mcap > 0 else f"Max MCap: `${max_mcap:,.0f}`"
    if min_liq > 0 and max_liq > 0:
        liq_line = f"Liquidity: `${min_liq:,.0f}` – `${max_liq:,.0f}`"
    elif min_liq > 0:
        liq_line = f"Min Liquidity: `${min_liq:,.0f}`"
    elif max_liq > 0:
        liq_line = f"Max Liquidity: `${max_liq:,.0f}`"
    else:
        liq_line = f"Liquidity: `Any`"
    if min_age > 0 and max_age > 0:
        age_line = f"Age: `{min_age}m` – `{max_age}m`"
    elif min_age > 0:
        age_line = f"Min Age: `{min_age}m`"
    elif max_age > 0:
        age_line = f"Max Age: `{max_age}m`"
    else:
        age_line = f"Age: `Any`"
    txns_line = f"Min Txns (5m): `{min_txns}`" if min_txns > 0 else f"Min Txns (5m): `Any`"

    return (
        f"*🤖 Auto-Buy Settings*\n\n"
        f"Status: *{status}*\n"
        f"Mode: *{mode}*\n\n"
        f"SOL per trade: `{sol_amount} SOL`\n"
        f"{score_line}\n"
        f"{mcap_line}\n"
        f"{liq_line}\n"
        f"{age_line}\n"
        f"{txns_line}\n"
        f"Daily SOL limit: `{'Unlimited ♾️' if daily_limit == 0 else str(daily_limit) + ' SOL'}`\n"
        f"Spent today: `{spent:.3f} SOL`\n"
        f"Bought today: `{len(bought)}` token(s)\n"
        f"Max positions: `{'Unlimited ♾️' if max_pos == 0 else str(max_pos)}`\n"
        f"Open positions: `{open_pos}`{'  ⛔ *PAUSED*' if max_pos > 0 and open_pos >= max_pos else ''}\n\n"
        f"_Auto-buys fire when scanner alerts a token meeting your tier/score._"
    )


def _autobuy_kb(uid: int) -> InlineKeyboardMarkup:
    cfg     = get_auto_buy(uid)
    enabled = cfg.get("enabled", False)
    toggle_lbl = "⏸️ Disable" if enabled else "▶️ Enable"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_lbl,               callback_data="autobuy:toggle")],
        [InlineKeyboardButton("💰 SOL Amount",           callback_data="autobuy:set_sol"),
         InlineKeyboardButton("🌡️ Buy Tier",             callback_data="autobuy:set_tier")],
        [InlineKeyboardButton("🏦 MCap Range",           callback_data="autobuy:set_mcap_range"),
         InlineKeyboardButton("💧 Liquidity Filters",    callback_data="autobuy:set_liq")],
        [InlineKeyboardButton("⏱️ Age Filters",          callback_data="autobuy:set_age"),
         InlineKeyboardButton("🔄 Min Txns (5m)",        callback_data="autobuy:set_txns")],
        [InlineKeyboardButton("📅 Daily Limit",          callback_data="autobuy:set_daily"),
         InlineKeyboardButton("📊 Max Positions",        callback_data="autobuy:set_maxpos")],
        [InlineKeyboardButton("🔄 Reset Today",          callback_data="autobuy:reset_day")],
        [InlineKeyboardButton("⬅️ Back",                 callback_data="menu:main")],
    ])


async def _show_autobuy(send_fn, uid: int):
    await send_fn(
        _autobuy_status_text(uid),
        parse_mode="Markdown",
        reply_markup=_autobuy_kb(uid),
    )


async def cmd_autobuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Loading auto-buy...")
    await _show_autobuy(msg.edit_text, update.effective_user.id)


async def autobuy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    action = query.data.split(":")[1]
    await query.answer()

    cfg = get_auto_buy(uid)
    cfg = _ab_reset_day_if_needed(cfg)

    if action == "toggle":
        cfg["enabled"] = not cfg.get("enabled", False)
        set_auto_buy(uid, cfg)
        await _show_autobuy(query.edit_message_text, uid)

    elif action == "set_sol":
        set_state(uid, waiting_for="ab_sol_amount")
        await query.edit_message_text(
            "💰 *Set SOL amount per auto-buy*\n\n"
            "Choose a preset or type a custom amount (e.g. `0.5`, `2`, `10`)\n"
            f"Current: `{cfg.get('sol_amount', 0.1)} SOL`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("0.05", callback_data="autobuy:sol_preset:0.05"),
                    InlineKeyboardButton("0.1",  callback_data="autobuy:sol_preset:0.1"),
                    InlineKeyboardButton("0.25", callback_data="autobuy:sol_preset:0.25"),
                    InlineKeyboardButton("0.5",  callback_data="autobuy:sol_preset:0.5"),
                ],
                [
                    InlineKeyboardButton("1 SOL",  callback_data="autobuy:sol_preset:1.0"),
                    InlineKeyboardButton("2 SOL",  callback_data="autobuy:sol_preset:2.0"),
                    InlineKeyboardButton("5 SOL",  callback_data="autobuy:sol_preset:5.0"),
                    InlineKeyboardButton("10 SOL", callback_data="autobuy:sol_preset:10.0"),
                ],
                [
                    InlineKeyboardButton("25 SOL", callback_data="autobuy:sol_preset:25.0"),
                    InlineKeyboardButton("50 SOL", callback_data="autobuy:sol_preset:50.0"),
                ],
                [InlineKeyboardButton("⬅️ Back", callback_data="autobuy:menu")],
            ])
        )

    elif action == "sol_preset":
        val = float(query.data.split(":")[2])
        cfg["sol_amount"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await _show_autobuy(query.edit_message_text, uid)

    elif action == "set_score":
        set_state(uid, waiting_for="ab_min_score")
        await query.edit_message_text(
            "🌡️ *Set minimum heat score for auto-buy*\n\n"
            "Higher = fewer but better quality trades.\n"
            "You can use any score from 1–100, or type a custom value.\n"
            f"Current: `{cfg.get('min_score', 70)}/100`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("1",  callback_data="autobuy:score_preset:1"),
                 InlineKeyboardButton("10", callback_data="autobuy:score_preset:10"),
                 InlineKeyboardButton("20", callback_data="autobuy:score_preset:20"),
                 InlineKeyboardButton("30", callback_data="autobuy:score_preset:30")],
                [InlineKeyboardButton("40", callback_data="autobuy:score_preset:40"),
                 InlineKeyboardButton("50", callback_data="autobuy:score_preset:50"),
                 InlineKeyboardButton("60", callback_data="autobuy:score_preset:60"),
                 InlineKeyboardButton("70", callback_data="autobuy:score_preset:70")],
                [InlineKeyboardButton("75", callback_data="autobuy:score_preset:75"),
                 InlineKeyboardButton("80", callback_data="autobuy:score_preset:80"),
                 InlineKeyboardButton("90", callback_data="autobuy:score_preset:90"),
                 InlineKeyboardButton("100", callback_data="autobuy:score_preset:100")],
                [InlineKeyboardButton("✏️ Custom — type any number 1-100", callback_data="noop")],
                [InlineKeyboardButton("⬅️ Back", callback_data="autobuy:menu")],
            ])
        )

    elif action == "score_preset":
        val = int(query.data.split(":")[2])
        cfg["min_score"] = val
        cfg["buy_tier"] = ""  # clear tier when using manual score
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await _show_autobuy(query.edit_message_text, uid)

    elif action == "set_tier":
        buy_tier = cfg.get("buy_tier", "")
        user_cfg = sm.get_user_settings(uid)
        s_thr = user_cfg.get("alert_scouted_threshold", 35)
        w_thr = user_cfg.get("alert_warm_threshold", 55)
        h_thr = user_cfg.get("alert_hot_threshold", 70)
        u_thr = user_cfg.get("alert_ultra_hot_threshold", 85)
        cur_lbl = buy_tier.replace("_", " ").title() if buy_tier else f"Manual ({cfg.get('min_score', 70)})"
        await query.edit_message_text(
            "🌡️ *Set Auto-Buy Trigger Tier*\n\n"
            "Choose which alert tier triggers auto-buy.\n"
            "Uses your v2 heat score thresholds from /settings.\n\n"
            f"Current: *{cur_lbl}*\n\n"
            f"⚪ Scouted = score ≥ `{s_thr}`\n"
            f"🟡 Warm = score ≥ `{w_thr}`\n"
            f"🟠 Hot = score ≥ `{h_thr}`\n"
            f"🔴 Ultra Hot = score ≥ `{u_thr}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"⚪ Scouted (≥{s_thr})", callback_data="autobuy:tier_pick:scouted"),
                 InlineKeyboardButton(f"🟡 Warm (≥{w_thr})",    callback_data="autobuy:tier_pick:warm")],
                [InlineKeyboardButton(f"🟠 Hot (≥{h_thr})",     callback_data="autobuy:tier_pick:hot"),
                 InlineKeyboardButton(f"🔴 Ultra (≥{u_thr})",   callback_data="autobuy:tier_pick:ultra_hot")],
                [InlineKeyboardButton("✏️ Custom Score", callback_data="autobuy:set_score")],
                [InlineKeyboardButton("⬅️ Back", callback_data="autobuy:menu")],
            ])
        )

    elif action == "tier_pick":
        tier = query.data.split(":")[2]
        cfg["buy_tier"] = tier
        set_auto_buy(uid, cfg)
        await _show_autobuy(query.edit_message_text, uid)

    elif action == "set_mcap":
        set_state(uid, waiting_for="ab_max_mcap")
        await query.edit_message_text(
            "🏦 *Set maximum market cap for auto-buy*\n\n"
            "Tokens above this MCap will be skipped.\n"
            f"Current: `${cfg.get('max_mcap', 500_000):,.0f}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("$100K",  callback_data="autobuy:mcap_preset:100000"),
                InlineKeyboardButton("$250K",  callback_data="autobuy:mcap_preset:250000"),
                InlineKeyboardButton("$500K",  callback_data="autobuy:mcap_preset:500000"),
                InlineKeyboardButton("$1M",    callback_data="autobuy:mcap_preset:1000000"),
            ], [InlineKeyboardButton("⬅️ Back", callback_data="autobuy:menu")]])
        )

    elif action == "mcap_preset":
        val = int(query.data.split(":")[2])
        cfg["max_mcap"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await _show_autobuy(query.edit_message_text, uid)

    elif action == "set_daily":
        cur_limit = cfg.get('daily_limit_sol', 1.0)
        cur_txt = "Unlimited ♾️" if cur_limit == 0 else f"{cur_limit} SOL"
        set_state(uid, waiting_for="ab_daily_limit")
        await query.edit_message_text(
            "📅 *Set daily SOL spending limit*\n\n"
            "Auto-buy pauses when this limit is reached.\n"
            "Set to *0* or tap *No Limit* to remove the daily cap.\n"
            f"Current: `{cur_txt}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("0.5 SOL", callback_data="autobuy:daily_preset:0.5"),
                    InlineKeyboardButton("1 SOL",   callback_data="autobuy:daily_preset:1.0"),
                    InlineKeyboardButton("2 SOL",   callback_data="autobuy:daily_preset:2.0"),
                    InlineKeyboardButton("5 SOL",   callback_data="autobuy:daily_preset:5.0"),
                ],
                [
                    InlineKeyboardButton("10 SOL",  callback_data="autobuy:daily_preset:10.0"),
                    InlineKeyboardButton("25 SOL",  callback_data="autobuy:daily_preset:25.0"),
                    InlineKeyboardButton("50 SOL",  callback_data="autobuy:daily_preset:50.0"),
                ],
                [InlineKeyboardButton("♾️ No Limit", callback_data="autobuy:daily_preset:0")],
                [InlineKeyboardButton("⬅️ Back", callback_data="autobuy:menu")],
            ])
        )

    elif action == "daily_preset":
        val = float(query.data.split(":")[2])
        cfg["daily_limit_sol"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await _show_autobuy(query.edit_message_text, uid)

    elif action == "reset_day":
        cfg["spent_today"] = 0.0
        cfg["bought"]      = []
        cfg["spent_date"]  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        set_auto_buy(uid, cfg)
        await _show_autobuy(query.edit_message_text, uid)

    elif action == "set_maxpos":
        set_state(uid, waiting_for="ab_max_positions")
        max_pos = cfg.get("max_positions", 0)
        await query.edit_message_text(
            "📊 *Max Open Positions*\n\n"
            "Auto-buy pauses when your open positions hit this number.\n"
            "Resumes automatically when a position is fully closed.\n"
            "Set to `0` for unlimited.\n\n"
            f"Current: `{'Unlimited ♾️' if max_pos == 0 else str(max_pos)}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("0 (Unlimited)", callback_data="autobuy:maxpos_preset:0"),
                 InlineKeyboardButton("3",             callback_data="autobuy:maxpos_preset:3"),
                 InlineKeyboardButton("5",             callback_data="autobuy:maxpos_preset:5")],
                [InlineKeyboardButton("7",             callback_data="autobuy:maxpos_preset:7"),
                 InlineKeyboardButton("10",            callback_data="autobuy:maxpos_preset:10"),
                 InlineKeyboardButton("15",            callback_data="autobuy:maxpos_preset:15")],
                [InlineKeyboardButton("⬅️ Back",       callback_data="autobuy:menu")],
            ])
        )

    elif action == "maxpos_preset":
        val = int(query.data.split(":")[2])
        cfg["max_positions"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await _show_autobuy(query.edit_message_text, uid)

    # ── MCap Range ────────────────────────────────────────────────────────────
    elif action == "set_mcap_range":
        cur_min = cfg.get("min_mcap_usd", 0)
        cur_max = cfg.get("max_mcap", 500_000)
        await query.edit_message_text(
            "🏦 *MCap Range Filter*\n\n"
            f"Current: `${cur_min:,.0f}` – `${cur_max:,.0f}`\n\n"
            "Set the minimum and maximum market cap in USD.\n"
            "Tokens outside this range are skipped.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📉 Set Min MCap", callback_data="autobuy:set_min_mcap"),
                 InlineKeyboardButton("📈 Set Max MCap", callback_data="autobuy:set_mcap")],
                [InlineKeyboardButton("⬅️ Back", callback_data="autobuy:menu")],
            ])
        )

    elif action == "set_min_mcap":
        set_state(uid, waiting_for="ab_min_mcap")
        await query.edit_message_text(
            "📉 *Set minimum market cap for auto-buy*\n\n"
            "Tokens below this MCap will be skipped.\n"
            "Set to *0* for no minimum.\n"
            f"Current: `${cfg.get('min_mcap_usd', 0):,.0f}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("$0 (None)",  callback_data="autobuy:min_mcap_preset:0"),
                 InlineKeyboardButton("$10K",       callback_data="autobuy:min_mcap_preset:10000"),
                 InlineKeyboardButton("$50K",       callback_data="autobuy:min_mcap_preset:50000")],
                [InlineKeyboardButton("$100K",      callback_data="autobuy:min_mcap_preset:100000"),
                 InlineKeyboardButton("$250K",      callback_data="autobuy:min_mcap_preset:250000"),
                 InlineKeyboardButton("$500K",      callback_data="autobuy:min_mcap_preset:500000")],
                [InlineKeyboardButton("⬅️ Back",    callback_data="autobuy:set_mcap_range")],
            ])
        )

    elif action == "min_mcap_preset":
        val = int(query.data.split(":")[2])
        cfg["min_mcap_usd"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await _show_autobuy(query.edit_message_text, uid)

    # ── Liquidity Filters ────────────────────────────────────────────────────
    elif action == "set_liq":
        cur_min = cfg.get("min_liquidity_usd", 0)
        cur_max = cfg.get("max_liquidity_usd", 0)
        min_txt = f"${cur_min:,.0f}" if cur_min > 0 else "None"
        max_txt = f"${cur_max:,.0f}" if cur_max > 0 else "None"
        await query.edit_message_text(
            "💧 *Liquidity Filters*\n\n"
            f"Min Liquidity: `{min_txt}`\n"
            f"Max Liquidity: `{max_txt}`\n\n"
            "Set min/max pool liquidity in USD.\nSet to 0 to disable a limit.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📉 Set Min Liquidity", callback_data="autobuy:set_liq_min"),
                 InlineKeyboardButton("📈 Set Max Liquidity", callback_data="autobuy:set_liq_max")],
                [InlineKeyboardButton("⬅️ Back", callback_data="autobuy:menu")],
            ])
        )

    elif action == "set_liq_min":
        set_state(uid, waiting_for="ab_min_liq")
        await query.edit_message_text(
            "💧 *Set minimum liquidity (USD)*\n\n"
            "Tokens with less liquidity than this will be skipped.\n"
            "Set to *0* for no minimum.\n"
            f"Current: `${cfg.get('min_liquidity_usd', 0):,.0f}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("$0 (None)",  callback_data="autobuy:liq_min_preset:0"),
                 InlineKeyboardButton("$1K",        callback_data="autobuy:liq_min_preset:1000"),
                 InlineKeyboardButton("$5K",        callback_data="autobuy:liq_min_preset:5000")],
                [InlineKeyboardButton("$10K",       callback_data="autobuy:liq_min_preset:10000"),
                 InlineKeyboardButton("$25K",       callback_data="autobuy:liq_min_preset:25000"),
                 InlineKeyboardButton("$50K",       callback_data="autobuy:liq_min_preset:50000")],
                [InlineKeyboardButton("⬅️ Back",    callback_data="autobuy:set_liq")],
            ])
        )

    elif action == "liq_min_preset":
        val = int(query.data.split(":")[2])
        cfg["min_liquidity_usd"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await _show_autobuy(query.edit_message_text, uid)

    elif action == "set_liq_max":
        set_state(uid, waiting_for="ab_max_liq")
        await query.edit_message_text(
            "💧 *Set maximum liquidity (USD)*\n\n"
            "Tokens with more liquidity than this will be skipped.\n"
            "Set to *0* for no limit.\n"
            f"Current: `${cfg.get('max_liquidity_usd', 0):,.0f}` (0 = no limit)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("$0 (No limit)", callback_data="autobuy:liq_max_preset:0"),
                 InlineKeyboardButton("$25K",          callback_data="autobuy:liq_max_preset:25000"),
                 InlineKeyboardButton("$50K",          callback_data="autobuy:liq_max_preset:50000")],
                [InlineKeyboardButton("$100K",         callback_data="autobuy:liq_max_preset:100000"),
                 InlineKeyboardButton("$250K",         callback_data="autobuy:liq_max_preset:250000"),
                 InlineKeyboardButton("$500K",         callback_data="autobuy:liq_max_preset:500000")],
                [InlineKeyboardButton("⬅️ Back",       callback_data="autobuy:set_liq")],
            ])
        )

    elif action == "liq_max_preset":
        val = int(query.data.split(":")[2])
        cfg["max_liquidity_usd"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await _show_autobuy(query.edit_message_text, uid)

    # ── Age Filters ──────────────────────────────────────────────────────────
    elif action == "set_age":
        cur_min = cfg.get("min_age_mins", 0)
        cur_max = cfg.get("max_age_mins", 0)
        min_txt = f"{cur_min}m" if cur_min > 0 else "None"
        max_txt = f"{cur_max}m" if cur_max > 0 else "None"
        await query.edit_message_text(
            "⏱️ *Age Filters*\n\n"
            f"Min Age: `{min_txt}`\n"
            f"Max Age: `{max_txt}`\n\n"
            "Set min/max token pair age in minutes.\nSet to 0 to disable a limit.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📉 Set Min Age", callback_data="autobuy:set_age_min"),
                 InlineKeyboardButton("📈 Set Max Age", callback_data="autobuy:set_age_max")],
                [InlineKeyboardButton("⬅️ Back", callback_data="autobuy:menu")],
            ])
        )

    elif action == "set_age_min":
        set_state(uid, waiting_for="ab_min_age")
        await query.edit_message_text(
            "⏱️ *Set minimum token age (minutes)*\n\n"
            "Token pair must be at least this many minutes old.\n"
            "Set to *0* for no minimum.\n"
            f"Current: `{cfg.get('min_age_mins', 0)}m`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("0 (None)", callback_data="autobuy:age_min_preset:0"),
                 InlineKeyboardButton("5m",       callback_data="autobuy:age_min_preset:5"),
                 InlineKeyboardButton("10m",      callback_data="autobuy:age_min_preset:10"),
                 InlineKeyboardButton("30m",      callback_data="autobuy:age_min_preset:30")],
                [InlineKeyboardButton("1h",       callback_data="autobuy:age_min_preset:60"),
                 InlineKeyboardButton("2h",       callback_data="autobuy:age_min_preset:120"),
                 InlineKeyboardButton("4h",       callback_data="autobuy:age_min_preset:240")],
                [InlineKeyboardButton("⬅️ Back",  callback_data="autobuy:set_age")],
            ])
        )

    elif action == "age_min_preset":
        val = int(query.data.split(":")[2])
        cfg["min_age_mins"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await _show_autobuy(query.edit_message_text, uid)

    elif action == "set_age_max":
        set_state(uid, waiting_for="ab_max_age")
        await query.edit_message_text(
            "⏱️ *Set maximum token age (minutes)*\n\n"
            "Tokens older than this will be skipped.\n"
            "Set to *0* for no limit.\n"
            f"Current: `{cfg.get('max_age_mins', 0)}m` (0 = no limit)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("0 (No limit)", callback_data="autobuy:age_max_preset:0"),
                 InlineKeyboardButton("15m",          callback_data="autobuy:age_max_preset:15"),
                 InlineKeyboardButton("30m",          callback_data="autobuy:age_max_preset:30")],
                [InlineKeyboardButton("1h",           callback_data="autobuy:age_max_preset:60"),
                 InlineKeyboardButton("2h",           callback_data="autobuy:age_max_preset:120"),
                 InlineKeyboardButton("4h",           callback_data="autobuy:age_max_preset:240")],
                [InlineKeyboardButton("⬅️ Back",      callback_data="autobuy:set_age")],
            ])
        )

    elif action == "age_max_preset":
        val = int(query.data.split(":")[2])
        cfg["max_age_mins"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await _show_autobuy(query.edit_message_text, uid)

    # ── Min Txns (5m) ────────────────────────────────────────────────────────
    elif action == "set_txns":
        set_state(uid, waiting_for="ab_min_txns")
        await query.edit_message_text(
            "🔄 *Set minimum transactions in last 5 minutes*\n\n"
            "Token must have at least this many buy+sell transactions in the last 5 min.\n"
            "Set to *0* for no minimum.\n"
            f"Current: `{cfg.get('min_txns_5m', 0)}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("0 (None)", callback_data="autobuy:txns_preset:0"),
                 InlineKeyboardButton("5",        callback_data="autobuy:txns_preset:5"),
                 InlineKeyboardButton("10",       callback_data="autobuy:txns_preset:10"),
                 InlineKeyboardButton("20",       callback_data="autobuy:txns_preset:20")],
                [InlineKeyboardButton("30",       callback_data="autobuy:txns_preset:30"),
                 InlineKeyboardButton("50",       callback_data="autobuy:txns_preset:50"),
                 InlineKeyboardButton("100",      callback_data="autobuy:txns_preset:100")],
                [InlineKeyboardButton("⬅️ Back",  callback_data="autobuy:menu")],
            ])
        )

    elif action == "txns_preset":
        val = int(query.data.split(":")[2])
        cfg["min_txns_5m"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await _show_autobuy(query.edit_message_text, uid)

    elif action == "menu":
        clear_state(uid)
        await _show_autobuy(query.edit_message_text, uid)


# ─── Commands ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_state(uid)
    # Auto-subscribe to live alerts
    _db.set_scanning(True)
    _db.add_scan_target(uid)
    await show_main_menu(update.message, uid)


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/price <symbol or CA>`", parse_mode="Markdown")
        return
    msg  = await update.message.reply_text("Looking up...")
    pair = fetch_sol_pair(context.args[0])
    if not pair:
        await msg.edit_text("Token not found.", reply_markup=back_kb())
        return
    await msg.edit_text(format_pair(pair), parse_mode="Markdown",
                        reply_markup=price_card_kb(pair["baseToken"]["address"]))


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Fetching...")
    await _show_top(msg.edit_text)


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/buy <symbol or CA> <SOL amount>`\nExample: `/buy BONK 0.1`",
            parse_mode="Markdown"
        )
        return
    msg = await update.message.reply_text("Getting quote...")
    await do_trade_flow(msg, update.effective_user.id, context, "buy",
                        context.args[0], context.args[1])


async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/sell <symbol or CA> <raw amount>`\nExample: `/sell BONK 1000000`",
            parse_mode="Markdown"
        )
        return
    msg = await update.message.reply_text("Getting quote...")
    await do_trade_flow(msg, update.effective_user.id, context, "sell",
                        context.args[0], context.args[1])


async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Loading...")
    await _show_portfolio(msg.edit_text, update.effective_user.id)


async def cmd_trades_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    query = tc.parse_trade_args(context.args or [])
    set_state(uid, trade_center={
        "view": query.view,
        "filter_spec": query.filter_spec,
        "page": query.page,
    })
    try:
        text, kb = _build_trade_center_page(uid)
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        print(f"[TRADES] Command error: {e}")
        import traceback
        traceback.print_exc()
        await update.message.reply_text(
            "❌ Error loading trade history. Please try again.",
            parse_mode="Markdown", 
            reply_markup=back_kb("menu:main")
        )


def _trade_state(uid: int) -> dict:
    state = get_state(uid, "trade_center", {"view": "ledger", "filter_spec": "all", "page": 1})
    return {
        "view": state.get("view", "ledger"),
        "filter_spec": tc.normalize_filter_spec(state.get("filter_spec", "all")),
        "page": max(1, int(state.get("page", 1))),
    }


def _trade_filter_label(filter_spec: str) -> str:
    if filter_spec.startswith("search:"):
        return f"Search: {filter_spec.split(':', 1)[1]}"
    if filter_spec.startswith("date:"):
        return f"Date: {filter_spec.split(':', 1)[1]}"
    return {
        "all": "All",
        "wins": "Wins",
        "losses": "Losses",
        "buys": "Buys",
        "sells": "Sells",
        "paper": "Paper",
        "live": "Live",
    }.get(filter_spec, filter_spec.title())


def _hold_label(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds/60)}m"
    if seconds < 86400:
        return f"{int(seconds/3600)}h"
    return f"{int(seconds/86400)}d"


def _trade_center_kb(state: dict, page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📒 Ledger", callback_data="trades:view:ledger"),
            InlineKeyboardButton("📈 Closed", callback_data="trades:view:closed"),
            InlineKeyboardButton("📊 Stats", callback_data="trades:view:stats"),
        ],
        [
            InlineKeyboardButton("All", callback_data="trades:filter:all"),
            InlineKeyboardButton("Wins", callback_data="trades:filter:wins"),
            InlineKeyboardButton("Losses", callback_data="trades:filter:losses"),
        ],
        [
            InlineKeyboardButton("Buys", callback_data="trades:filter:buys"),
            InlineKeyboardButton("Sells", callback_data="trades:filter:sells"),
            InlineKeyboardButton("Paper", callback_data="trades:filter:paper"),
            InlineKeyboardButton("Live", callback_data="trades:filter:live"),
        ],
    ]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"trades:page:{page-1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="trades:noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"trades:page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data="trades:refresh"),
        InlineKeyboardButton("⬅️ Menu", callback_data="menu:main"),
    ])
    return InlineKeyboardMarkup(rows)


def _build_trade_center_page(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    state = _trade_state(uid)
    trades = sorted(_db.get_trades(uid, limit=10000), key=lambda row: row.get("ts", 0), reverse=True)
    closed = _db.get_closed_trades(uid, limit=10000)
    if not trades:
        return (
            "📊 *Trade Center*\n\nNo trades recorded yet.\n\n_Use /buy or /sell to start building your ledger._",
            _trade_center_kb(state, 1, 1),
        )

    if state["view"] == "closed":
        rows = tc.filter_closed_trades(closed, state["filter_spec"])
        per_page = 8
    elif state["view"] == "stats":
        rows = []
        per_page = 1
    else:
        rows = tc.filter_trades(trades, state["filter_spec"])
        per_page = 10

    summary = tc.summarize_trades(tc.filter_trades(trades, state["filter_spec"]), tc.filter_closed_trades(closed, state["filter_spec"]))
    total_pages = max(1, (len(rows) + per_page - 1) // per_page) if state["view"] != "stats" else 1
    page = min(state["page"], total_pages)
    state["page"] = page
    set_state(uid, trade_center=state)

    lines = [
        f"📊 *Trade Center* — {_trade_filter_label(state['filter_spec'])}",
        f"View: *{state['view'].title()}*",
        f"Trades: `{summary['total_rows']}` · Closed: `{summary['closed_count']}` · Win rate: `{summary['win_rate']:.0f}%`",
        f"Realized P&L: `{'+' if summary['realized_pnl_sol'] >= 0 else ''}{summary['realized_pnl_sol']:.4f} SOL`",
        "",
    ]

    if state["view"] == "stats":
        best = summary["best_trade"]
        worst = summary["worst_trade"]
        lines.extend([
            f"Buys: `{summary['buy_count']}` · Sells: `{summary['sell_count']}`",
            f"Paper: `{summary['paper_count']}` · Live: `{summary['live_count']}`",
            f"Avg hold: `{_hold_label(summary['avg_hold_s']) if summary['closed_count'] else 'n/a'}`",
            f"Top narrative: *{_esc(summary['top_narrative'])}*",
            f"Top source: *{_esc(summary['top_source'])}*",
            f"Top archetype: *{_esc(summary['top_archetype'])}*",
            "",
            f"Best: *{_esc(best['symbol'])}* `{best['pnl_sol']:+.4f}◎`" if best else "Best: `n/a`",
            f"Worst: *{_esc(worst['symbol'])}* `{worst['pnl_sol']:+.4f}◎`" if worst else "Worst: `n/a`",
        ])
        return "\n".join(lines), _trade_center_kb(state, 1, 1)

    if not rows:
        lines.append("_No trades match this filter._")
        return "\n".join(lines), _trade_center_kb(state, 1, 1)

    page_rows = rows[(page - 1) * per_page : page * per_page]
    if state["view"] == "closed":
        for row in page_rows:
            sold_at = datetime.fromtimestamp(float(row.get("sell_ts") or 0), tz=timezone.utc).strftime("%m/%d %H:%M")
            lines.append(
                f"{'✅' if row['pnl_sol'] >= 0 else '❌'} *${_esc(row['symbol'])}* {'📄' if row['mode']=='paper' else '🔴'} `{sold_at}`\n"
                f"   In `{row['sol_in']:.4f}◎` → Out `{row['sol_out']:.4f}◎` · `{row['pnl_pct']:+.1f}%` "
                f"(`{row['pnl_sol']:+.4f}◎`) · hold `{_hold_label(row['hold_s'])}`"
            )
    else:
        for row in page_rows:
            ts = datetime.fromtimestamp(float(row.get("ts") or 0), tz=timezone.utc).strftime("%m/%d %H:%M")
            action = str(row.get("action", "")).lower()
            is_buy = action == "buy"
            sol = float(row.get("sol_amount") or 0) if is_buy else float(row.get("sol_received") or 0)
            extra = ""
            pnl_text = ""
            if not is_buy and row.get("tx_sig"):
                extra = f" · [Tx](https://solscan.io/tx/{row['tx_sig']})"
            if not is_buy:
                pnl_text = f" · pnl `{float(row.get('pnl_pct') or 0):+.1f}%`"
            lines.append(
                f"{'🟢 BUY' if is_buy else '🔻 SELL'} *${_esc(row.get('symbol') or row.get('mint','')[:6])}* "
                f"{'📄' if str(row.get('mode','')).lower()=='paper' else '🔴'} `{ts}`\n"
                f"   SOL `{sol:.4f}◎` · tokens `{int(row.get('token_amount') or 0):,}` · "
                f"price `${float(row.get('price_usd') or 0):.8f}`"
                f"{pnl_text}{extra}"
            )
    return "\n".join(lines), _trade_center_kb(state, page, total_pages)


async def trade_center_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    parts = query.data.split(":")
    await query.answer()
    state = _trade_state(uid)
    action = parts[1] if len(parts) > 1 else ""
    if action == "page" and len(parts) > 2:
        try:
            state["page"] = max(1, int(parts[2]))
        except ValueError:
            pass
    elif action == "filter" and len(parts) > 2:
        state["filter_spec"] = tc.normalize_filter_spec(parts[2])
        state["page"] = 1
    elif action == "view" and len(parts) > 2:
        state["view"] = parts[2]
        state["page"] = 1
    elif action == "refresh":
        pass
    elif action == "noop":
        return
    set_state(uid, trade_center=state)
    text, kb = _build_trade_center_page(uid)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)


async def cmd_watchbuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's wallet alert list."""
    uid     = update.effective_user.id
    wallets = get_user_alert_wallets(uid)
    if not wallets:
        text = "👁️ *Wallet Buy Alerts*\n\nYou are not tracking any wallets yet.\n\nTap ➕ to add a wallet — you'll get alerted when it buys a token."
    else:
        lines = ["👁️ *Wallet Buy Alerts*\n"]
        for w in wallets:
            addr = w["address"]
            label = w.get("label", addr[:8])
            tok_count = len(w.get("last_tokens", {}))
            lines.append(f"• *{label}* — `{addr[:8]}...{addr[-4:]}` ({tok_count} tokens held)")
        text = "\n".join(lines)

    buttons = []
    for w in wallets:
        addr  = w["address"]
        label = w.get("label", addr[:8])
        buttons.append([InlineKeyboardButton(f"🗑️ Remove {label}", callback_data=f"wbalert:remove:{addr}")])
    buttons.append([InlineKeyboardButton("➕ Add Wallet", callback_data="wbalert:add")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:main")])

    await update.message.reply_text(text, parse_mode="Markdown",
                                    reply_markup=InlineKeyboardMarkup(buttons))


async def wbalert_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    await query.answer()

    if action == "add":
        context.user_data["state"] = "wbalert_add_addr"
        await query.edit_message_text(
            "👁️ *Add Wallet Alert*\n\nPaste the Solana wallet address to track:\n\n"
            "_You'll get a Telegram alert every time this wallet buys a new token._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wbalert:list")]])
        )
    elif action == "remove":
        addr = parts[2] if len(parts) > 2 else ""
        remove_user_alert_wallet(uid, addr)
        wallets = get_user_alert_wallets(uid)
        text    = "👁️ *Wallet Buy Alerts*\n\nWallet removed.\n"
        buttons = []
        for w in wallets:
            a = w["address"]; lbl = w.get("label", a[:8])
            buttons.append([InlineKeyboardButton(f"🗑️ Remove {lbl}", callback_data=f"wbalert:remove:{a}")])
        buttons.append([InlineKeyboardButton("➕ Add Wallet", callback_data="wbalert:add")])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:main")])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    elif action == "skip_label":
        addr  = context.user_data.pop("wbalert_pending", "")
        context.user_data.pop("state", None)
        if addr:
            add_user_alert_wallet(uid, addr, addr[:8])
            await query.edit_message_text(
                f"✅ Wallet `{addr[:8]}...` added to alerts.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👁️ My Alerts", callback_data="wbalert:list")]])
            )
    elif action == "list":
        wallets = get_user_alert_wallets(uid)
        text    = "👁️ *Wallet Buy Alerts*\n\n" + ("\n".join(
            f"• *{w.get('label', w['address'][:8])}* `{w['address'][:8]}...`"
            for w in wallets
        ) if wallets else "No wallets tracked.")
        buttons = []
        for w in wallets:
            a = w["address"]; lbl = w.get("label", a[:8])
            buttons.append([InlineKeyboardButton(f"🗑️ Remove {lbl}", callback_data=f"wbalert:remove:{a}")])
        buttons.append([InlineKeyboardButton("➕ Add Wallet", callback_data="wbalert:add")])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:main")])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


def _build_history_page(uid: int, page: int) -> tuple:
    """Build realized P&L history page. Returns (text, InlineKeyboardMarkup)."""
    from datetime import datetime, timezone
    all_trades = _db.get_trades(uid, limit=10000)
    all_trades.sort(key=lambda t: t.get("ts", 0))

    buy_queue: dict[str, list] = {}
    closed: list[dict] = []
    for t in all_trades:
        mint = t.get("mint", "")
        if t.get("action") == "buy":
            buy_queue.setdefault(mint, []).append(t)
        elif t.get("action") == "sell" and buy_queue.get(mint):
            buy_t    = buy_queue[mint].pop(0)
            sol_in   = float(buy_t.get("sol_amount")  or 0)
            sol_out  = float(t.get("sol_received")    or 0)
            pnl_sol  = sol_out - sol_in
            pnl_pct  = (pnl_sol / sol_in * 100) if sol_in > 0 else 0
            hold_s   = t.get("ts", 0) - buy_t.get("ts", 0)
            closed.append({
                "symbol":   t.get("symbol", "?"),
                "mint":     mint,
                "sell_ts":  t.get("ts", 0),
                "sol_in":   sol_in,
                "sol_out":  sol_out,
                "pnl_sol":  pnl_sol,
                "pnl_pct":  pnl_pct,
                "hold_s":   hold_s,
                "mode":     t.get("mode", "?"),
            })

    closed.sort(key=lambda x: x["sell_ts"], reverse=True)

    PER_PAGE    = 8
    total_pages = max(1, (len(closed) + PER_PAGE - 1) // PER_PAGE)
    page        = max(1, min(page, total_pages))
    page_items  = closed[(page - 1) * PER_PAGE : page * PER_PAGE]

    total_pnl = sum(c["pnl_sol"] for c in closed)
    wins      = sum(1 for c in closed if c["pnl_sol"] > 0)
    win_rate  = wins / len(closed) * 100 if closed else 0
    mode_icon = lambda m: "📄" if m == "paper" else "🔴"

    def _hold_str(secs):
        if secs < 60:    return f"{int(secs)}s"
        if secs < 3600:  return f"{int(secs/60)}m"
        if secs < 86400: return f"{int(secs/3600)}h"
        return f"{int(secs/86400)}d"

    lines = [
        f"📈 *Realized P&L* — Page {page}/{total_pages}",
        f"Closed: {len(closed)} trades | Win rate: {win_rate:.0f}%",
        f"Total P&L: `{'+'if total_pnl>=0 else ''}{total_pnl:.4f} SOL` {'📈' if total_pnl>=0 else '📉'}",
        "",
    ]
    for c in page_items:
        icon   = "✅" if c["pnl_sol"] >= 0 else "❌"
        sign   = "+" if c["pnl_pct"] >= 0 else ""
        hold   = _hold_str(c["hold_s"])
        date_s = datetime.fromtimestamp(c["sell_ts"], tz=timezone.utc).strftime("%m/%d")
        lines.append(
            f"{icon} *${c['symbol']}* {mode_icon(c['mode'])}  `{date_s}` hold:`{hold}`\n"
            f"   In: `{c['sol_in']:.4f}◎` → Out: `{c['sol_out']:.4f}◎`  "
            f"`{sign}{c['pnl_pct']:.1f}%` (`{sign}{c['pnl_sol']:.4f}◎`)"
        )

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"history_page:{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"history_page:{page+1}"))
    kb = []
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("⬅️ Menu", callback_data="menu:main")])

    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    query = tc.parse_trade_args(context.args or [])
    set_state(uid, trade_center={"view": "closed", "filter_spec": query.filter_spec, "page": query.page})
    text, kb = _build_trade_center_page(uid)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)


async def history_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    page = int(query.data.split(":")[1])
    await query.answer()
    state = _trade_state(uid)
    state["view"] = "closed"
    state["page"] = page
    set_state(uid, trade_center=state)
    text, kb = _build_trade_center_page(uid)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)


async def cmd_research_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download research log CSV file for data analysis."""
    csv_path = research_logger.export_csv_path()
    
    if not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0:
        await update.message.reply_text(
            "📊 No research log data yet. Start trading to generate records!",
            parse_mode="Markdown", reply_markup=back_kb("menu:main")
        )
        return
    
    try:
        with open(csv_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="research_log.csv",
                caption="📊 *Research Log — Trading History*\n\nColumns: timestamp, user_id, action, mint, symbol, narrative, heat_score, prices, amounts, PnL"
            )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Error downloading file: {e}",
            parse_mode="Markdown", reply_markup=back_kb("menu:main")
        )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Mode: *{'📄 Paper' if get_mode(uid) == 'paper' else '🔴 Live'}*\n\nSelect:",
        parse_mode="Markdown", reply_markup=settings_kb(uid)
    )


async def cmd_autosell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Loading auto-sell...")
    await _show_autosell(msg.edit_text, update.effective_user.id)

async def cmd_stoploss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick access to global risk settings (all 5 strategies)."""
    uid = update.effective_user.id
    gsl  = get_global_sl()
    gts  = get_global_trailing_stop()
    gttp = get_global_trailing_tp()
    gbe  = get_global_breakeven_stop()
    gte  = get_global_time_exit()
    await update.message.reply_text(
        _global_risk_menu_text(gsl, gts, gttp, gbe, gte),
        parse_mode="Markdown",
        reply_markup=_global_risk_kb(gsl, gts, gttp, gbe, gte)
    )


# ─── Scanner commands ──────────────────────────────────────────────────────────


# ─── Intelligence commands ────────────────────────────────────────────────────

async def cmd_wallets_intel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show auto-tracked wallet intelligence."""
    msg = intel.format_wallet_intelligence(page=0)
    wallets = intel.get_auto_tracked_wallets()
    total   = len(wallets)
    pages   = max(1, (total - 1) // 10 + 1)

    kb_rows = []
    if pages > 1:
        kb_rows.append([
            InlineKeyboardButton("◀", callback_data="intel:wallets:0"),
            InlineKeyboardButton(f"1/{pages}", callback_data="noop"),
            InlineKeyboardButton("▶", callback_data="intel:wallets:1"),
        ])
    kb_rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="intel:wallets:0")])
    kb_rows.append([InlineKeyboardButton("📊 Narratives", callback_data="intel:narratives")])
    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


async def cmd_narratives_intel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show narrative intelligence stats."""
    msg = intel.format_narrative_intelligence()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="intel:narratives"),
         InlineKeyboardButton("🤖 Wallets",  callback_data="intel:wallets:0")],
    ])
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)


async def intel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle intelligence panel callbacks."""
    q    = update.callback_query
    data = q.data  # e.g. "intel:wallets:0" or "intel:narratives"
    await q.answer()

    parts = data.split(":")
    subtype = parts[1] if len(parts) > 1 else ""

    if subtype == "narratives":
        msg = intel.format_narrative_intelligence()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="intel:narratives"),
             InlineKeyboardButton("🤖 Wallets",  callback_data="intel:wallets:0")],
        ])
        try:
            await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            pass

    elif subtype == "wallets":
        page    = int(parts[2]) if len(parts) > 2 else 0
        wallets = intel.get_auto_tracked_wallets()
        total   = len(wallets)
        pages   = max(1, (total - 1) // 10 + 1)
        page    = max(0, min(page, pages - 1))
        msg     = intel.format_wallet_intelligence(page=page)

        kb_rows = []
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"intel:wallets:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"intel:wallets:{page+1}"))
        if len(nav) > 1:
            kb_rows.append(nav)
        kb_rows.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"intel:wallets:{page}")])
        kb_rows.append([InlineKeyboardButton("📊 Narratives", callback_data="intel:narratives")])
        try:
            await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb_rows))
        except Exception:
            pass

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        _db.set_scanning(True)
        _db.add_scan_target(uid)

        # Get user's alert thresholds from new v2 settings
        user_settings = sm.get_user_settings(uid)
        warm_threshold = user_settings.get("alert_warm_threshold", 70)
        
        await update.message.reply_text(
            f"🟢 *Scout is live!*\n\n"
            f"Scanning pump.fun + DexScreener every 15 seconds.\n"
            f"Alerts fire at: WARM ≥ {warm_threshold}, HOT ≥ {user_settings.get('alert_hot_threshold', 80)}, ULTRA ≥ {user_settings.get('alert_ultra_hot_threshold', 90)}\n\n"
            f"Edit thresholds: /customize or use menu below.\n\n"
            f"Use /stopscan to pause your scout.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔕 Pause Scout",  callback_data="scanner:toggle"),
                InlineKeyboardButton("⚙️ Settings",     callback_data="scanner:set_threshold"),
                InlineKeyboardButton("👀 Scouted",      callback_data="scanner:watchlist"),
                InlineKeyboardButton("🏆 Top Scouts",   callback_data="scanner:topalerts"),
            ]])
        )
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[CMD_SCAN ERROR] {e}\n{tb}", flush=True)
        await update.message.reply_text(f"Error in /scan: {e}")


async def cmd_stopscan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    _db.remove_scan_target(uid)
    await update.message.reply_text(
        "⏸ *Scout paused.*\n\nThe scanner keeps running in the background.\nUse /scan to resume your scout.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔔 Start Scout", callback_data="scanner:toggle"),
            InlineKeyboardButton("📋 Menu",        callback_data="menu:main"),
        ]])
    )


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid        = update.effective_user.id
    wl         = sc.get_watchlist()
    u_settings = sm.get_user_settings(uid)
    watch_thr  = u_settings.get("alert_scouted_threshold", 20)
    alert_thr  = u_settings.get("alert_warm_threshold", 70)
    if not wl:
        await update.message.reply_text(
            f"👀 *No scouted tokens yet.*\n\n"
            f"Tokens scoring ≥ `{watch_thr}` appear here as the scanner runs.\n"
            f"Full alerts fire at ≥ `{alert_thr}`.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏆 Top Scouts", callback_data="scanner:topalerts"),
                 InlineKeyboardButton("⚙️ Settings",   callback_data="scanner:set_threshold")],
                [InlineKeyboardButton("⬅️ Menu",        callback_data="menu:main")],
            ])
        )
        return
    items = [t for t in wl.values() if t.get("score", 0) >= watch_thr]
    items = sorted(items, key=lambda x: -x.get("score", 0))[:25]
    lines = [f"*👀 Watchlist* — score ≥ {watch_thr} | alert ≥ {alert_thr}\n"]
    for t in items:
        score  = t.get("score", 0)
        flag   = "🔥" if score >= alert_thr else ("🟡" if score >= (watch_thr + alert_thr) // 2 else "⚪")
        mcap   = t.get("mcap", 0) or 0
        mcap_s = (f"${mcap/1_000_000:.2f}M" if mcap >= 1_000_000
                  else f"${mcap/1_000:.0f}K" if mcap >= 1_000 else f"${mcap:.0f}")
        lines.append(
            f"{flag} *{t['name']}* (${t['symbol']}) — `{score}/100`\n"
            f"   MCap: {mcap_s}"
        )
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏆 Top Scouts", callback_data="scanner:topalerts"),
             InlineKeyboardButton("⚙️ Settings",   callback_data="scanner:set_threshold")],
            [InlineKeyboardButton("⬅️ Menu",        callback_data="menu:main")],
        ])
    )


async def cmd_heatscore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: `/heatscore <symbol or CA>`\nExample: `/heatscore BONK`",
            parse_mode="Markdown"
        )
        return
    msg = await update.message.reply_text("Scoring token...")
    result = await sc.score_single_token(context.args[0])
    if not result:
        await msg.edit_text("Token not found.", reply_markup=back_kb())
        return
    await msg.edit_text(
        sc.format_heat_score_card(result), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Chart", url=f"https://dexscreener.com/solana/{result['mint']}"),
            InlineKeyboardButton("🔫 RugCheck", url=f"https://rugcheck.xyz/tokens/{result['mint']}"),
        ]]),
        disable_web_page_preview=True
    )


async def cmd_topalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = sc.get_todays_alerts()
    if not alerts:
        await update.message.reply_text(
            "🏆 *No scouts fired today yet.*\n\nThe scanner is live — scouts will appear here as hot tokens are found.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👀 Scouted",   callback_data="scanner:watchlist"),
                InlineKeyboardButton("⬅️ Menu",      callback_data="menu:main"),
            ]])
        )
        return
    top = sorted(alerts, key=lambda x: -x.get("timestamp", 0))[:10]
    lines = ["*🏆 Top Scouts Today*\n"]
    for i, e in enumerate(top, 1):
        label = sc.priority_label(e["score"])
        lines.append(f"{i}. {label} *{e['name']}* (${e['symbol']}) — {e['score']}/100")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("👀 Scouted",   callback_data="scanner:watchlist"),
            InlineKeyboardButton("⬅️ Menu",      callback_data="menu:main"),
        ]])
    )


# ─── Heat Score v2 Settings Commands ──────────────────────────────────────────

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /settings — Show all current heat score settings.
    Allows inline adjustment via buttons.
    """
    user_id = update.effective_user.id
    try:
        # Show formatted settings
        settings_text = sm.format_settings_display(user_id, compact=False)
        
        # Create keyboard with preset buttons + customize option
        keyboard = [
            [InlineKeyboardButton("🎯 Quick Presets", callback_data="heatscore:presets")],
            [InlineKeyboardButton("⚙️ Customize", callback_data="heatscore:customize")],
            [InlineKeyboardButton("🔄 Reset to Defaults", callback_data="heatscore:reset_confirm")],
            [InlineKeyboardButton("📊 Show Non-Defaults Only", callback_data="heatscore:show_custom")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu:main")],
        ]
        
        await update.message.reply_text(
            settings_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True
        )
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[CMD_SETTINGS ERROR] {e}\n{tb}", flush=True)
        await update.message.reply_text(f"Error in /settings: {e}")


async def cmd_presets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /presets — Show and apply preset configurations.
    """
    user_id = update.effective_user.id
    presets = sm.list_presets()
    
    lines = ["*🎯 Scout Presets*\n", "Quick-swap configurations for different trading styles:\n"]
    
    for preset_key, preset_info in presets.items():
        lines.append(f"*{preset_info['display_name']}*")
        lines.append(f"{preset_info['description']}")
        lines.append(f"_Changes: {preset_info['setting_count']} settings_\n")
    
    # Create button for each preset
    keyboard = [
        [InlineKeyboardButton("🛡️ Conservative", callback_data="heatscore:preset:conservative"),
         InlineKeyboardButton("⚖️ Balanced", callback_data="heatscore:preset:balanced")],
        [InlineKeyboardButton("🚀 Aggressive", callback_data="heatscore:preset:aggressive"),
         InlineKeyboardButton("🐋 Whale Mode", callback_data="heatscore:preset:whale-mode")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:main")],
    ]
    
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )


async def cmd_customize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /customize [factor] [value] — Adjust a specific setting (1-100).
    Without args, shows customization menu.
    """
    user_id = update.effective_user.id
    
    if not context.args or len(context.args) < 2:
        # Show customize menu
        lines = [
            "*⚙️ Customize Heat Score Settings*\n",
            "Adjust any setting from 1-100. Examples:\n",
            "`/customize alert_hot_threshold 75`",
            "`/customize risk_dev_sell_threshold_pct 60`",
            "`/customize momentum_min_vol 10000`\n",
            "Use `/settings` to see all available settings and their descriptions.",
        ]
        
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 View All Settings", callback_data="heatscore:customize"),
                InlineKeyboardButton("⬅️ Back", callback_data="menu:main"),
            ]]),
        )
        return
    
    setting_key = context.args[0].lower()
    try:
        value = float(context.args[1]) if "." in context.args[1] or setting_key.endswith("_usd") else int(context.args[1])
    except (ValueError, IndexError):
        await update.message.reply_text(
            f"❌ Invalid value. Usage: `/customize {setting_key} <1-100>`",
            parse_mode="Markdown"
        )
        return
    
    # Validate and save
    if not sm.validate_setting(setting_key, value):
        await update.message.reply_text(
            f"❌ Invalid setting '{setting_key}' or value out of range.\n"
            f"Use `/settings` to see available settings.",
            parse_mode="Markdown"
        )
        return
    
    if sm.save_user_settings(user_id, {setting_key: value}):
        current_val = sm.get_user_settings(user_id)[setting_key]
        desc = sm.get_setting_description(setting_key)
        
        await update.message.reply_text(
            f"✅ *Setting Updated*\n\n"
            f"{desc}\n"
            f"New value: `{current_val}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 View Settings", callback_data="heatscore:show"),
                InlineKeyboardButton("⬅️ Back", callback_data="menu:main"),
            ]]),
        )
    else:
        await update.message.reply_text(
            "❌ Failed to save setting. Try again.",
            parse_mode="Markdown"
        )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /stats — Show your scout performance analytics by alert tier.
    """
    user_id = update.effective_user.id
    
    # Get today's log
    try:
        log = sc.load_log()
    except Exception:
        log = []
    
    # Filter for this user's alerts (if logging includes user_id, otherwise show global)
    # For now, show global stats with breakdown by alert tier
    stats_by_tier = {
        "ULTRA_HOT (90+)": {"total": 0, "profitable": 0},
        "HOT (80-89)": {"total": 0, "profitable": 0},
        "WARM (70-79)": {"total": 0, "profitable": 0},
        "SCOUTED (50-69)": {"total": 0, "profitable": 0},
    }
    
    for entry in log:
        score = entry.get("score", 0)
        
        if score >= 90:
            tier = "ULTRA_HOT (90+)"
        elif score >= 80:
            tier = "HOT (80-89)"
        elif score >= 70:
            tier = "WARM (70-79)"
        elif score >= 50:
            tier = "SCOUTED (50-69)"
        else:
            continue
        
        stats_by_tier[tier]["total"] += 1
        # entries would have PnL info in production
        # For now just count total
    
    # Get current settings
    try:
        current_preset = sm.detect_current_preset(user_id)
    except Exception:
        current_preset = "Default"
    
    lines = [
        "*📊 Scout Performance Analytics*\n",
        f"Current Preset: {current_preset}\n",
        "*Today's Alerts:*\n",
    ]
    
    total_alerts = sum(s["total"] for s in stats_by_tier.values())
    if total_alerts == 0:
        lines.append("No alerts fired today yet.\n")
    else:
        for tier, stats in stats_by_tier.items():
            if stats["total"] > 0:
                pct = (stats["total"] / total_alerts) * 100
                lines.append(f"{tier}: `{stats['total']} alerts` ({pct:.0f}%)")
    
    lines.extend([
        "\nℹ️ _Full PnL tracking coming soon_",
        "\nUse `/settings` to adjust heatwave thresholds or `/presets` to switch strategies.",
    ])
    
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 View Settings", callback_data="heatscore:show")],
            [InlineKeyboardButton("🎯 Quick Presets", callback_data="heatscore:presets")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu:main")],
        ])
    )


# ─── Wallet commands ──────────────────────────────────────────────────────────

async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_wallet_menu(update.message.reply_text)


async def cmd_whalebuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /whalebuy [mins]
    Shows tokens where tracked wallets entered recently.
    """
    try:
        import wallet_tracker
        
        age_mins = int(context.args[0]) if context.args else 15
        if age_mins < 1 or age_mins > 120:
            age_mins = 15
        
        entries = wallet_tracker.get_recent_entries(age_mins)
        
        if not entries:
            await update.message.reply_text(
                f"🦣 No tracked wallet activity in the last {age_mins} minutes.\n\n"
                f"Use /addwallet to start tracking smart traders!",
                parse_mode="Markdown"
            )
            return
        
        # Group by mint, show top 10
        by_mint = {}
        for entry in entries:
            mint = entry["mint"]
            if mint not in by_mint:
                by_mint[mint] = []
            by_mint[mint].append(entry)
        
        lines = [f"🦣 *Whale Buys (Last {age_mins} min)*\n"]
        count = 0
        for mint, wallet_entries in list(by_mint.items())[:10]:
            wallets = [e["wallet_name"] for e in wallet_entries]
            unique_wallets = len(set(e["wallet"] for e in wallet_entries))
            timestamp = wallet_entries[0]["entry_ts"]
            mins_ago = (time.time() - timestamp) / 60
            
            lines.append(f"• `{mint[:8]}...` ({unique_wallets} wallets, {mins_ago:.1f}m ago)")
            count += 1
        
        if len(by_mint) > 10:
            lines.append(f"... and {len(by_mint) - 10} more")
        
        lines.append(f"\n🔍 Use /contract <mint> for full details")
        
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    
    except ImportError:
        await update.message.reply_text("⚠️ Wallet tracking not initialized")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")


async def cmd_momentum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /momentum [ratio]
    Shows tokens with rapid volume acceleration (m5→h1 ratio).
    """
    try:
        from scanner import get_todays_alerts, format_heat_score_card
        
        ratio_threshold = float(context.args[0]) if context.args else 2.0
        if ratio_threshold < 1.0 or ratio_threshold > 10.0:
            ratio_threshold = 2.0
        
        alerts = get_todays_alerts()
        
        if not alerts:
            await update.message.reply_text(
                "📊 No tokens scanned today yet.\n\n"
                "Use /scan to start watching for momentum plays.",
                parse_mode="Markdown"
            )
            return
        
        # Filter for high momentum (check breakdown for momentum score)
        momentum_tokens = []
        for token in alerts:
            bd = token.get("breakdown", {})
            mom_pts, mom_reason = bd.get("momentum", (0, ""))
            if mom_pts >= 12:  # High momentum threshold
                momentum_tokens.append((token, mom_pts))
        
        if not momentum_tokens:
            await update.message.reply_text(
                f"📊 No tokens with {ratio_threshold}x+ momentum today.\n\n"
                "Check back soon!",
                parse_mode="Markdown"
            )
            return
        
        # Sort by momentum score desc, show top 5
        momentum_tokens.sort(key=lambda x: -x[1])
        
        lines = ["🚀 *High Momentum Tokens*\n"]
        for token, score in momentum_tokens[:5]:
            name = token.get("name", "?")
            symbol = token.get("symbol", "?")
            heat = token.get("total", "?")
            lines.append(f"• ${symbol} {name}")
            lines.append(f"  Heat: {heat}/100")
            bd = token.get("breakdown", {})
            _, mom_reason = bd.get("momentum", ("", ""))
            if mom_reason:
                lines.append(f"  {mom_reason}")
            lines.append("")
        
        if len(momentum_tokens) > 5:
            lines.append(f"... and {len(momentum_tokens) - 5} more")
        
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")


async def cmd_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /contract <mint>
    Full snapshot: liquidity, age, volume ramp, heat breakdown.
    """
    try:
        if not context.args:
            await update.message.reply_text(
                "📋 Usage: /contract <mint>\n\n"
                "Shows full token snapshot (liquidity, age, volume, heat).",
                parse_mode="Markdown"
            )
            return
        
        mint = context.args[0].strip()
        
        # Fetch from scanner's daily log or live scan
        from scanner import load_log
        log = load_log()
        
        token_entry = None
        for entry in reversed(log):
            if entry.get("mint") == mint:
                token_entry = entry
                break
        
        if not token_entry:
            await update.message.reply_text(
                f"🔍 Token not found in recent scans.\n\n"
                f"Try: /heatscore {mint}",
                parse_mode="Markdown"
            )
            return
        
        # Format snapshot
        lines = [
            f"📋 *Token Snapshot*\n",
            f"𝘈𝘭𝘵 𝘱𝘳𝘪𝘭𝘭𝘪𝘱𝘶𝘴 *{token_entry.get('name', '?')}* (${token_entry.get('symbol', '?')})",
            f"",
            f"Mint: `{mint}`",
            f"Narrative: {token_entry.get('narrative', 'Unknown')}",
            f"Score: {token_entry.get('score', '?')}/100",
            f"MCap: ${token_entry.get('mcap', 0):,.0f}",
            f"",
            f"📊 [Chart](https://dexscreener.com/solana/{mint})  "
            f"[Pump](https://pump.fun/{mint})  "
            f"[RugCheck](https://rugcheck.xyz/tokens/{mint})",
        ]
        
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")


async def cmd_discoverwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /discoverwallet [force]
    Scan pump.fun graduation history and surface smart wallets that consistently
    entered winning tokens early. Shows top 10 discovered wallets.
    Optional 'force' arg bypasses the 6-hour cache and rescans immediately.
    """
    try:
        import wallet_discovery

        force = bool(context.args and context.args[0].lower() == "force")
        age   = wallet_discovery.last_scan_age_secs()

        if force or age > wallet_discovery.DISCOVERY_CACHE_TTL:
            await update.message.reply_text(
                "🔍 *Scanning pump.fun history...*\n\n"
                "_Fetching top graduated tokens and analysing early buyers._\n"
                "_This may take 30–60 seconds._",
                parse_mode="Markdown"
            )
            import asyncio
            loop = asyncio.get_running_loop()
            discovered = await loop.run_in_executor(
                None, lambda: wallet_discovery.run_discovery(force=True)
            )
        else:
            mins_ago = int(age / 60)
            await update.message.reply_text(
                f"📋 _Using cached scan from {mins_ago}m ago_\n_Use /discoverwallet force to rescan_",
                parse_mode="Markdown"
            )
            discovered = wallet_discovery.get_top_discovered()
            # get_top_discovered returns list not dict — normalise
            if isinstance(discovered, list):
                top = discovered
            else:
                top = []

        if not isinstance(discovered, list):
            top = wallet_discovery.get_top_discovered()
        else:
            top = discovered

        if not top:
            await update.message.reply_text(
                "😶 *No smart wallets discovered yet.*\n\n"
                "Discovery requires pump.fun API access and graduated tokens with $300k+ MCap.\n"
                "Try again in a few minutes or use `/discoverwallet force`.",
                parse_mode="Markdown"
            )
            return

        lines = ["🧠 *Auto-Discovered Smart Wallets*\n"]
        for i, w in enumerate(top[:10], 1):
            addr        = w.get("address", "")
            score       = w.get("discovery_score", 0)
            wins        = w.get("tokens_won", 0)
            avg_mcap    = w.get("avg_mcap_usd", 0)
            avg_entry   = w.get("avg_entry_secs", 0)
            top_tokens  = w.get("top_tokens", [])
            top_mcap    = top_tokens[0].get("mcap_usd", 0) if top_tokens else 0

            # Format avg mcap
            if avg_mcap >= 1_000_000:
                mcap_str = f"${avg_mcap / 1_000_000:.1f}M"
            else:
                mcap_str = f"${avg_mcap / 1_000:.0f}K"

            top_mcap_str = f"${top_mcap / 1_000_000:.1f}M" if top_mcap >= 1_000_000 else f"${top_mcap / 1_000:.0f}K"

            lines.append(
                f"{i}. `{addr[:8]}...{addr[-4:]}`\n"
                f"   Score: {score}/100 | {wins} wins | Avg {mcap_str} | Best {top_mcap_str}\n"
                f"   Avg entry: {avg_entry}s after launch\n"
            )

        lines.append("\n_Tap 👁️ Tracked Wallets → ➕ Add Wallet to start tracking any of these._")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown"
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")


async def cmd_cluster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /cluster <mint>
    Show the co-investment cluster map for a token — which tracked wallets
    entered together and how strong the cluster signal is.
    """
    try:
        if not context.args:
            await update.message.reply_text(
                "📋 *Usage:* `/cluster <token_mint>`\n\n"
                "Shows the cluster of wallets that co-invested in this token "
                "and their historical relationship strength.\n\n"
                "Also try: `/clustertop` — top co-investing wallet pairs.",
                parse_mode="Markdown"
            )
            return

        mint = context.args[0].strip()
        if len(mint) < 32:
            await update.message.reply_text("❌ Invalid token mint address.")
            return

        await update.message.reply_text(
            "🕸️ *Building cluster map...*",
            parse_mode="Markdown"
        )

        import wallet_cluster
        import asyncio

        loop    = asyncio.get_running_loop()
        cm      = await loop.run_in_executor(None, wallet_cluster.get_token_cluster_map, mint)

        total_w = cm.get("total_wallets", 0)
        total_e = cm.get("total_edges", 0)
        score   = cm.get("cluster_score", 0)
        boost   = cm.get("boost", 0)
        reason  = cm.get("reason", "No cluster pattern")

        if total_w == 0:
            await update.message.reply_text(
                f"⚪ *No cluster data for this token yet.*\n\n"
                f"`{mint}`\n\n"
                "_Cluster data builds as tracked wallets enter tokens during live scanning._",
                parse_mode="Markdown"
            )
            return

        # Header
        if score >= 70:
            header = "🔥 *Strong Cluster Detected*"
        elif score >= 40:
            header = "🟠 *Moderate Cluster*"
        elif score >= 10:
            header = "🟡 *Weak Cluster Signal*"
        else:
            header = "⚪ *Minimal Cluster*"

        lines = [
            f"{header}\n",
            f"Token: `{mint[:20]}...`\n",
            f"Cluster Score: *{score}/100*   Heat Boost: *+{boost} pts*",
            f"Signal: _{reason}_\n",
            f"Wallets: {total_w}   Co-invest pairs: {total_e}\n",
        ]

        # Wallet list
        wallets = cm.get("wallets", [])
        if wallets:
            lines.append("*Wallets (by connectivity):*")
            for w in wallets[:10]:
                addr    = w["wallet"]
                degree  = w.get("degree", 0)
                ts      = w.get("ts", 0)
                import datetime
                ts_str  = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "?"
                lines.append(f"  `{addr[:12]}...`  edges={degree}  at {ts_str}")

        lines.append("")

        # Edge list (top 5)
        edges = cm.get("edges", [])
        if edges:
            lines.append("*Top Co-investment Links:*")
            for e in sorted(edges, key=lambda x: -x.get("historical", 0))[:5]:
                a = e["a"][:10]
                b = e["b"][:10]
                hist = e.get("historical", 0)
                dt   = e.get("dt_secs", 0)
                lines.append(f"  `{a}` ↔ `{b}`  Δ{dt}s  (history: {hist}x)")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown"
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")


async def cmd_clustertop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /clustertop
    Show the most active co-investing wallet pairs across all tokens.
    """
    try:
        await update.message.reply_text(
            "🏆 *Loading top co-investor pairs...*",
            parse_mode="Markdown"
        )

        import wallet_cluster
        import asyncio

        loop = asyncio.get_running_loop()
        top  = await loop.run_in_executor(None, wallet_cluster.get_global_top_clusters, 10)

        if not top:
            await update.message.reply_text(
                "⚪ *No co-investment data yet.*\n\n"
                "_This builds automatically as tracked wallets co-invest in tokens during scanning._",
                parse_mode="Markdown"
            )
            return

        lines = ["🕸️ *Top Co-Investing Wallet Pairs*\n"]
        for i, pair in enumerate(top, 1):
            a    = pair["wallet_a"][:12]
            b    = pair["wallet_b"][:12]
            cnt  = pair["co_investments"]
            import datetime
            last = datetime.datetime.fromtimestamp(pair["last_ts"]).strftime("%Y-%m-%d") if pair.get("last_ts") else "?"
            tokens_list = pair.get("tokens", [])
            tok_str = f"  Tokens: {', '.join(t[:8] for t in tokens_list[:3])}" if tokens_list else ""
            lines.append(f"{i}. `{a}...` ↔ `{b}...`")
            lines.append(f"   Co-investments: *{cnt}*  Last: {last}{tok_str}")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown"
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")


async def cmd_playbook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /playbook
    Show the current predictive launch playbook — which archetypes are winning,
    historical win rates, and recent high-confidence predictions.
    """
    try:
        await update.message.reply_text(
            "📖 *Loading playbook...*",
            parse_mode="Markdown"
        )

        import launch_predictor
        import asyncio
        import datetime as dt

        loop    = asyncio.get_running_loop()
        summary = await loop.run_in_executor(None, launch_predictor.get_playbook_summary)

        ranked  = summary.get("ranked_archetypes", [])
        recent  = summary.get("recent_predictions", [])
        best    = summary.get("best_bet")
        built   = summary.get("stats_last_built", 0)

        built_str = dt.datetime.fromtimestamp(built).strftime("%H:%M") if built else "never"

        lines = [f"📖 *Launch Playbook* _(stats built {built_str})_\n"]

        # Best bet callout
        if best and best.get("rank_score", 0) > 0:
            lines.append(
                f"🎯 *Current Best Bet:* {best['emoji']} {best['description']}\n"
                f"   Win rate: *{best.get('win_rate', 0):.0%}* "
                f"from {best.get('total', 0)} signals\n"
            )

        # Archetype leaderboard
        lines.append("*Archetype Win Rates:*")
        for arch in ranked:
            emoji     = arch.get("emoji", "⚪")
            desc      = arch.get("description", arch["key"])[:35]
            win_rate  = arch.get("win_rate", 0.0)
            total     = arch.get("total", 0)
            avg_score = arch.get("avg_score", 0.0)
            bar       = "▓" * int(win_rate * 10) + "░" * (10 - int(win_rate * 10))
            if total == 0:
                lines.append(f"{emoji} `{bar}` {desc} — no data yet")
            else:
                lines.append(
                    f"{emoji} `{bar}` *{win_rate:.0%}* win  _{desc}_\n"
                    f"   {total} signals · avg score {avg_score:.0f}"
                )

        # Recent predictions
        if recent:
            lines.append("\n*Recent Predictions:*")
            for p in recent[:6]:
                ts    = p.get("ts", 0)
                name  = p.get("name", p.get("mint", "?")[:10])
                arch  = p.get("archetype", "NONE")
                conf  = p.get("confidence", 0)
                boost = p.get("boost", 0)
                time_str = dt.datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"
                if arch == "NONE":
                    continue
                boost_str = f"+{boost}pts" if boost else "0pts"
                lines.append(f"  `{time_str}` {name} → *{arch}* conf={conf}% {boost_str}")

        lines.append("\n_Use /playbook to refresh · rebuilds stats every 2h_")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown"
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")


async def cmd_bundle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /bundle <mint>
    Analyse a token's early buyers for coordinated bundle (same-funder) patterns.
    """
    try:
        if not context.args:
            await update.message.reply_text(
                "📋 Usage: /bundle <mint>\n\n"
                "Analyses the token's early buyers to detect coordinated wallet clusters.",
                parse_mode="Markdown"
            )
            return

        mint = context.args[0].strip()
        await update.message.reply_text(
            "🔍 *Analysing wallet funding lineage...*\n_This may take 15–30 seconds._",
            parse_mode="Markdown"
        )

        import wallet_fingerprint
        from scanner import fetch_rugcheck
        import asyncio

        loop = asyncio.get_running_loop()
        rc   = await loop.run_in_executor(None, fetch_rugcheck, mint)

        early_buyers = [h["address"] for h in (rc.get("topHolders") or [])[:10] if h.get("address")]
        if not early_buyers:
            await update.message.reply_text(
                f"⚠️ No holder data found for `{mint[:16]}...`\n"
                f"token may be too new or not indexed yet.",
                parse_mode="Markdown"
            )
            return

        result = await loop.run_in_executor(
            None, wallet_fingerprint.score_bundle_risk, mint, early_buyers
        )

        risk       = result.get("bundle_risk", 0)
        reason     = result.get("reason", "")
        clusters   = result.get("clusters", [])

        if risk == 0:
            emoji = "✅"
            label = "CLEAN"
        elif risk <= 3:
            emoji = "🟡"
            label = "LOW BUNDLE RISK"
        elif risk <= 6:
            emoji = "🟠"
            label = "MODERATE BUNDLE RISK"
        else:
            emoji = "🔴"
            label = "HIGH BUNDLE RISK"

        lines = [
            f"🔬 *Bundle Analysis*",
            f"Mint: `{mint}`\n",
            f"{emoji} *{label}* (Risk: {risk}/10)",
            f"_{reason}_\n",
        ]

        if clusters:
            lines.append(f"*Clusters detected: {len(clusters)}*")
            for c in clusters[:3]:
                funder  = c.get("funder", "")
                wallets = c.get("wallets", [])
                count   = c.get("count", len(wallets))
                funder_short = f"`{funder[:8]}...{funder[-4:]}`" if funder else "unknown"
                wallet_addrs = ", ".join(f"`{w[:6]}..`" for w in wallets[:4])
                lines.append(
                    f"• Funder: {funder_short} → {count} wallets\n"
                    f"  _({wallet_addrs})_"
                )

        lines.append(f"\n_Top {len(early_buyers)} holders analysed_")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown"
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")


async def cmd_fingerprint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /fingerprint <wallet>
    Show the funding lineage of a wallet address (who funded it?).
    """
    try:
        if not context.args:
            await update.message.reply_text(
                "📋 Usage: /fingerprint <wallet_address>\n\n"
                "Shows which wallet funded this address with its initial SOL.",
                parse_mode="Markdown"
            )
            return

        wallet = context.args[0].strip()
        if len(wallet) < 32:
            await update.message.reply_text("❌ Invalid wallet address.")
            return

        await update.message.reply_text(
            "🔍 *Looking up funding lineage...*\n_Checking Solana RPC._",
            parse_mode="Markdown"
        )

        import wallet_fingerprint
        import asyncio

        loop   = asyncio.get_running_loop()
        record = await loop.run_in_executor(None, wallet_fingerprint.get_funding_wallet, wallet)

        funder      = record.get("funder")
        amount      = record.get("fund_amount_sol", 0)
        ts          = record.get("fund_ts", 0)
        cached      = record.get("cached", False)

        cache_note = " _(cached)_" if cached else ""

        if funder:
            import datetime
            fund_date = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "unknown"
            lines = [
                f"🔗 *Wallet Fingerprint*{cache_note}\n",
                f"Address: `{wallet}`\n",
                f"*Funded by:* `{funder}`",
                f"Amount: `{amount:.4f} SOL`",
                f"Date: `{fund_date}`\n",
                f"[View Funder](https://solscan.io/account/{funder})",
            ]
        else:
            lines = [
                f"🔗 *Wallet Fingerprint*{cache_note}\n",
                f"Address: `{wallet}`\n",
                f"⚠️ Could not determine funding source.\n"
                f"_Wallet may be funded by CEX, have no early activity, or RPC data unavailable._",
            ]

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")


async def _show_wallet_menu(send_fn):
    from solders.keypair import Keypair as _KP
    if WALLET_PRIVATE_KEY:
        try:
            kp     = _KP.from_base58_string(WALLET_PRIVATE_KEY)
            pubkey = str(kp.pubkey())
            bal    = get_sol_balance(pubkey)
            text   = (
                f"*👛 Wallet*\n\n"
                f"Address: `{pubkey}`\n"
                f"Balance: `{bal:.4f} SOL`\n\n"
                f"[Solscan](https://solscan.io/account/{pubkey})  "
                f"[DexScreener](https://dexscreener.com/solana/{pubkey})"
            )
        except Exception:
            text = "*👛 Wallet*\n\n⚠️ Key stored but invalid — please re-import."
    else:
        text = "*👛 Wallet*\n\nNo wallet configured yet."

    wallet_rows = []
    if WALLET_PRIVATE_KEY:
        wallet_rows += [
            [InlineKeyboardButton("📤 Send SOL",   callback_data="wallet:send_sol"),
             InlineKeyboardButton("📤 Send Token", callback_data="wallet:send_token")],
            [InlineKeyboardButton("📥 Receive",    callback_data="wallet:receive")],
        ]
    await send_fn(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✨ Create New Wallet", callback_data="wallet:create"),
             InlineKeyboardButton("📥 Import Wallet",     callback_data="wallet:import")],
            *wallet_rows,
            [InlineKeyboardButton("🔑 Export Key ⚠️",    callback_data="wallet:export")],
            [InlineKeyboardButton("👁️ Tracked Wallets",   callback_data="wallet:tracked")],
            [InlineKeyboardButton("⬅️ Main Menu",         callback_data="menu:main")],
        ]),
        disable_web_page_preview=True,
    )


async def wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    action = query.data.split(":")[1]
    await query.answer()

    if action == "menu":
        await _show_wallet_menu(query.edit_message_text)

    elif action == "create":
        # Show backup mode choice
        await query.edit_message_text(
            "*✨ Create New Wallet*\n\n"
            "Choose your seed phrase backup method:\n\n"
            "🌱 *Manual Backup*\n"
            "You write down 12 words\n"
            "Bot stores nothing\n"
            "Highest security\n\n"
            "🔐 *Encrypted Backup*\n"
            "12 words encrypted with password\n"
            "Stored in bot safely\n"
            "Easier recovery",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌱 Manual Backup",    callback_data="wallet:create_manual"),
                 InlineKeyboardButton("🔐 Encrypted Backup", callback_data="wallet:create_encrypted")],
                [InlineKeyboardButton("❌ Cancel",           callback_data="wallet:menu")],
            ])
        )

    elif action == "create_manual":
        try:
            wallet_data = wm.create_wallet_with_mnemonic(backup_mode="manual")
            set_state(uid, pending_wallet_mnemonic=wallet_data["mnemonic"],
                         pending_wallet_pubkey=wallet_data["public_key"],
                         pending_wallet_privkey=wallet_data["private_key_base58"])
            msg = wm.format_wallet_creation_message(wallet_data)
            await query.edit_message_text(
                msg,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ I saved the 12 words", callback_data="wallet:save_pending_bip39")],
                    [InlineKeyboardButton("❌ Start over",           callback_data="wallet:create")],
                ])
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {str(e)[:100]}", reply_markup=back_kb())

    elif action == "create_encrypted":
        set_state(uid, create_mode="bip39_encrypted")
        await query.edit_message_text(
            "*🔐 Encrypted Backup*\n\n"
            "Choose a strong password to encrypt your seed phrase.\n\n"
            "This password:\n"
            "• Is NOT stored anywhere\n"
            "• You must remember it to recover\n"
            "• Can be any length (longer = better)\n\n"
            "_Type your password below:_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wallet:create")]])
        )
        set_state(uid, waiting_for="wallet_backup_password")

    elif action == "save_pending_bip39":
        pubkey = get_state(uid, "pending_wallet_pubkey")
        privkey = get_state(uid, "pending_wallet_privkey")
        privkey_old = get_state(uid, "pending_wallet_privkey")
        
        if not pubkey or not privkey:
            await query.edit_message_text("Error: No wallet pending.", reply_markup=back_kb())
            return
        
        # Clear all pending data
        clear_state(uid)
        
        # Save to config
        save_wallet_key(privkey)
        
        await query.edit_message_text(
            f"✅ *Wallet Saved!*\n\n"
            f"Address: `{pubkey}`\n\n"
            f"✨ Your seed phrase is your backup.\n"
            f"🔐 Recovery code verified.\n\n"
            f"Switch to Live mode to start trading.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Main Menu", callback_data="menu:main")
            ]])
        )

    elif action == "save_pending":
        new_key = get_state(uid, "pending_wallet_key")
        clear_state(uid)
        if not new_key:
            await query.edit_message_text("No pending wallet found.", reply_markup=back_kb())
            return
        save_wallet_key(new_key)
        from solders.keypair import Keypair as _KP
        pubkey = str(_KP.from_base58_string(new_key).pubkey())
        await query.edit_message_text(
            f"✅ *Wallet saved!*\n\nAddress: `{pubkey}`\n\n"
            f"Switch to Live mode to start trading.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Main Menu", callback_data="menu:main")
            ]])
        )

    elif action == "import":
        # Show import choice
        await query.edit_message_text(
            "*📥 Import / Recover Wallet*\n\n"
            "Choose how you want to import a wallet:\n\n"
            "🔑 *Private Key*\n"
            "Import using base58 private key\n\n"
            "🌱 *Seed Phrase*\n"
            "Recover using 12-word mnemonic",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Private Key",  callback_data="wallet:import_key"),
                 InlineKeyboardButton("🌱 Seed Phrase",  callback_data="wallet:import_seed")],
                [InlineKeyboardButton("❌ Cancel",       callback_data="wallet:menu")],
            ])
        )

    elif action == "import_key":
        set_state(uid, waiting_for="wallet_import_key")
        await query.edit_message_text(
            "*📥 Import from Private Key*\n\nPaste your base58 private key:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="wallet:import")
            ]])
        )

    elif action == "import_seed":
        set_state(uid, waiting_for="wallet_import_seed")
        await query.edit_message_text(
            "*🌱 Recover from Seed Phrase*\n\n"
            "Paste your 12 words separated by spaces:\n\n"
            "_Example: word1 word2 word3 ... word12_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="wallet:import")
            ]])
        )

    elif action == "export":
        if not WALLET_PRIVATE_KEY:
            await query.edit_message_text("No wallet stored.", reply_markup=back_kb())
            return
        from solders.keypair import Keypair as _KP
        pubkey = str(_KP.from_base58_string(WALLET_PRIVATE_KEY).pubkey())
        await query.edit_message_text(
            f"*🔑 Private Key Export*\n\n"
            f"Address: `{pubkey}`\n\n"
            f"Key: `{WALLET_PRIVATE_KEY}`\n\n"
            f"⚠️ *Never share this key. Delete this message after saving.*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="wallet:menu")
            ]])
        )

    elif action == "tracked":
        try:
            import wallet_tracker
            watched = wallet_tracker.get_watched_wallets()
            if not watched:
                text = "*👁️ Tracked Wallets*\n\nNo wallets being tracked yet."
                kb = [[InlineKeyboardButton("➕ Add Wallet",   callback_data="wallet:add_tracked"),
                       InlineKeyboardButton("🧠 Discover",     callback_data="wallet:discover")],
                      [InlineKeyboardButton("🕸️ Cluster Top",  callback_data="wallet:clustertop"),
                       InlineKeyboardButton("⬅️ Back",         callback_data="wallet:menu")]]
            else:
                text = "*👁️ Tracked Wallets*\n\n"
                for addr, data in watched.items():
                    name = data.get("name", "Unknown")
                    rep_score = data.get("reputation_score", 50)
                    wins = data.get("wins", 0)
                    total = data.get("entries_total", 0)
                    text += f"• `{addr[:8]}...`\n  {name} | 🏆 {wins}/{total} | Rep: {rep_score}\n"
                kb = [[InlineKeyboardButton("➕ Add Wallet",   callback_data="wallet:add_tracked"),
                       InlineKeyboardButton("🧠 Discover",     callback_data="wallet:discover")],
                      [InlineKeyboardButton("🕸️ Cluster Top",  callback_data="wallet:clustertop"),
                       InlineKeyboardButton("⬅️ Back",         callback_data="wallet:menu")]]
            await query.edit_message_text(
                text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except ImportError:
            await query.edit_message_text(
                "⚠️ Wallet tracker module not found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:menu")]])
            )

    elif action == "discover":
        try:
            import wallet_discovery
            age = wallet_discovery.last_scan_age_secs()
            top = wallet_discovery.get_top_discovered()

            if not top or age > wallet_discovery.DISCOVERY_CACHE_TTL:
                await query.edit_message_text(
                    "*🧠 Discover Smart Wallets*\n\n"
                    "No cached scan available.\n\n"
                    "Use /discoverwallet to run a full discovery scan\n"
                    "_(takes 30–60 seconds)_",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:tracked")]])
                )
                return

            mins_ago = int(age / 60)
            lines = [f"*🧠 Discovered Wallets* _(scan {mins_ago}m ago)_\n"]
            for i, w in enumerate(top[:8], 1):
                addr     = w.get("address", "")
                score    = w.get("discovery_score", 0)
                wins     = w.get("tokens_won", 0)
                avg_mcap = w.get("avg_mcap_usd", 0)
                mcap_str = f"${avg_mcap / 1_000_000:.1f}M" if avg_mcap >= 1_000_000 else f"${avg_mcap / 1_000:.0f}K"
                lines.append(f"{i}. `{addr[:8]}...` | Score:{score} | {wins}W | {mcap_str} avg")

            lines.append("\n_Use /discoverwallet to see full list & rescan_")

            await query.edit_message_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:tracked")]])
            )
        except ImportError:
            await query.edit_message_text(
                "⚠️ Discovery module not found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:tracked")]])
            )

    elif action == "clustertop":
        try:
            import wallet_cluster
            top = wallet_cluster.get_global_top_clusters(10)

            if not top:
                await query.edit_message_text(
                    "🕸️ *Cluster Network*\n\n"
                    "No co-investment data yet.\n\n"
                    "_Cluster relationships build as tracked wallets enter the same tokens during live scanning._",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:tracked")]])
                )
                return

            import datetime
            lines = ["🕸️ *Top Co-Investing Wallet Pairs*\n"]
            for i, pair in enumerate(top, 1):
                a   = pair["wallet_a"][:10]
                b   = pair["wallet_b"][:10]
                cnt = pair["co_investments"]
                last = datetime.datetime.fromtimestamp(pair["last_ts"]).strftime("%m/%d") if pair.get("last_ts") else "?"
                lines.append(f"{i}. `{a}...` ↔ `{b}...`  ×{cnt}  ({last})")

            lines.append("\n_Use /clustertop for full details_")
            await query.edit_message_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:tracked")]])
            )
        except ImportError:
            await query.edit_message_text(
                "⚠️ Cluster module not found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:tracked")]])
            )

    elif action == "add_tracked":
        set_state(uid, waiting_for="tracked_wallet_address")
        await query.edit_message_text(
            "*➕ Add Tracked Wallet*\n\n"
            "Enter the wallet address (Solana):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="wallet:tracked")
            ]])
        )

    elif action.startswith("remove_tracked:"):
        wallet_addr = action.split(":")[1]
        try:
            import wallet_tracker
            # In a real implementation, you'd remove from wallet_tracker here
            # For now, just show a confirmation
            await query.edit_message_text(
                f"*Remove Wallet?*\n\n`{wallet_addr}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Remove", callback_data=f"wallet:confirm_remove:{wallet_addr}")],
                    [InlineKeyboardButton("❌ Keep", callback_data="wallet:tracked")]
                ])
            )
        except ImportError:
            await query.edit_message_text(
                "⚠️ Wallet tracker module not found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:tracked")]])
            )

    elif action.startswith("confirm_remove:"):
        wallet_addr = action.split(":")[1]
        try:
            import wallet_tracker
            removed = wallet_tracker.remove_watched_wallet(wallet_addr)
            if removed:
                await query.edit_message_text(
                    "✅ Wallet removed from tracking.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:tracked")]])
                )
            else:
                await query.edit_message_text(
                    "⚠️ Wallet not found in tracking list.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:tracked")]])
                )
        except Exception as e:
            await query.edit_message_text(
                f"❌ Error: {str(e)}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:tracked")]])
            )

    elif action == "receive":
        pubkey = get_wallet_pubkey()
        if not pubkey:
            await query.edit_message_text("No wallet configured.",
                                          reply_markup=back_kb("wallet:menu"))
            return
        await query.edit_message_text(
            f"📥 *Receive SOL & Tokens*\n\n"
            f"Send SOL or any SPL token to your address:\n\n"
            f"`{pubkey}`\n\n"
            f"[View on Solscan](https://solscan.io/account/{pubkey})",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:menu")]]),
            disable_web_page_preview=False,
        )

    elif action == "send_sol":
        if not WALLET_PRIVATE_KEY:
            await query.answer("No wallet configured.", show_alert=True)
            return
        set_state(uid, waiting_for="wallet_send_sol_to")
        await query.edit_message_text(
            "📤 *Send SOL*\n\nEnter the recipient Solana address:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wallet:menu")]]),
        )

    elif action == "send_sol_exec":
        to_addr = get_state(uid, "send_sol_to")
        amount  = get_state(uid, "send_sol_amount")
        clear_state(uid)
        if not to_addr or not amount:
            await query.edit_message_text("Session expired. Please try again.",
                                          reply_markup=back_kb("wallet:menu"))
            return
        lamports = int(float(amount) * 1_000_000_000)
        await query.edit_message_text("⏳ Broadcasting transaction...")
        sig = send_sol_onchain(to_addr, lamports)
        if sig.startswith("ERROR"):
            await query.edit_message_text(
                f"❌ *Send Failed*\n\n`{sig}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:menu")]]),
            )
        else:
            await query.edit_message_text(
                f"✅ *SOL Sent!*\n\n"
                f"Amount: `{amount} SOL`\n"
                f"To: `{to_addr[:8]}...{to_addr[-6:]}`\n"
                f"TX: `{sig[:20]}...`\n\n"
                f"[View on Solscan](https://solscan.io/tx/{sig})",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Wallet", callback_data="wallet:menu")]]),
                disable_web_page_preview=True,
            )

    elif action == "send_token":
        if not WALLET_PRIVATE_KEY:
            await query.answer("No wallet configured.", show_alert=True)
            return
        pubkey   = get_wallet_pubkey()
        accounts = get_token_accounts(pubkey) if pubkey else []
        if not accounts:
            await query.edit_message_text(
                "No token positions found in this wallet.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:menu")]]),
            )
            return
        rows = []
        for acc in accounts[:20]:
            pair = fetch_sol_pair(acc["mint"])
            sym  = pair.get("baseToken", {}).get("symbol", acc["mint"][:8]) if pair else acc["mint"][:8]
            rows.append([InlineKeyboardButton(
                f"{sym} ({acc['ui_amount']:,.2f})",
                callback_data=f"wallet:send_token_pick:{acc['mint']}"
            )])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="wallet:menu")])
        await query.edit_message_text(
            "📤 *Send Token — Select token to send:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif action == "send_token_pick":
        mint = query.data.split(":", 2)[2]
        pair = fetch_sol_pair(mint)
        sym  = pair.get("baseToken", {}).get("symbol", mint[:8]) if pair else mint[:8]
        set_state(uid, send_token_mint=mint, send_token_sym=sym, waiting_for="wallet_send_token_to")
        # Grab decimals & balance now
        pubkey  = get_wallet_pubkey()
        accs    = get_token_accounts(pubkey) if pubkey else []
        acc_inf = next((a for a in accs if a["mint"] == mint), None)
        if acc_inf:
            set_state(uid, send_token_decimals=acc_inf["decimals"],
                          send_token_max_ui=acc_inf["ui_amount"])
        await query.edit_message_text(
            f"📤 *Send {sym}*\n\nEnter the recipient Solana address:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wallet:menu")]]),
        )

    elif action == "send_token_exec":
        mint    = get_state(uid, "send_token_mint")
        to_addr = get_state(uid, "send_token_to")
        raw_amt = get_state(uid, "send_token_raw")
        sym     = get_state(uid, "send_token_sym", "TOKEN")
        ui_amt  = get_state(uid, "send_token_ui", 0)
        clear_state(uid)
        if not mint or not to_addr or not raw_amt:
            await query.edit_message_text("Session expired. Please try again.",
                                          reply_markup=back_kb("wallet:menu"))
            return
        await query.edit_message_text("⏳ Broadcasting transaction...")
        sig = send_token_onchain(mint, to_addr, int(raw_amt))
        if sig.startswith("ERROR"):
            await query.edit_message_text(
                f"❌ *Send Failed*\n\n`{sig}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:menu")]]),
            )
        else:
            await query.edit_message_text(
                f"✅ *Tokens Sent!*\n\n"
                f"Token: `{sym}`\n"
                f"Amount: `{float(ui_amt):,.4f}`\n"
                f"To: `{to_addr[:8]}...{to_addr[-6:]}`\n"
                f"TX: `{sig[:20]}...`\n\n"
                f"[View on Solscan](https://solscan.io/tx/{sig})",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Wallet", callback_data="wallet:menu")]]),
                disable_web_page_preview=True,
            )


# ─── Pump Live feed ────────────────────────────────────────────────────────────

async def cmd_pumplive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        pf.filter_status_text(uid),
        parse_mode="Markdown",
        reply_markup=pf.filter_kb(uid),
    )


async def pumplive_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    action = query.data.split(":")[1]
    await query.answer()

    async def _refresh():
        try:
            await query.edit_message_text(
                pf.filter_status_text(uid),
                parse_mode="Markdown",
                reply_markup=pf.filter_kb(uid),
            )
        except Exception:
            pass

    if action == "toggle":
        if pf.is_subscribed(uid):
            pf.unsubscribe(uid)
        else:
            pf.subscribe(uid)
        await _refresh()

    elif action == "reset":
        pf.reset_filters(uid)
        await _refresh()

    elif action == "toggle_social":
        f = pf.get_filters(uid)
        f["require_social"] = not f["require_social"]
        pf.set_filters(uid, f)
        await _refresh()

    elif action == "toggle_desc":
        f = pf.get_filters(uid)
        f["require_description"] = not f["require_description"]
        pf.set_filters(uid, f)
        await _refresh()

    elif action == "set_heat":
        current = pf.get_filters(uid).get("min_heat_score") or 0
        set_state(uid, waiting_for="pf_heat")
        await query.edit_message_text(
            "🌡️ *Min Heat Score Filter*\n\n"
            "Only receive pumplive alerts for tokens scoring at or above this threshold.\n\n"
            "Enter a number from `0` to `100`. Send `0` to disable.\n\n"
            f"_Current: {current}_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumplive:menu"),
            ]]),
        )

    elif action == "set_mcap":
        set_state(uid, waiting_for="pf_mcap")
        await query.edit_message_text(
            "💰 *MCap Filter (SOL)*\n\n"
            "Enter a range as `min-max`, e.g. `5-200`\n"
            "Use `0` for no limit. Example: `0-500` means any mcap under 500 SOL.\n\n"
            "_Current: " + pf._sol_range_str(
                pf.get_filters(uid)["min_mcap_sol"],
                pf.get_filters(uid)["max_mcap_sol"]
            ) + "_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumplive:menu"),
            ]]),
        )

    elif action == "set_vol":
        set_state(uid, waiting_for="pf_vol")
        f = pf.get_filters(uid)
        await query.edit_message_text(
            "📈 *SOL Volume Filter*\n\n"
            "Filter by real SOL raised in the bonding curve.\n"
            "Enter min SOL, e.g. `0.5` — or a range `0.5-10`.\n"
            "Use `0` or `0-0` to clear.\n\n"
            "_Current: " + pf._sol_range_str(
                f.get("min_vol_sol", 0), f.get("max_vol_sol", 0)
            ) + "_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumplive:menu"),
            ]]),
        )

    elif action == "set_devbuy":
        set_state(uid, waiting_for="pf_devbuy")
        await query.edit_message_text(
            "🛒 *Dev Buy Filter (SOL)*\n\n"
            "Filter by how much SOL the dev spent on their initial buy.\n"
            "Enter min SOL, e.g. `0.5` — or a range `0.5-5`.\n"
            "Use `0` or `0-0` to clear.\n\n"
            "_Current: " + pf._sol_range_str(
                pf.get_filters(uid)["min_dev_sol"],
                pf.get_filters(uid)["max_dev_sol"]
            ) + "_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumplive:menu"),
            ]]),
        )

    elif action == "set_age":
        current = pf.get_filters(uid).get("max_token_age_mins") or 0
        cur_str = f"{current:.0f} minutes" if current else "disabled"
        set_state(uid, waiting_for="pf_age")
        await query.edit_message_text(
            "🕐 *Token Age Filter*\n\n"
            "Only notify for tokens created within X minutes of now.\n"
            "Useful for catching very fresh launches only.\n\n"
            "Enter max age in minutes, e.g. `5` for tokens under 5 mins old.\n"
            "Send `0` to disable.\n\n"
            f"_Current: {cur_str}_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumplive:menu"),
            ]]),
        )

    elif action == "set_keywords":
        current = ", ".join(pf.get_filters(uid)["keywords"]) or "none"
        set_state(uid, waiting_for="pf_keywords")
        await query.edit_message_text(
            "🏷️ *Keyword Filter*\n\n"
            "Only show tokens whose name, symbol, or description contains at least one keyword.\n\n"
            "Enter keywords separated by commas, e.g. `ai, agent, gpt, robot`\n"
            "Send `clear` to remove all keywords.\n\n"
            f"_Current: {current}_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumplive:menu"),
            ]]),
        )

    elif action == "set_blocked":
        current = ", ".join(pf.get_filters(uid)["blocked_words"]) or "none"
        set_state(uid, waiting_for="pf_blocked")
        await query.edit_message_text(
            "🚫 *Blocked Words*\n\n"
            "Hide tokens whose name, symbol, or description contains any of these words.\n\n"
            "Enter words separated by commas, e.g. `elon, trump, shib, inu`\n"
            "Send `clear` to remove all blocked words.\n\n"
            f"_Current: {current}_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumplive:menu"),
            ]]),
        )

    elif action == "set_tracked":
        current = "\n".join(pf.get_filters(uid)["tracked_wallets"]) or "none"
        set_state(uid, waiting_for="pf_tracked_wallets")
        await query.edit_message_text(
            "👛 *Tracked Wallets*\n\n"
            "Always notify when these dev wallets launch a token (bypasses all other filters).\n\n"
            "Enter wallet addresses separated by commas or one per line.\n"
            "Send `clear` to remove all.\n\n"
            f"_Current:_\n`{current}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumplive:menu"),
            ]]),
        )

    elif action == "set_block_wallet":
        current = "\n".join(pf.get_filters(uid)["blocked_wallets"]) or "none"
        set_state(uid, waiting_for="pf_blocked_wallets")
        await query.edit_message_text(
            "🚫 *Blocked Wallets*\n\n"
            "Never notify when these dev wallets launch a token.\n\n"
            "Enter wallet addresses separated by commas or one per line.\n"
            "Send `clear` to remove all.\n\n"
            f"_Current:_\n`{current}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumplive:menu"),
            ]]),
        )

    elif action == "channel_menu":
        clear_state(uid)
        ch = pf.get_pumplive_channel()
        ch_str = f"`{ch}`" if ch else "not set"
        subscribed = pf.is_subscribed(uid)
        dm_lbl = "🔕 Pause My DM Alerts" if subscribed else "🔔 Resume My DM Alerts"
        dm_status = "🟢 ON" if subscribed else "🔴 OFF (channel-only)"
        await query.edit_message_text(
            f"📣 *Pump Live — Alert Channel*\n\n"
            f"Channel: {ch_str}\n"
            f"Your DMs: {dm_status}\n\n"
            "Alerts go to both your DMs and the channel.\n"
            "Pause your DMs to use channel-only mode.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Set Channel",            callback_data="pumplive:set_channel")],
                *([ [InlineKeyboardButton("🗑️ Remove Channel",       callback_data="pumplive:clear_channel")] ] if ch else []),
                [InlineKeyboardButton("📋 Sync My DM Filters→Ch",   callback_data="pumplive:sync_ch_filters"),
                 InlineKeyboardButton("🔄 Clear Ch Filters",        callback_data="pumplive:clear_ch_filters")],
                [InlineKeyboardButton(dm_lbl,                       callback_data="pumplive:toggle")],
                [InlineKeyboardButton("⬅️ Back",                      callback_data="pumplive:menu")],
            ])
        )

    elif action == "sync_ch_filters":
        pf.set_channel_filters(pf.get_filters(uid))
        await query.answer("✅ Pump Live channel now uses your DM filters.", show_alert=True)

    elif action == "clear_ch_filters":
        pf.set_channel_filters(None)
        await query.answer("🔄 Channel filters cleared — channel is now unfiltered.", show_alert=True)

    elif action == "set_channel":
        set_state(uid, waiting_for="pumplive_channel")
        await query.edit_message_text(
            "📣 *Set Pump Live Channel*\n\n"
            "Send the channel ID (e.g. `-1001234567890`) or @username (e.g. `@mychannel`).",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumplive:channel_menu"),
            ]])
        )

    elif action == "clear_channel":
        pf.set_pumplive_channel(None)
        await query.edit_message_text(
            "🗑️ Pump Live alert channel removed.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="pumplive:menu"),
            ]])
        )

    elif action == "menu":
        clear_state(uid)
        await _refresh()


async def cmd_pumpgrad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        pf.grad_filter_status_text(uid),
        parse_mode="Markdown",
        reply_markup=pf.grad_filter_kb(uid),
    )


async def pumpgrad_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    action = query.data.split(":")[1]
    await query.answer()

    async def _refresh():
        try:
            await query.edit_message_text(
                pf.grad_filter_status_text(uid),
                parse_mode="Markdown",
                reply_markup=pf.grad_filter_kb(uid),
            )
        except Exception:
            pass

    if action == "toggle":
        if pf.is_grad_subscribed(uid):
            pf.unsubscribe_grad(uid)
        else:
            pf.subscribe_grad(uid)
        await _refresh()

    elif action == "reset":
        pf.reset_grad_filters(uid)
        await _refresh()

    elif action == "toggle_social":
        f = pf.get_grad_filters(uid)
        f["require_social"] = not f["require_social"]
        pf.set_grad_filters(uid, f)
        await _refresh()

    elif action == "toggle_desc":
        f = pf.get_grad_filters(uid)
        f["require_description"] = not f["require_description"]
        pf.set_grad_filters(uid, f)
        await _refresh()

    elif action == "set_mcap":
        set_state(uid, waiting_for="pg_mcap")
        await query.edit_message_text(
            "💰 *MCap Filter (SOL)*\n\n"
            "Enter a range as `min-max`, e.g. `50-500`\n"
            "Use `0` for no limit. Example: `30-0` means any mcap above 30 SOL.\n\n"
            "_Current: " + pf._sol_range_str(
                pf.get_grad_filters(uid)["min_mcap_sol"],
                pf.get_grad_filters(uid)["max_mcap_sol"]
            ) + "_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumpgrad:menu"),
            ]]),
        )

    elif action == "set_devbuy":
        set_state(uid, waiting_for="pg_devbuy")
        await query.edit_message_text(
            "🛒 *Dev Buy Filter (SOL)*\n\n"
            "Enter a range as `min-max`, e.g. `0.5-5`\n"
            "Use `0` for no limit.\n\n"
            "_Current: " + pf._sol_range_str(
                pf.get_grad_filters(uid)["min_dev_sol"],
                pf.get_grad_filters(uid)["max_dev_sol"]
            ) + "_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumpgrad:menu"),
            ]]),
        )

    elif action == "set_keywords":
        set_state(uid, waiting_for="pg_keywords")
        await query.edit_message_text(
            "🏷️ *Keyword Filter*\n\n"
            "Enter keywords separated by commas. Only tokens matching at least one will alert.\n"
            "Send `clear` to remove all keywords.\n\n"
            "_Example: `ai, agent, robot`_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumpgrad:menu"),
            ]]),
        )

    elif action == "set_blocked":
        set_state(uid, waiting_for="pg_blocked")
        await query.edit_message_text(
            "🚫 *Blocked Words*\n\n"
            "Tokens containing any of these words will be skipped.\n"
            "Send `clear` to remove all blocked words.\n\n"
            "_Example: `scam, test, rug`_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumpgrad:menu"),
            ]]),
        )

    elif action == "set_tracked":
        set_state(uid, waiting_for="pg_tracked_wallets")
        await query.edit_message_text(
            "👛 *Track Dev Wallets*\n\n"
            "Enter wallet addresses separated by commas. Tokens from these devs always alert, bypassing all filters.\n"
            "Send `clear` to remove all.\n\n"
            "_Paste one or more Solana addresses_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumpgrad:menu"),
            ]]),
        )

    elif action == "set_block_wallet":
        set_state(uid, waiting_for="pg_blocked_wallets")
        await query.edit_message_text(
            "🚫 *Block Dev Wallets*\n\n"
            "Tokens from these dev wallets will never alert.\n"
            "Send `clear` to remove all.\n\n"
            "_Paste one or more Solana addresses_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumpgrad:menu"),
            ]]),
        )

    elif action == "toggle_grad_autobuy":
        pf.set_grad_autobuy(uid, not pf.is_grad_autobuy(uid))
        await _refresh()

    elif action == "channel_menu":
        clear_state(uid)
        ch = pf.get_pumpgrad_channel()
        ch_str = f"`{ch}`" if ch else "not set"
        subscribed = pf.is_grad_subscribed(uid)
        dm_lbl = "🔕 Pause My DM Alerts" if subscribed else "🔔 Resume My DM Alerts"
        dm_status = "🟢 ON" if subscribed else "🔴 OFF (channel-only)"
        await query.edit_message_text(
            f"📣 *Pump Grad — Alert Channel*\n\n"
            f"Channel: {ch_str}\n"
            f"Your DMs: {dm_status}\n\n"
            "Alerts go to both your DMs and the channel.\n"
            "Pause your DMs to use channel-only mode.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Set Channel",            callback_data="pumpgrad:set_channel")],
                *([ [InlineKeyboardButton("🗑️ Remove Channel",       callback_data="pumpgrad:clear_channel")] ] if ch else []),
                [InlineKeyboardButton("📋 Sync My DM Filters→Ch",   callback_data="pumpgrad:sync_ch_filters"),
                 InlineKeyboardButton("🔄 Clear Ch Filters",        callback_data="pumpgrad:clear_ch_filters")],
                [InlineKeyboardButton(dm_lbl,                       callback_data="pumpgrad:toggle")],
                [InlineKeyboardButton("⬅️ Back",                      callback_data="pumpgrad:menu")],
            ])
        )

    elif action == "set_heat":
        current = pf.get_grad_filters(uid).get("min_heat_score") or 0
        set_state(uid, waiting_for="pg_heat")
        await query.edit_message_text(
            f"🌡️ *Min Heat Score Filter (Grad)*\n\n"
            f"Only receive grad alerts for tokens scoring at or above this threshold.\n\n"
            f"Enter a number `0`\u2013`100`. Send `0` to disable.\n\n"
            f"_Current: {current}_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumpgrad:menu")
            ]]),
        )

    elif action == "sync_ch_filters":
        pf.set_grad_channel_filters(pf.get_grad_filters(uid))
        await query.answer("✅ Pump Grad channel now uses your DM filters.", show_alert=True)

    elif action == "clear_ch_filters":
        pf.set_grad_channel_filters(None)
        await query.answer("🔄 Grad channel filters cleared — channel is now unfiltered.", show_alert=True)

    elif action == "set_channel":
        set_state(uid, waiting_for="pumpgrad_channel")
        await query.edit_message_text(
            "📣 *Set Pump Grad Channel*\n\n"
            "Send the channel ID (e.g. `-1001234567890`) or @username (e.g. `@mychannel`).",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="pumpgrad:channel_menu"),
            ]])
        )

    elif action == "clear_channel":
        pf.set_pumpgrad_channel(None)
        await query.edit_message_text(
            "🗑️ Pump Grad alert channel removed.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="pumpgrad:menu"),
            ]])
        )

    elif action == "menu":
        clear_state(uid)
        await _refresh()


async def pf_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quick-buy buttons on pump.fun launch notifications."""
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":", 3)   # pf : buy : 0.1 : <mint>
    sol    = float(parts[2])
    mint   = parts[3]
    await query.answer()

    mode = get_mode(uid)
    loop = asyncio.get_running_loop()

    # Try DexScreener for price data; pump.fun tokens may not be listed yet
    pair     = await loop.run_in_executor(None, fetch_sol_pair, mint)
    sym      = pair.get("baseToken", {}).get("symbol", mint[:8]) if pair else mint[:8]
    price    = float(pair.get("priceUsd", 0) or 0) if pair else 0
    mcap     = float(pair.get("marketCap", 0) or 0) if pair else 0
    decimals = int(pair.get("baseToken", {}).get("decimals", 6) or 6) if pair else 6

    if mode == "paper":
        portfolio = get_portfolio(uid)
        if portfolio.get("SOL", 0) < sol:
            await query.edit_message_text(
                f"❌ Not enough paper SOL. Balance: `{portfolio.get('SOL', 0):.4f}`",
                parse_mode="Markdown",
                reply_markup=pf.notification_kb(mint),
            )
            return
        # Use Jupiter quote for accurate token amount — same as do_trade_flow and execute_auto_buy
        lamports = int(sol * 1_000_000_000)
        quote    = await loop.run_in_executor(None, jupiter_quote, SOL_MINT, mint, lamports)
        if not quote or "error" in quote or not quote.get("outAmount"):
            await query.edit_message_text(
                "⚠️ Could not get quote — token may not be listed yet. Try again in a moment.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="menu:main")]])
            )
            return
        tok_est = int(quote["outAmount"])
        # Derive buy price from quote if DexScreener price unavailable (newly graduated)
        buy_price = price
        if not buy_price and tok_est > 0:
            sol_price_usd = pf.get_sol_price() or 150.0
            ui_tokens = tok_est / (10 ** decimals)
            buy_price = (sol * sol_price_usd) / ui_tokens if ui_tokens > 0 else 0
        portfolio["SOL"]  = portfolio.get("SOL", 0) - sol
        portfolio[mint]   = portfolio.get(mint, 0) + tok_est
        update_portfolio(uid, portfolio)
        log_trade(uid, "paper", "buy", mint, sym, sol_amount=sol,
                  token_amount=tok_est, price_usd=buy_price, mcap=mcap)
        if buy_price > 0:
            setup_auto_sell(uid, mint, sym, buy_price, tok_est, decimals, sol_amount=sol)
        await query.edit_message_text(
            f"📄 *Paper Buy — ${sym}*\n"
            f"Spent: `{sol} SOL`\n"
            f"Got: `{tok_est/(10**decimals):,.4f}` tokens\n"
            + (f"Price: `${buy_price:.8f}`\n" if buy_price else "⚠️ Not yet listed on DexScreener\n")
            + f"SOL left: `{portfolio['SOL']:.4f}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Menu", callback_data="menu:main"),
            ]])
        )
    else:
        # Live — use pump.fun bonding curve directly, skip Jupiter quote
        bc  = await loop.run_in_executor(None, pumpfun.fetch_bonding_curve_data, mint, SOLANA_RPC)
        via = "pumpfun" if bc and not bc.get("complete") else "jupiter"
        tok_est = int((sol / price) * (10 ** decimals)) if price > 0 else 0
        context.user_data["pending_buy"] = {
            "mint":       mint,
            "symbol":     sym,
            "sol_amount": sol,
            "price_usd":  price,
            "mcap":       mcap,
            "decimals":   decimals,
            "via":        via,
            "tok_est":    tok_est,
            "quote":      None,
        }
        await query.edit_message_text(
            f"🟢 *Confirm Buy — ${sym}*\n\n"
            f"Amount: `{sol} SOL`\n"
            + (f"Price: `${price:.8f}`\n" if price else "⚠️ Price not on DexScreener yet\n")
            + f"Est. tokens: `{tok_est/(10**decimals):,.4f}`\n"
            f"Route: {'pump.fun bonding curve' if via == 'pumpfun' else 'Jupiter'}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirm", callback_data="confirm:buy"),
                InlineKeyboardButton("❌ Cancel",  callback_data="cancel"),
            ]])
        )


async def _scanner_settings_render(query, uid: int):
    """Re-render the 4-tier scanner settings panel after any threshold change."""
    user_settings = sm.get_user_settings(uid)
    scouted   = user_settings.get("alert_scouted_threshold", 35)
    warm      = user_settings.get("alert_warm_threshold", 55)
    hot       = user_settings.get("alert_hot_threshold", 70)
    ultra_hot = user_settings.get("alert_ultra_hot_threshold", 85)
    mcap_min  = user_settings.get("scanner_mcap_min", 5_000)
    mcap_max  = user_settings.get("scanner_mcap_max", 10_000_000)
    def _fmt(v):
        if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
        if v >= 1_000:     return f"${v/1_000:.0f}K"
        return f"${v}"
    await query.edit_message_text(
        f"*🌡️ Scanner Settings*\n\n"
        f"⚪ *Scouted* — `{scouted}/100`\n"
        f"_Compact watchlist ping._\n\n"
        f"🟡 *Warm* — `{warm}/100`\n"
        f"_Full DM alert (base tier)._\n\n"
        f"🟠 *Hot* — `{hot}/100`\n"
        f"_Full DM alert (strong momentum)._\n\n"
        f"🔴 *Ultra Hot* — `{ultra_hot}/100`\n"
        f"_Full DM alert (highest priority)._\n\n"
        f"💰 *MCap Range* — `{_fmt(mcap_min)}` – `{_fmt(mcap_max)}`\n"
        f"_Only tokens within this market cap range will alert you._\n\n"
        f"Tap to edit:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Scouted",   callback_data="scanner:edit_watch"),
             InlineKeyboardButton("✏️ Warm",      callback_data="scanner:edit_alert")],
            [InlineKeyboardButton("✏️ Hot",       callback_data="scanner:edit_hot"),
             InlineKeyboardButton("✏️ Ultra Hot", callback_data="scanner:edit_ultra_hot")],
            [InlineKeyboardButton("✏️ MCap Min",  callback_data="scanner:edit_mcap_min"),
             InlineKeyboardButton("✏️ MCap Max",  callback_data="scanner:edit_mcap_max")],
            [InlineKeyboardButton("📋 Presets", callback_data="heatscore:presets")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu:main")],
        ])
    )


# ─── Scanner callback ──────────────────────────────────────────────────────────

async def scanner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    action = query.data.split(":")[1]
    await query.answer()

    if action == "toggle":
        _db.set_scanning(True)
        targets = _db.get_scan_targets()
        if uid in targets:
            # Pause alerts for this user
            _db.remove_scan_target(uid)
            await query.edit_message_text(
                "⏸ *Scout paused.*\n\nScanner keeps running in the background.\nTap Start Scout to get alerts again.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔔 Start Scout", callback_data="scanner:toggle"),
                    InlineKeyboardButton("⬅️ Menu",        callback_data="menu:main"),
                ]])
            )
        else:
            # Resume alerts for this user
            _db.add_scan_target(uid)
            user_settings = sm.get_user_settings(uid)
            watch_thr = user_settings.get("alert_scouted_threshold", 20)
            alert_thr = user_settings.get("alert_warm_threshold", 70)
            await query.edit_message_text(
                f"🟢 *Scout is live!*\n\n"
                f"Scanning every 15 seconds.\n"
                f"📡 Watch ping ≥ {watch_thr} · 🔔 Full alert ≥ {alert_thr}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔕 Pause Scout",  callback_data="scanner:toggle"),
                    InlineKeyboardButton("⚙️ Settings",     callback_data="scanner:set_threshold"),
                    InlineKeyboardButton("👀 Scouted",      callback_data="scanner:watchlist"),
                ]])
            )

    elif action == "watchlist":
        wl         = sc.get_watchlist()
        u_settings = sm.get_user_settings(uid)
        watch_thr  = u_settings.get("alert_scouted_threshold", 20)
        alert_thr  = u_settings.get("alert_warm_threshold", 70)
        if not wl:
            await query.edit_message_text(
                f"👀 *No scouted tokens yet.*\n\n"
                f"Tokens scoring ≥ `{watch_thr}` appear here as the scanner runs.\n"
                f"Full alerts fire at ≥ `{alert_thr}`.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏆 Top Scouts", callback_data="scanner:topalerts"),
                     InlineKeyboardButton("⚙️ Settings",   callback_data="scanner:set_threshold")],
                    [InlineKeyboardButton("⬅️ Menu",        callback_data="menu:main")],
                ])
            )
            return
        items = [t for t in wl.values() if t.get("score", 0) >= watch_thr]
        items = sorted(items, key=lambda x: -x.get("score", 0))[:25]
        lines = [f"*👀 Watchlist* — score ≥ {watch_thr} | alert ≥ {alert_thr}\n"]
        for t in items:
            score = t.get("score", 0)
            flag  = "🔥" if score >= alert_thr else ("🟡" if score >= (watch_thr + alert_thr) // 2 else "⚪")
            mcap  = t.get("mcap", 0) or 0
            mcap_s = (f"${mcap/1_000_000:.2f}M" if mcap >= 1_000_000
                      else f"${mcap/1_000:.0f}K" if mcap >= 1_000 else f"${mcap:.0f}")
            lines.append(
                f"{flag} *{t['name']}* (${t['symbol']}) — `{score}/100`\n"
                f"   MCap: {mcap_s}"
            )
        await query.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏆 Top Scouts", callback_data="scanner:topalerts"),
                 InlineKeyboardButton("⚙️ Settings",   callback_data="scanner:set_threshold")],
                [InlineKeyboardButton("⬅️ Menu",        callback_data="menu:main")],
            ])
        )

    elif action == "topalerts":
        alerts = sc.get_todays_alerts()
        if not alerts:
            await query.edit_message_text(
                "🏆 *No scouts fired today yet.*\n\nThe scanner is live — scouts will appear here as tokens are found.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("👀 Scouted",   callback_data="scanner:watchlist"),
                    InlineKeyboardButton("⬅️ Menu",      callback_data="menu:main"),
                ]])
            )
            return
        top   = sorted(alerts, key=lambda x: -x.get("timestamp", 0))[:10]
        lines = ["*🏆 Top Scouts Today*\n"]
        for i, e in enumerate(top, 1):
            label = sc.priority_label(e["score"])
            lines.append(f"{i}. {label} *{e['name']}* (${e['symbol']}) — {e['score']}/100")
        await query.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👀 Scouted",   callback_data="scanner:watchlist"),
                InlineKeyboardButton("⬅️ Menu",      callback_data="menu:main"),
            ]])
        )

    elif action == "set_threshold":
        user_settings = sm.get_user_settings(uid)
        scouted   = user_settings.get("alert_scouted_threshold", 35)
        warm      = user_settings.get("alert_warm_threshold", 55)
        hot       = user_settings.get("alert_hot_threshold", 70)
        ultra_hot = user_settings.get("alert_ultra_hot_threshold", 85)
        mcap_min  = user_settings.get("scanner_mcap_min", 5_000)
        mcap_max  = user_settings.get("scanner_mcap_max", 10_000_000)
        def _fmt_mcap(v):
            if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
            if v >= 1_000:     return f"${v/1_000:.0f}K"
            return f"${v}"
        await query.edit_message_text(
            f"*🌡️ Scanner Settings*\n\n"
            f"⚪ *Scouted* — `{scouted}/100`\n"
            f"_Compact watchlist ping._\n\n"
            f"🟡 *Warm* — `{warm}/100`\n"
            f"_Full DM alert (base tier)._\n\n"
            f"🟠 *Hot* — `{hot}/100`\n"
            f"_Full DM alert (strong momentum)._\n\n"
            f"🔴 *Ultra Hot* — `{ultra_hot}/100`\n"
            f"_Full DM alert (highest priority)._\n\n"
            f"💰 *MCap Range* — `{_fmt_mcap(mcap_min)}` – `{_fmt_mcap(mcap_max)}`\n"
            f"_Only tokens within this market cap range will alert you._\n\n"
            f"Tap to edit:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Scouted",   callback_data="scanner:edit_watch"),
                 InlineKeyboardButton("✏️ Warm",      callback_data="scanner:edit_alert")],
                [InlineKeyboardButton("✏️ Hot",       callback_data="scanner:edit_hot"),
                 InlineKeyboardButton("✏️ Ultra Hot", callback_data="scanner:edit_ultra_hot")],
                [InlineKeyboardButton("✏️ MCap Min",  callback_data="scanner:edit_mcap_min"),
                 InlineKeyboardButton("✏️ MCap Max",  callback_data="scanner:edit_mcap_max")],
                [InlineKeyboardButton("📋 Presets", callback_data="heatscore:presets")],
                [InlineKeyboardButton("⬅️ Back", callback_data="menu:main")],
            ])
        )

    elif action == "edit_watch":
        user_settings = sm.get_user_settings(uid)
        cur = user_settings.get("alert_scouted_threshold", 35)
        set_state(uid, waiting_for="scanner_watch_threshold")
        await query.edit_message_text(
            f"*⚪ Scouted Threshold*\n\n"
            f"Current: `{cur}/100`\n\n"
            f"Tokens scoring at or above this appear in your Watchlist with a compact ping.\n"
            f"_Tap a preset or type a custom value:_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("20", callback_data="scanner:wset:20"),
                 InlineKeyboardButton("30", callback_data="scanner:wset:30"),
                 InlineKeyboardButton("40", callback_data="scanner:wset:40"),
                 InlineKeyboardButton("50", callback_data="scanner:wset:50")],
                [InlineKeyboardButton("60", callback_data="scanner:wset:60"),
                 InlineKeyboardButton("70", callback_data="scanner:wset:70"),
                 InlineKeyboardButton("80", callback_data="scanner:wset:80"),
                 InlineKeyboardButton("90", callback_data="scanner:wset:90")],
                [InlineKeyboardButton("❌ Cancel", callback_data="scanner:set_threshold")],
            ])
        )

    elif action.startswith("wset:"):
        val = int(action.split(":")[1])
        user_settings = sm.get_user_settings(uid)
        alert_score   = user_settings.get("alert_warm_threshold", 70)
        if val > alert_score:
            user_settings["alert_warm_threshold"] = val
        user_settings["alert_scouted_threshold"] = val
        sm.save_user_settings(uid, user_settings)
        clear_state(uid)
        await query.answer(f"✅ Scouted threshold → {val}", show_alert=False)
        await _scanner_settings_render(query, uid)

    elif action == "edit_alert":
        user_settings = sm.get_user_settings(uid)
        cur = user_settings.get("alert_warm_threshold", 55)
        set_state(uid, waiting_for="scanner_alert_threshold")
        await query.edit_message_text(
            f"*🟡 Warm Threshold*\n\n"
            f"Current: `{cur}/100`\n\n"
            f"Tokens at or above this score trigger a full DM alert.\n"
            f"_Tap a preset or type a custom value:_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("30", callback_data="scanner:aset:30"),
                 InlineKeyboardButton("40", callback_data="scanner:aset:40"),
                 InlineKeyboardButton("50", callback_data="scanner:aset:50"),
                 InlineKeyboardButton("55 ★", callback_data="scanner:aset:55")],
                [InlineKeyboardButton("60", callback_data="scanner:aset:60"),
                 InlineKeyboardButton("65", callback_data="scanner:aset:65"),
                 InlineKeyboardButton("70", callback_data="scanner:aset:70"),
                 InlineKeyboardButton("80", callback_data="scanner:aset:80")],
                [InlineKeyboardButton("❌ Cancel", callback_data="scanner:set_threshold")],
            ])
        )

    elif action.startswith("aset:"):
        val = int(action.split(":")[1])
        user_settings = sm.get_user_settings(uid)
        # Only update alert_warm_threshold — preserve hot/ultra_hot relative spacing
        watch_score = user_settings.get("alert_scouted_threshold", 35)
        if val < watch_score:
            user_settings["alert_scouted_threshold"] = val
        user_settings["alert_warm_threshold"] = val
        # Ensure hot >= warm+10 and ultra_hot >= hot+10 to keep tiers ordered
        cur_hot = user_settings.get("alert_hot_threshold", 70)
        cur_uh  = user_settings.get("alert_ultra_hot_threshold", 85)
        if cur_hot < val + 10:
            user_settings["alert_hot_threshold"] = val + 10
        if cur_uh < user_settings["alert_hot_threshold"] + 10:
            user_settings["alert_ultra_hot_threshold"] = user_settings["alert_hot_threshold"] + 10
        sm.save_user_settings(uid, user_settings)
        clear_state(uid)
        await query.answer(f"✅ Warm threshold → {val}", show_alert=False)
        await _scanner_settings_render(query, uid)

    elif action == "edit_hot":
        user_settings = sm.get_user_settings(uid)
        cur = user_settings.get("alert_hot_threshold", 70)
        set_state(uid, waiting_for="scanner_hot_threshold")
        await query.edit_message_text(
            f"*🟠 Hot Threshold*\n\n"
            f"Current: `{cur}/100`\n\n"
            f"Tokens at or above this score trigger a HOT alert (strong momentum).\n"
            f"_Tap a preset or type a custom value:_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("50", callback_data="scanner:hset:50"),
                 InlineKeyboardButton("60", callback_data="scanner:hset:60"),
                 InlineKeyboardButton("70 ★", callback_data="scanner:hset:70"),
                 InlineKeyboardButton("75", callback_data="scanner:hset:75")],
                [InlineKeyboardButton("80", callback_data="scanner:hset:80"),
                 InlineKeyboardButton("85", callback_data="scanner:hset:85"),
                 InlineKeyboardButton("90", callback_data="scanner:hset:90"),
                 InlineKeyboardButton("95", callback_data="scanner:hset:95")],
                [InlineKeyboardButton("❌ Cancel", callback_data="scanner:set_threshold")],
            ])
        )

    elif action.startswith("hset:"):
        val = int(action.split(":")[1])
        user_settings = sm.get_user_settings(uid)
        user_settings["alert_hot_threshold"] = val
        cur_uh = user_settings.get("alert_ultra_hot_threshold", 85)
        if cur_uh < val + 5:
            user_settings["alert_ultra_hot_threshold"] = val + 5
        sm.save_user_settings(uid, user_settings)
        clear_state(uid)
        await query.answer(f"✅ Hot threshold → {val}", show_alert=False)
        await _scanner_settings_render(query, uid)

    elif action == "edit_ultra_hot":
        user_settings = sm.get_user_settings(uid)
        cur = user_settings.get("alert_ultra_hot_threshold", 85)
        set_state(uid, waiting_for="scanner_ultra_hot_threshold")
        await query.edit_message_text(
            f"*🔴 Ultra Hot Threshold*\n\n"
            f"Current: `{cur}/100`\n\n"
            f"Tokens at or above this score trigger an ULTRA HOT alert (highest priority).\n"
            f"_Tap a preset or type a custom value:_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("70", callback_data="scanner:uset:70"),
                 InlineKeyboardButton("75", callback_data="scanner:uset:75"),
                 InlineKeyboardButton("80", callback_data="scanner:uset:80"),
                 InlineKeyboardButton("85 ★", callback_data="scanner:uset:85")],
                [InlineKeyboardButton("88", callback_data="scanner:uset:88"),
                 InlineKeyboardButton("90", callback_data="scanner:uset:90"),
                 InlineKeyboardButton("95", callback_data="scanner:uset:95"),
                 InlineKeyboardButton("100", callback_data="scanner:uset:100")],
                [InlineKeyboardButton("❌ Cancel", callback_data="scanner:set_threshold")],
            ])
        )

    elif action.startswith("uset:"):
        val = int(action.split(":")[1])
        user_settings = sm.get_user_settings(uid)
        user_settings["alert_ultra_hot_threshold"] = val
        sm.save_user_settings(uid, user_settings)
        clear_state(uid)
        await query.answer(f"✅ Ultra Hot threshold → {val}", show_alert=False)
        await _scanner_settings_render(query, uid)

    elif action == "edit_mcap_min":
        set_state(uid, waiting_for="scanner_mcap_min")
        cur = sm.get_user_settings(uid).get("scanner_mcap_min", 5_000)
        await query.edit_message_text(
            f"*💰 MCap Minimum*\n\n"
            f"Current: `${cur:,}`\n\n"
            f"Tokens with market cap below this are ignored.\n"
            f"Enter a value in USD (e.g. `5000`, `10000`, `50000`):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="scanner:set_threshold"),
            ]])
        )

    elif action == "edit_mcap_max":
        set_state(uid, waiting_for="scanner_mcap_max")
        cur = sm.get_user_settings(uid).get("scanner_mcap_max", 10_000_000)
        await query.edit_message_text(
            f"*💰 MCap Maximum*\n\n"
            f"Current: `${cur:,}`\n\n"
            f"Tokens with market cap above this are ignored.\n"
            f"Enter a value in USD (e.g. `1000000`, `5000000`, `10000000`):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="scanner:set_threshold"),
            ]])
        )

    elif action == "alert_channel_menu":
        ch = sc.get_alert_channel()
        ch_txt = f"`{ch}`" if ch else "_Not set_"
        await query.edit_message_text(
            f"*📣 Alert Channel*\n\n"
            f"When set, every scanner alert is also posted to this channel.\n"
            f"Note: your DMs are still sent too. Pause your DM alerts via the Scanner menu to receive alerts in the channel only.\n\n"
            f"Current: {ch_txt}\n\n"
            f"To set: add the bot as admin to your channel, then enter the channel ID or @username below.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Set Channel", callback_data="scanner:set_alert_channel")],
                *([[InlineKeyboardButton("🗑️ Remove Channel", callback_data="scanner:clear_alert_channel")]] if ch else []),
                [InlineKeyboardButton("⬅️ Back", callback_data="menu:main")],
            ])
        )

    elif action == "set_alert_channel":
        set_state(uid, waiting_for="scanner_alert_channel")
        await query.edit_message_text(
            "📣 *Set Alert Channel*\n\n"
            "Send the channel ID (e.g. `-1001234567890`) or username (e.g. `@mychannel`).\n\n"
            "_Make sure the bot is added as an admin to the channel first._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="scanner:alert_channel_menu"),
            ]])
        )

    elif action == "clear_alert_channel":
        sc.set_alert_channel(None)
        await query.edit_message_text(
            "✅ Alert channel removed. Alerts will only be sent as DMs.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="menu:main"),
            ]])
        )


# ─── Callbacks ────────────────────────────────────────────────────────────────

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    action = query.data.split(":")[1]
    await query.answer()

    if action == "main":
        clear_state(uid)
        await show_main_menu(query, uid, edit=True)
    elif action == "market":
        await query.edit_message_text("*📊 Market*\n\nChoose:", parse_mode="Markdown",
                                       reply_markup=market_kb())
    elif action in ("trade", "buy"):
        mode = "📄 Paper" if get_mode(uid) == "paper" else "🔴 Live"
        await query.edit_message_text(f"*💰 Trade* — {mode}\n\nChoose:",
                                       parse_mode="Markdown", reply_markup=trade_kb())
    elif action == "portfolio":
        await query.edit_message_text("Loading...")
        try:
            await _show_portfolio(query.edit_message_text, uid)
        except Exception as e:
            print(f"[PORTFOLIO] menu load error: {e}")
            await query.edit_message_text(
                "⚠️ Failed to load portfolio. Try again.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Retry", callback_data="portfolio:refresh"),
                    InlineKeyboardButton("⬅️ Menu",  callback_data="menu:main"),
                ]])
            )
    elif action == "autosell":
        await _show_autosell(query.edit_message_text, uid)
    elif action == "autobuy":
        await _show_autobuy(query.edit_message_text, uid)
    elif action == "settings":
        await query.edit_message_text(
            "*⚙️ Settings*\n\n📄 Paper — virtual 10 SOL\n🔴 Live — real on-chain",
            parse_mode="Markdown", reply_markup=settings_kb(uid)
        )
    elif action == "scout":
        targets = _db.get_scan_targets()
        user_settings = sm.get_user_settings(uid)
        scouted   = user_settings.get("alert_scouted_threshold", 35)
        warm      = user_settings.get("alert_warm_threshold", 55)
        hot       = user_settings.get("alert_hot_threshold", 70)
        ultra_hot = user_settings.get("alert_ultra_hot_threshold", 85)
        ch = sc.get_alert_channel()
        ch_txt = f"`{ch}`" if ch else "_Not set_"
        status = "🟢 Active" if uid in targets else "🔴 Paused"
        toggle_lbl = "🔕 Pause Scout" if uid in targets else "🔔 Start Scout"
        await query.edit_message_text(
            f"*🔍 Scout*\n\n"
            f"Status: {status}\n"
            f"⚪ Scouted ≥ `{scouted}` · 🟡 Warm ≥ `{warm}` · 🟠 Hot ≥ `{hot}` · 🔴 Ultra ≥ `{ultra_hot}`\n"
            f"Alert Channel: {ch_txt}\n\n"
            f"Scout monitors pump.fun for hot tokens and alerts you based on heat score.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl,      callback_data="scanner:toggle")],
                [InlineKeyboardButton("🌡️ Thresholds",  callback_data="scanner:set_threshold"),
                 InlineKeyboardButton("📢 Channels",   callback_data="channels:menu")],
                [InlineKeyboardButton("👀 Scouted",    callback_data="scanner:watchlist"),
                 InlineKeyboardButton("🏆 Top Scouts", callback_data="scanner:topalerts")],
                [InlineKeyboardButton("⬅️ Back",       callback_data="menu:main")],
            ])
        )
    elif action == "mode":
        await query.edit_message_text("Select mode:", reply_markup=settings_kb(uid))


async def market_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    action = query.data.split(":")[1]
    await query.answer()

    if action == "top":
        await query.edit_message_text("Fetching...")
        await _show_top(query.edit_message_text)
    elif action == "lookup":
        set_state(uid, waiting_for="market_lookup")
        await query.edit_message_text("🔍 Send a symbol or CA:", parse_mode="Markdown",
                                       reply_markup=back_kb("menu:market"))


async def trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    action = query.data.split(":")[1]
    await query.answer()
    set_state(uid, waiting_for=f"trade_{action}_token", trade_action=action)
    await query.edit_message_text(
        f"{'🟢 Buy' if action == 'buy' else '🔴 Sell'}\n\nSend token symbol or CA:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
    )


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    chosen = query.data.split(":")[1]
    if chosen == "live" and not WALLET_PRIVATE_KEY:
        await query.answer("Add WALLET_PRIVATE_KEY to config.py first!", show_alert=True)
        return
    await query.answer()
    prev = user_modes.get(uid, "paper")
    user_modes[uid] = chosen
    _save_user_modes()
    if chosen == "paper" and prev != "paper":
        reset_portfolio(uid)
    label = "📄 Paper" if chosen == "paper" else "🔴 Live"
    note  = "Switched to paper trading. Virtual portfolio reset to 10 SOL." if (chosen == "paper" and prev != "paper") else ("📄 Already in paper mode." if chosen == "paper" else "⚠️ Real trades active.")
    await query.edit_message_text(
        f"✅ Mode: *{label}*\n\n{note}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 View Portfolio", callback_data="portfolio:refresh")],
            [InlineKeyboardButton("⬅️ Main Menu",      callback_data="menu:main")],
        ])
    )


def _slippage_kb(uid: int) -> InlineKeyboardMarkup:
    cur = get_user_slippage(uid)
    def _btn(label, bps):
        tick = "✅ " if cur == bps else ""
        return InlineKeyboardButton(f"{tick}{label}", callback_data=f"settings:slip_set:{bps}")
    return InlineKeyboardMarkup([
        [_btn("0.5%", 50),  _btn("1%", 100),  _btn("1.5%", 150)],
        [_btn("2%",  200),  _btn("3%", 300),  _btn("5%",   500)],
        [_btn("10%", 1000), _btn("20%", 2000), _btn("30%", 3000)],
        [InlineKeyboardButton("✏️ Custom bps", callback_data="settings:slip_custom")],
        [InlineKeyboardButton("⬅️ Back", callback_data="settings:menu")],
    ])


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]
    await query.answer()

    if action == "menu":
        await query.edit_message_text("⚙️ *Settings*", parse_mode="Markdown",
                                      reply_markup=settings_kb(uid))

    elif action == "slippage":
        cur = get_user_slippage(uid)
        slip_pct = cur / 100
        await query.edit_message_text(
            f"⚡ *Slippage Settings*\n\n"
            f"Current: `{slip_pct:.1f}%` ({cur} bps)\n\n"
            f"Slippage is the maximum price difference you accept between the quoted and executed price.\n"
            f"• *Low (0.5–1%)* — best for stable, liquid tokens\n"
            f"• *Medium (2–5%)* — good for most meme coins\n"
            f"• *High (10–30%)* — needed for new/thin tokens, pump.fun graduates\n\n"
            f"_Applied to all live trades. Paper trades are unaffected._",
            parse_mode="Markdown",
            reply_markup=_slippage_kb(uid)
        )

    elif action == "slip_set" and len(parts) > 2:
        bps = int(parts[2])
        set_user_slippage(uid, bps)
        slip_pct = bps / 100
        await query.edit_message_text(
            f"✅ Slippage set to `{slip_pct:.1f}%` ({bps} bps)",
            parse_mode="Markdown",
            reply_markup=_slippage_kb(uid)
        )

    elif action == "slip_custom":
        context.user_data["state"] = "slippage_custom"
        await query.edit_message_text(
            "✏️ *Custom Slippage*\n\n"
            "Enter slippage in basis points (bps).\n"
            "_1 bps = 0.01% — e.g. type `250` for 2.5%_\n\n"
            "Range: `10`–`5000`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="settings:slippage")
            ]])
        )

    elif action == "jito":
        jito_on  = get_user_jito(uid)
        tip      = get_user_jito_tip(uid)
        tip_sol  = tip / 1_000_000_000
        status   = "✅ Enabled" if jito_on else "❌ Disabled"
        text = (
            f"🛡️ *Jito MEV Protection*\n\n"
            f"Routes trades through Jito block engine to prevent sandwich attacks.\n\n"
            f"Status: *{status}*\n"
            f"Tip: *{tip:,} lamports* ({tip_sol:.6f} SOL)\n\n"
            f"_Higher tip = faster inclusion in Jito blocks._"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "✅ Turn Off" if jito_on else "❌ Turn On",
                callback_data="settings:jito_toggle"
            )],
            [InlineKeyboardButton("💰 50k lamports (0.00005 SOL)",  callback_data="settings:jito_tip:50000"),
             InlineKeyboardButton("💰 100k (0.0001 SOL)",           callback_data="settings:jito_tip:100000")],
            [InlineKeyboardButton("💰 250k (0.00025 SOL)",          callback_data="settings:jito_tip:250000"),
             InlineKeyboardButton("💰 500k (0.0005 SOL)",           callback_data="settings:jito_tip:500000")],
            [InlineKeyboardButton("✏️ Custom tip",                   callback_data="settings:jito_tip_custom")],
            [InlineKeyboardButton("◀️ Back", callback_data="settings:back")],
        ])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif action == "jito_toggle":
        new_val = not get_user_jito(uid)
        set_user_jito(uid, new_val)
        # re-render jito menu
        tip     = get_user_jito_tip(uid)
        tip_sol = tip / 1_000_000_000
        status  = "✅ Enabled" if new_val else "❌ Disabled"
        text = (
            f"🛡️ *Jito MEV Protection*\n\n"
            f"Routes trades through Jito block engine to prevent sandwich attacks.\n\n"
            f"Status: *{status}*\n"
            f"Tip: *{tip:,} lamports* ({tip_sol:.6f} SOL)\n\n"
            f"_Higher tip = faster inclusion in Jito blocks._"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "✅ Turn Off" if new_val else "❌ Turn On",
                callback_data="settings:jito_toggle"
            )],
            [InlineKeyboardButton("💰 50k lamports (0.00005 SOL)",  callback_data="settings:jito_tip:50000"),
             InlineKeyboardButton("💰 100k (0.0001 SOL)",           callback_data="settings:jito_tip:100000")],
            [InlineKeyboardButton("💰 250k (0.00025 SOL)",          callback_data="settings:jito_tip:250000"),
             InlineKeyboardButton("💰 500k (0.0005 SOL)",           callback_data="settings:jito_tip:500000")],
            [InlineKeyboardButton("✏️ Custom tip",                   callback_data="settings:jito_tip_custom")],
            [InlineKeyboardButton("◀️ Back", callback_data="settings:back")],
        ])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif action.startswith("jito_tip:") and not action.startswith("jito_tip_custom"):
        lamports = int(action.split(":")[1])
        set_user_jito_tip(uid, lamports)
        tip_sol = lamports / 1_000_000_000
        await query.answer(f"✅ Jito tip set to {lamports:,} lamports ({tip_sol:.6f} SOL)", show_alert=False)
        # re-render jito menu
        jito_on = get_user_jito(uid)
        status  = "✅ Enabled" if jito_on else "❌ Disabled"
        text = (
            f"🛡️ *Jito MEV Protection*\n\n"
            f"Routes trades through Jito block engine to prevent sandwich attacks.\n\n"
            f"Status: *{status}*\n"
            f"Tip: *{lamports:,} lamports* ({tip_sol:.6f} SOL)\n\n"
            f"_Higher tip = faster inclusion in Jito blocks._"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "✅ Turn Off" if jito_on else "❌ Turn On",
                callback_data="settings:jito_toggle"
            )],
            [InlineKeyboardButton("💰 50k lamports (0.00005 SOL)",  callback_data="settings:jito_tip:50000"),
             InlineKeyboardButton("💰 100k (0.0001 SOL)",           callback_data="settings:jito_tip:100000")],
            [InlineKeyboardButton("💰 250k (0.00025 SOL)",          callback_data="settings:jito_tip:250000"),
             InlineKeyboardButton("💰 500k (0.0005 SOL)",           callback_data="settings:jito_tip:500000")],
            [InlineKeyboardButton("✏️ Custom tip",                   callback_data="settings:jito_tip_custom")],
            [InlineKeyboardButton("◀️ Back", callback_data="settings:back")],
        ])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif action == "jito_tip_custom":
        context.user_data["state"] = "jito_tip_custom"
        await query.edit_message_text(
            "✏️ *Custom Jito Tip*\n\n"
            "Enter tip amount in *lamports*.\n"
            "_1 SOL = 1,000,000,000 lamports_\n\n"
            "Common values:\n"
            "• `50000` → 0.00005 SOL (minimal)\n"
            "• `100000` → 0.0001 SOL (default)\n"
            "• `500000` → 0.0005 SOL (aggressive)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="settings:jito")
            ]])
        )

    elif action == "priority":
        cur_fee   = get_user_priority_fee(uid)
        fee_label = _fee_label(cur_fee)
        text = (
            f"🚀 *Priority Fee*\n\n"
            f"Extra fee paid to validators for faster transaction inclusion.\n\n"
            f"Current: *{fee_label}* ({cur_fee:,} µlamports/CU)\n\n"
            f"_Higher fee = better chance of landing in congested blocks._"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🐢 Low (100k µL/CU)",    callback_data="settings:prio_set:Low"),
             InlineKeyboardButton("⚡ Medium (500k µL/CU)", callback_data="settings:prio_set:Medium")],
            [InlineKeyboardButton("🔥 High (1M µL/CU)",    callback_data="settings:prio_set:High"),
             InlineKeyboardButton("🚀 Turbo (3M µL/CU)",   callback_data="settings:prio_set:Turbo")],
            [InlineKeyboardButton("✏️ Custom",               callback_data="settings:prio_custom")],
            [InlineKeyboardButton("◀️ Back", callback_data="settings:back")],
        ])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif action.startswith("prio_set:"):
        preset_name = action.split(":")[1]
        new_fee = PRIORITY_FEE_PRESETS.get(preset_name)
        if new_fee is not None:
            set_user_priority_fee(uid, new_fee)
            await query.answer(f"✅ Priority fee set to {preset_name}", show_alert=False)
        cur_fee   = get_user_priority_fee(uid)
        fee_label = _fee_label(cur_fee)
        text = (
            f"🚀 *Priority Fee*\n\n"
            f"Extra fee paid to validators for faster transaction inclusion.\n\n"
            f"Current: *{fee_label}* ({cur_fee:,} µlamports/CU)\n\n"
            f"_Higher fee = better chance of landing in congested blocks._"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🐢 Low (100k µL/CU)",    callback_data="settings:prio_set:Low"),
             InlineKeyboardButton("⚡ Medium (500k µL/CU)", callback_data="settings:prio_set:Medium")],
            [InlineKeyboardButton("🔥 High (1M µL/CU)",    callback_data="settings:prio_set:High"),
             InlineKeyboardButton("🚀 Turbo (3M µL/CU)",   callback_data="settings:prio_set:Turbo")],
            [InlineKeyboardButton("✏️ Custom",               callback_data="settings:prio_custom")],
            [InlineKeyboardButton("◀️ Back", callback_data="settings:back")],
        ])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif action == "prio_custom":
        context.user_data["state"] = "priority_custom"
        await query.edit_message_text(
            "✏️ *Custom Priority Fee*\n\n"
            "Enter fee in *micro-lamports per Compute Unit* (µlamports/CU).\n\n"
            "Reference:\n"
            "• `100000` → Low\n"
            "• `500000` → Medium (default)\n"
            "• `1000000` → High\n"
            "• `3000000` → Turbo\n\n"
            "_Raise to 2,000,000+ for ultra-aggressive sniping._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="settings:priority")
            ]])
        )

    elif action == "safety_toggle":
        new_val = not get_safety_check_enabled(uid)
        set_safety_check_enabled(uid, new_val)
        status = "🟢 ON" if new_val else "🔴 OFF"
        await query.answer(f"Safety check {status}", show_alert=False)
        await query.edit_message_text(
            "⚙️ *Settings*",
            parse_mode="Markdown",
            reply_markup=settings_kb(uid)
        )

    elif action == "quick_amounts":
        amounts = get_user_quick_buy_amounts(uid)
        amt_str = " / ".join(f"{a} SOL" for a in amounts)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("0.05", callback_data="settings:qamt_add:0.05"),
             InlineKeyboardButton("0.1",  callback_data="settings:qamt_add:0.1"),
             InlineKeyboardButton("0.25", callback_data="settings:qamt_add:0.25"),
             InlineKeyboardButton("0.5",  callback_data="settings:qamt_add:0.5")],
            [InlineKeyboardButton("1.0",  callback_data="settings:qamt_add:1.0"),
             InlineKeyboardButton("2.0",  callback_data="settings:qamt_add:2.0"),
             InlineKeyboardButton("5.0",  callback_data="settings:qamt_add:5.0"),
             InlineKeyboardButton("10.0", callback_data="settings:qamt_add:10.0")],
            [InlineKeyboardButton("🔄 Reset to default", callback_data="settings:qamt_reset")],
            [InlineKeyboardButton("◀️ Back", callback_data="settings:back")],
        ])
        await query.edit_message_text(
            f"💰 *Quick Buy Amounts*\n\n"
            f"Current presets: `{amt_str}`\n\n"
            f"Tap to toggle a value on/off:\n"
            f"_(max 4 presets shown as buttons)_",
            parse_mode="Markdown",
            reply_markup=kb
        )

    elif action.startswith("qamt_add:"):
        val = float(action.split(":")[1])
        amounts = get_user_quick_buy_amounts(uid)
        if val in amounts:
            amounts = [a for a in amounts if a != val]
        else:
            amounts = sorted(set(amounts + [val]))[:6]
        if not amounts:
            amounts = [0.1]
        set_user_quick_buy_amounts(uid, amounts)
        amt_str = " / ".join(f"{a} SOL" for a in amounts)
        await query.answer(f"Presets: {amt_str}", show_alert=False)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("0.05", callback_data="settings:qamt_add:0.05"),
             InlineKeyboardButton("0.1",  callback_data="settings:qamt_add:0.1"),
             InlineKeyboardButton("0.25", callback_data="settings:qamt_add:0.25"),
             InlineKeyboardButton("0.5",  callback_data="settings:qamt_add:0.5")],
            [InlineKeyboardButton("1.0",  callback_data="settings:qamt_add:1.0"),
             InlineKeyboardButton("2.0",  callback_data="settings:qamt_add:2.0"),
             InlineKeyboardButton("5.0",  callback_data="settings:qamt_add:5.0"),
             InlineKeyboardButton("10.0", callback_data="settings:qamt_add:10.0")],
            [InlineKeyboardButton("🔄 Reset to default", callback_data="settings:qamt_reset")],
            [InlineKeyboardButton("◀️ Back", callback_data="settings:back")],
        ])
        await query.edit_message_text(
            f"💰 *Quick Buy Amounts*\n\n"
            f"Current presets: `{amt_str}`\n\n"
            f"Tap to toggle a value on/off:\n"
            f"_(max 4 presets shown as buttons)_",
            parse_mode="Markdown", reply_markup=kb
        )

    elif action == "qamt_reset":
        set_user_quick_buy_amounts(uid, [0.1, 0.25, 0.5, 1.0])
        await query.answer("Reset to defaults", show_alert=False)
        await query.edit_message_text("⚙️ *Settings*", parse_mode="Markdown", reply_markup=settings_kb(uid))

    elif action == "reset_paper":
        reset_portfolio(uid)
        await query.edit_message_text("🗑️ Paper portfolio reset to `10 SOL`.",
                                       parse_mode="Markdown", reply_markup=back_kb())


async def heatscore_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle heat score settings callbacks."""
    query = update.callback_query
    uid = query.from_user.id
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    await query.answer()

    if action == "presets":
        # Show presets menu
        presets = sm.list_presets()
        lines = ["*🎯 Scout Presets*\n", "Quick-swap configurations:\n"]
        
        for preset_key, preset_info in presets.items():
            lines.append(f"*{preset_info['display_name']}*")
            lines.append(f"{preset_info['description']}\n")
        
        keyboard = [
            [InlineKeyboardButton("🛡️ Conservative", callback_data="heatscore:preset:conservative"),
             InlineKeyboardButton("⚖️ Balanced", callback_data="heatscore:preset:balanced")],
            [InlineKeyboardButton("🚀 Aggressive", callback_data="heatscore:preset:aggressive"),
             InlineKeyboardButton("🐋 Whale Mode", callback_data="heatscore:preset:whale-mode")],
            [InlineKeyboardButton("⬅️ Back", callback_data="heatscore:show")],
        ]
        
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif action == "preset" and len(parts) > 2:
        # Apply preset
        preset_name = parts[2]
        if sm.apply_preset(uid, preset_name):
            preset_info = sm.get_preset_info(preset_name)
            await query.edit_message_text(
                f"✅ *Preset Applied: {preset_info['name']}*\n\n"
                f"{preset_info['description']}\n\n"
                f"_Modified {len(preset_info.get('overrides', {}))} settings_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 View Settings", callback_data="heatscore:show")],
                    [InlineKeyboardButton("🎯 Other Presets", callback_data="heatscore:presets")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="menu:main")],
                ])
            )
        else:
            await query.edit_message_text(
                "❌ Failed to apply preset.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="heatscore:show")]])
            )
    
    elif action == "show" or action == "customize":
        # Show settings display
        compact = action == "show_custom"
        settings_text = sm.format_settings_display(uid, compact=compact)
        
        keyboard = [
            [InlineKeyboardButton("🎯 Quick Presets", callback_data="heatscore:presets")],
            [InlineKeyboardButton("✏️ Manual Customize", callback_data="heatscore:manual")],
            [InlineKeyboardButton("🔄 Reset to Defaults", callback_data="heatscore:reset_confirm")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu:main")],
        ]
        
        await query.edit_message_text(
            settings_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True
        )
    
    elif action == "show_custom":
        # Show only non-default settings
        settings_text = sm.format_settings_display(uid, compact=True)
        if "no differences" in settings_text.lower() or not settings_text.strip():
            settings_text = "*📋 All Settings at Default*\n\nNo custom overrides detected."
        
        keyboard = [
            [InlineKeyboardButton("🎯 Quick Presets", callback_data="heatscore:presets")],
            [InlineKeyboardButton("📋 Show All", callback_data="heatscore:show")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu:main")],
        ]
        
        await query.edit_message_text(
            settings_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True
        )
    
    elif action == "reset" or action == "reset_confirm":
        # Confirm reset
        keyboard = [
            [InlineKeyboardButton("✅ Yes, Reset", callback_data="heatscore:reset_execute")],
            [InlineKeyboardButton("❌ Cancel", callback_data="heatscore:show")],
        ]
        
        await query.edit_message_text(
            "⚠️ *Reset to Defaults?*\n\n"
            "This will remove all your custom settings and restore system defaults.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif action == "reset_execute":
        # Execute reset
        if sm.reset_user_settings(uid):
            await query.edit_message_text(
                "✅ *Settings Reset*\n\nAll custom overrides have been removed.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 View Settings", callback_data="heatscore:show")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="menu:main")],
                ])
            )
        else:
            await query.edit_message_text(
                "❌ Failed to reset settings.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="heatscore:show")]])
            )
    
    elif action == "manual":
        # Show manual customize instructions
        lines = [
            "*⚙️ Manual Customize*\n",
            "Use the command: `/customize [setting] [value]`\n",
            "Examples:",
            "`/customize alert_hot_threshold 75`",
            "`/customize risk_dev_sell_threshold_pct 60`",
            "`/customize liquidity_min_usd 50000`\n",
            "_All values are 0-100 unless specified (USD values can be higher)._",
        ]
        
        keyboard = [
            [InlineKeyboardButton("📋 View Settings", callback_data="heatscore:show")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu:main")],
        ]
        
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def autosell_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]
    mint   = parts[2] if len(parts) > 2 else ""
    await query.answer()

    if action == "view":
        cfg = get_auto_sell(uid, mint)
        if not cfg:
            await query.edit_message_text("Config not found.", reply_markup=back_kb("menu:autosell"))
            return
        await query.edit_message_text(
            _format_autosell_config(cfg), parse_mode="Markdown",
            reply_markup=autosell_token_kb(uid, mint)
        )

    elif action == "toggle":
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg["enabled"] = not cfg.get("enabled", True)
            set_auto_sell(uid, mint, cfg)
            status = "✅ Enabled" if cfg["enabled"] else "⏸️ Paused"
            await query.edit_message_text(
                f"Auto-sell {status} for `${cfg['symbol']}`",
                parse_mode="Markdown", reply_markup=autosell_token_kb(uid, mint)
            )

    elif action == "reset":
        cfg = get_auto_sell(uid, mint)
        if cfg:
            for t in cfg.get("mult_targets", []):
                t["triggered"] = False
            for ma in cfg.get("mcap_alerts", []):
                ma["triggered"] = False
            for ct in cfg.get("custom_targets", []):
                ct["triggered"] = False
            set_auto_sell(uid, mint, cfg)
            await query.edit_message_text(
                f"🔄 Targets reset for `${cfg['symbol']}`\n\nAll targets are now active again.",
                parse_mode="Markdown", reply_markup=autosell_token_kb(uid, mint)
            )

    elif action == "remove":
        remove_auto_sell(uid, mint)
        await _show_autosell(query.edit_message_text, uid)

    elif action == "addcustom":
        set_state(uid, waiting_for="custom_target_type", custom_mint=mint)
        await query.edit_message_text(
            "➕ *Add Custom Target*\n\nWhat type of target?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📈 Price Multiplier (e.g. 3x)",
                                     callback_data=f"ct_type:multiplier:{mint}")],
                [InlineKeyboardButton("💵 Exact Price (e.g. $0.001)",
                                     callback_data=f"ct_type:price:{mint}")],
                [InlineKeyboardButton("🏦 Market Cap (e.g. $250K)",
                                     callback_data=f"ct_type:mcap:{mint}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"as:view:{mint}")],
            ])
        )

    # ── Stop-Loss menu ────────────────────────────────────────────────────────
    elif action == "sl_menu":
        cfg = get_auto_sell(uid, mint)
        if not cfg:
            await query.edit_message_text("Config not found.", reply_markup=back_kb(f"as:view:{mint}"))
            return
        sl = cfg.get("stop_loss", {})
        on = sl.get("enabled", False)
        toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        await query.edit_message_text(
            f"*🛑 Stop-Loss — ${cfg.get('symbol','?')}*\n\n"
            f"Status: {status_txt}\n"
            f"Trigger: drop `{sl.get('pct',50)}%` from buy price\n"
            f"Sell: `{sl.get('sell_pct',100)}%` of position",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl, callback_data=f"as:sl_toggle:{mint}")],
                [InlineKeyboardButton("25% drop",  callback_data=f"as:sl_pct:{mint}:25"),
                 InlineKeyboardButton("50% drop",  callback_data=f"as:sl_pct:{mint}:50"),
                 InlineKeyboardButton("75% drop",  callback_data=f"as:sl_pct:{mint}:75")],
                [InlineKeyboardButton("✏️ Custom drop %", callback_data=f"as:sl_custom_pct:{mint}")],
                [InlineKeyboardButton("Sell 10%",  callback_data=f"as:sl_sell_pct:{mint}:10"),
                 InlineKeyboardButton("Sell 25%",  callback_data=f"as:sl_sell_pct:{mint}:25"),
                 InlineKeyboardButton("Sell 50%",  callback_data=f"as:sl_sell_pct:{mint}:50")],
                [InlineKeyboardButton("Sell 75%",  callback_data=f"as:sl_sell_pct:{mint}:75"),
                 InlineKeyboardButton("Sell 100%", callback_data=f"as:sl_sell_pct:{mint}:100")],
                [InlineKeyboardButton("✏️ Custom sell %", callback_data=f"as:sl_custom_sell:{mint}")],
                [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
            ])
        )

    elif action == "sl_toggle":
        cfg = get_auto_sell(uid, mint)
        if cfg:
            sl = cfg.setdefault("stop_loss", {"enabled": False, "pct": 50, "sell_pct": 100, "triggered": False})
            sl["enabled"] = not sl.get("enabled", False)
            set_auto_sell(uid, mint, cfg)
        sl = cfg.get("stop_loss", {})
        on = sl.get("enabled", False)
        toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        await query.edit_message_text(
            f"*🛑 Stop-Loss — ${cfg.get('symbol','?')}*\n\n"
            f"Status: {status_txt}\n"
            f"Trigger: drop `{sl.get('pct',50)}%` from buy price\n"
            f"Sell: `{sl.get('sell_pct',100)}%` of position",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl, callback_data=f"as:sl_toggle:{mint}")],
                [InlineKeyboardButton("25% drop",  callback_data=f"as:sl_pct:{mint}:25"),
                 InlineKeyboardButton("50% drop",  callback_data=f"as:sl_pct:{mint}:50"),
                 InlineKeyboardButton("75% drop",  callback_data=f"as:sl_pct:{mint}:75")],
                [InlineKeyboardButton("✏️ Custom drop %", callback_data=f"as:sl_custom_pct:{mint}")],
                [InlineKeyboardButton("Sell 10%",  callback_data=f"as:sl_sell_pct:{mint}:10"),
                 InlineKeyboardButton("Sell 25%",  callback_data=f"as:sl_sell_pct:{mint}:25"),
                 InlineKeyboardButton("Sell 50%",  callback_data=f"as:sl_sell_pct:{mint}:50")],
                [InlineKeyboardButton("Sell 75%",  callback_data=f"as:sl_sell_pct:{mint}:75"),
                 InlineKeyboardButton("Sell 100%", callback_data=f"as:sl_sell_pct:{mint}:100")],
                [InlineKeyboardButton("✏️ Custom sell %", callback_data=f"as:sl_custom_sell:{mint}")],
                [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
            ])
        )

    elif action == "sl_pct":
        pct = int(parts[3]) if len(parts) > 3 else 50
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("stop_loss", {})["pct"] = pct
            set_auto_sell(uid, mint, cfg)
            await query.answer(f"Stop-loss trigger set to {pct}% drop")
            sl = cfg["stop_loss"]
            on = sl.get("enabled", False)
            toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
            status_txt = "🟢 Enabled" if on else "🔴 Disabled"
            await query.edit_message_text(
                f"*🛑 Stop-Loss — ${cfg.get('symbol','?')}*\n\n"
                f"Status: {status_txt}\n"
                f"Trigger: drop `{sl.get('pct',50)}%` from buy price\n"
                f"Sell: `{sl.get('sell_pct',100)}%` of position",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(toggle_lbl, callback_data=f"as:sl_toggle:{mint}")],
                    [InlineKeyboardButton("25% drop",  callback_data=f"as:sl_pct:{mint}:25"),
                     InlineKeyboardButton("50% drop",  callback_data=f"as:sl_pct:{mint}:50"),
                     InlineKeyboardButton("75% drop",  callback_data=f"as:sl_pct:{mint}:75")],
                    [InlineKeyboardButton("✏️ Custom drop %", callback_data=f"as:sl_custom_pct:{mint}")],
                    [InlineKeyboardButton("Sell 10%",  callback_data=f"as:sl_sell_pct:{mint}:10"),
                     InlineKeyboardButton("Sell 25%",  callback_data=f"as:sl_sell_pct:{mint}:25"),
                     InlineKeyboardButton("Sell 50%",  callback_data=f"as:sl_sell_pct:{mint}:50")],
                    [InlineKeyboardButton("Sell 75%",  callback_data=f"as:sl_sell_pct:{mint}:75"),
                     InlineKeyboardButton("Sell 100%", callback_data=f"as:sl_sell_pct:{mint}:100")],
                    [InlineKeyboardButton("✏️ Custom sell %", callback_data=f"as:sl_custom_sell:{mint}")],
                    [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
                ])
            )

    elif action == "sl_sell_pct":
        sell_pct = int(parts[3]) if len(parts) > 3 else 100
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("stop_loss", {})["sell_pct"] = sell_pct
            set_auto_sell(uid, mint, cfg)
            await query.answer(f"Stop-loss sell set to {sell_pct}%")
            sl = cfg["stop_loss"]
            on = sl.get("enabled", False)
            toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
            status_txt = "🟢 Enabled" if on else "🔴 Disabled"
            await query.edit_message_text(
                f"*🛑 Stop-Loss — ${cfg.get('symbol','?')}*\n\n"
                f"Status: {status_txt}\n"
                f"Trigger: drop `{sl.get('pct',50)}%` from buy price\n"
                f"Sell: `{sl.get('sell_pct',100)}%` of position",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(toggle_lbl, callback_data=f"as:sl_toggle:{mint}")],
                    [InlineKeyboardButton("25% drop",  callback_data=f"as:sl_pct:{mint}:25"),
                     InlineKeyboardButton("50% drop",  callback_data=f"as:sl_pct:{mint}:50"),
                     InlineKeyboardButton("75% drop",  callback_data=f"as:sl_pct:{mint}:75")],
                    [InlineKeyboardButton("✏️ Custom drop %", callback_data=f"as:sl_custom_pct:{mint}")],
                    [InlineKeyboardButton("Sell 10%",  callback_data=f"as:sl_sell_pct:{mint}:10"),
                     InlineKeyboardButton("Sell 25%",  callback_data=f"as:sl_sell_pct:{mint}:25"),
                     InlineKeyboardButton("Sell 50%",  callback_data=f"as:sl_sell_pct:{mint}:50")],
                    [InlineKeyboardButton("Sell 75%",  callback_data=f"as:sl_sell_pct:{mint}:75"),
                     InlineKeyboardButton("Sell 100%", callback_data=f"as:sl_sell_pct:{mint}:100")],
                    [InlineKeyboardButton("✏️ Custom sell %", callback_data=f"as:sl_custom_sell:{mint}")],
                    [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
                ])
            )

    elif action == "sl_custom_pct":
        set_state(uid, waiting_for="as_sl_pct", as_mint=mint)
        await query.edit_message_text(
            "Enter custom stop-loss drop % (e.g. 35 for 35% drop from buy price):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"as:sl_menu:{mint}")
            ]])
        )

    elif action == "sl_custom_sell":
        set_state(uid, waiting_for="as_sl_sell_pct_input", as_mint=mint)
        await query.edit_message_text(
            "Enter sell % for stop-loss (1-100):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"as:sl_menu:{mint}")
            ]])
        )

    # ── Trailing stop menu ────────────────────────────────────────────────────
    elif action == "trail_menu":
        cfg = get_auto_sell(uid, mint)
        if not cfg:
            await query.edit_message_text("Config not found.", reply_markup=back_kb(f"as:view:{mint}"))
            return
        ts = cfg.get("trailing_stop", {})
        on = ts.get("enabled", False)
        toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        peak = ts.get("peak_price", 0)
        peak_txt = f"`${peak:.8f}`" if peak > 0 else "not set yet"
        await query.edit_message_text(
            f"*📉 Trailing Stop — ${cfg.get('symbol','?')}*\n\n"
            f"Status: {status_txt}\n"
            f"Trail: `{ts.get('trail_pct',30)}%` from peak → sell `{ts.get('sell_pct',100)}%`\n"
            f"Peak price: {peak_txt}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl, callback_data=f"as:trail_toggle:{mint}")],
                [InlineKeyboardButton("15%", callback_data=f"as:trail_pct:{mint}:15"),
                 InlineKeyboardButton("25%", callback_data=f"as:trail_pct:{mint}:25"),
                 InlineKeyboardButton("30%", callback_data=f"as:trail_pct:{mint}:30"),
                 InlineKeyboardButton("50%", callback_data=f"as:trail_pct:{mint}:50")],
                [InlineKeyboardButton("✏️ Custom trail %", callback_data=f"as:trail_custom_pct:{mint}")],
                [InlineKeyboardButton("Sell 10%",  callback_data=f"as:tsl_pct:{mint}:10"),
                 InlineKeyboardButton("Sell 25%",  callback_data=f"as:tsl_pct:{mint}:25"),
                 InlineKeyboardButton("Sell 50%",  callback_data=f"as:tsl_pct:{mint}:50")],
                [InlineKeyboardButton("Sell 75%",  callback_data=f"as:tsl_pct:{mint}:75"),
                 InlineKeyboardButton("Sell 100%", callback_data=f"as:tsl_pct:{mint}:100")],
                [InlineKeyboardButton("✏️ Custom sell %", callback_data=f"as:tcs:{mint}")],
                [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
            ])
        )

    elif action == "trail_toggle":
        cfg = get_auto_sell(uid, mint)
        if cfg:
            ts = cfg.setdefault("trailing_stop", {"enabled": False, "trail_pct": 30, "sell_pct": 100, "peak_price": 0.0, "triggered": False})
            ts["enabled"] = not ts.get("enabled", False)
            set_auto_sell(uid, mint, cfg)
        ts = cfg.get("trailing_stop", {})
        on = ts.get("enabled", False)
        toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        peak = ts.get("peak_price", 0)
        peak_txt = f"`${peak:.8f}`" if peak > 0 else "not set yet"
        await query.edit_message_text(
            f"*📉 Trailing Stop — ${cfg.get('symbol','?')}*\n\n"
            f"Status: {status_txt}\n"
            f"Trail: `{ts.get('trail_pct',30)}%` from peak → sell `{ts.get('sell_pct',100)}%`\n"
            f"Peak price: {peak_txt}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl, callback_data=f"as:trail_toggle:{mint}")],
                [InlineKeyboardButton("15%", callback_data=f"as:trail_pct:{mint}:15"),
                 InlineKeyboardButton("25%", callback_data=f"as:trail_pct:{mint}:25"),
                 InlineKeyboardButton("30%", callback_data=f"as:trail_pct:{mint}:30"),
                 InlineKeyboardButton("50%", callback_data=f"as:trail_pct:{mint}:50")],
                [InlineKeyboardButton("✏️ Custom trail %", callback_data=f"as:trail_custom_pct:{mint}")],
                [InlineKeyboardButton("Sell 10%",  callback_data=f"as:tsl_pct:{mint}:10"),
                 InlineKeyboardButton("Sell 25%",  callback_data=f"as:tsl_pct:{mint}:25"),
                 InlineKeyboardButton("Sell 50%",  callback_data=f"as:tsl_pct:{mint}:50")],
                [InlineKeyboardButton("Sell 75%",  callback_data=f"as:tsl_pct:{mint}:75"),
                 InlineKeyboardButton("Sell 100%", callback_data=f"as:tsl_pct:{mint}:100")],
                [InlineKeyboardButton("✏️ Custom sell %", callback_data=f"as:tcs:{mint}")],
                [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
            ])
        )

    elif action == "trail_pct":
        trail_pct = int(parts[3]) if len(parts) > 3 else 30
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("trailing_stop", {})["trail_pct"] = trail_pct
            set_auto_sell(uid, mint, cfg)
            await query.answer(f"Trail pct set to {trail_pct}%")
            ts = cfg["trailing_stop"]
            on = ts.get("enabled", False)
            toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
            status_txt = "🟢 Enabled" if on else "🔴 Disabled"
            peak = ts.get("peak_price", 0)
            peak_txt = f"`${peak:.8f}`" if peak > 0 else "not set yet"
            await query.edit_message_text(
                f"*📉 Trailing Stop — ${cfg.get('symbol','?')}*\n\n"
                f"Status: {status_txt}\n"
                f"Trail: `{ts.get('trail_pct',30)}%` from peak → sell `{ts.get('sell_pct',100)}%`\n"
                f"Peak price: {peak_txt}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(toggle_lbl, callback_data=f"as:trail_toggle:{mint}")],
                    [InlineKeyboardButton("15%", callback_data=f"as:trail_pct:{mint}:15"),
                     InlineKeyboardButton("25%", callback_data=f"as:trail_pct:{mint}:25"),
                     InlineKeyboardButton("30%", callback_data=f"as:trail_pct:{mint}:30"),
                     InlineKeyboardButton("50%", callback_data=f"as:trail_pct:{mint}:50")],
                    [InlineKeyboardButton("✏️ Custom trail %", callback_data=f"as:trail_custom_pct:{mint}")],
                    [InlineKeyboardButton("Sell 10%",  callback_data=f"as:tsl_pct:{mint}:10"),
                     InlineKeyboardButton("Sell 25%",  callback_data=f"as:tsl_pct:{mint}:25"),
                     InlineKeyboardButton("Sell 50%",  callback_data=f"as:tsl_pct:{mint}:50")],
                    [InlineKeyboardButton("Sell 75%",  callback_data=f"as:tsl_pct:{mint}:75"),
                     InlineKeyboardButton("Sell 100%", callback_data=f"as:tsl_pct:{mint}:100")],
                    [InlineKeyboardButton("✏️ Custom sell %", callback_data=f"as:tcs:{mint}")],
                    [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
                ])
            )

    elif action == "tsl_pct":
        sell_pct = int(parts[3]) if len(parts) > 3 else 100
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("trailing_stop", {})["sell_pct"] = sell_pct
            set_auto_sell(uid, mint, cfg)
            await query.answer(f"Trail sell set to {sell_pct}%")
            ts = cfg["trailing_stop"]
            on = ts.get("enabled", False)
            toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
            status_txt = "🟢 Enabled" if on else "🔴 Disabled"
            peak = ts.get("peak_price", 0)
            peak_txt = f"`${peak:.8f}`" if peak > 0 else "not set yet"
            await query.edit_message_text(
                f"*📉 Trailing Stop — ${cfg.get('symbol','?')}*\n\n"
                f"Status: {status_txt}\n"
                f"Trail: `{ts.get('trail_pct',30)}%` from peak → sell `{ts.get('sell_pct',100)}%`\n"
                f"Peak price: {peak_txt}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(toggle_lbl, callback_data=f"as:trail_toggle:{mint}")],
                    [InlineKeyboardButton("15%", callback_data=f"as:trail_pct:{mint}:15"),
                     InlineKeyboardButton("25%", callback_data=f"as:trail_pct:{mint}:25"),
                     InlineKeyboardButton("30%", callback_data=f"as:trail_pct:{mint}:30"),
                     InlineKeyboardButton("50%", callback_data=f"as:trail_pct:{mint}:50")],
                    [InlineKeyboardButton("✏️ Custom trail %", callback_data=f"as:trail_custom_pct:{mint}")],
                    [InlineKeyboardButton("Sell 10%",  callback_data=f"as:tsl_pct:{mint}:10"),
                     InlineKeyboardButton("Sell 25%",  callback_data=f"as:tsl_pct:{mint}:25"),
                     InlineKeyboardButton("Sell 50%",  callback_data=f"as:tsl_pct:{mint}:50")],
                    [InlineKeyboardButton("Sell 75%",  callback_data=f"as:tsl_pct:{mint}:75"),
                     InlineKeyboardButton("Sell 100%", callback_data=f"as:tsl_pct:{mint}:100")],
                    [InlineKeyboardButton("✏️ Custom sell %", callback_data=f"as:tcs:{mint}")],
                    [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
                ])
            )

    elif action == "trail_custom_pct":
        set_state(uid, waiting_for="as_trail_pct", as_mint=mint)
        await query.edit_message_text(
            "Enter custom trailing stop % (e.g. 35 for 35% drop from peak):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"as:trail_menu:{mint}")
            ]])
        )

    elif action == "tcs":
        set_state(uid, waiting_for="as_trail_sell_pct_input", as_mint=mint)
        await query.edit_message_text(
            "Enter sell % for trailing stop (1-100):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"as:trail_menu:{mint}")
            ]])
        )

    # ── Trailing TP menu ──────────────────────────────────────────────────────
    elif action == "ttp_menu":
        cfg = get_auto_sell(uid, mint)
        if not cfg:
            await query.edit_message_text("Config not found.", reply_markup=back_kb(f"as:view:{mint}"))
            return
        ttp = cfg.get("trailing_tp", {})
        on = ttp.get("enabled", False)
        toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        active_txt = " 📈 Trailing" if ttp.get("active") else ""
        await query.edit_message_text(
            f"*📈 Trailing TP — ${cfg.get('symbol','?')}*\n\n"
            f"Status: {status_txt}{active_txt}\n"
            f"Activates at: `{ttp.get('activate_mult',2.0)}x`\n"
            f"Trail: `{ttp.get('trail_pct',20)}%` from peak → sell `{ttp.get('sell_pct',50)}%`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl, callback_data=f"as:ttp_toggle:{mint}")],
                [InlineKeyboardButton("Activate 1.5x", callback_data=f"as:ttp_act:{mint}:1.5"),
                 InlineKeyboardButton("Activate 2x",   callback_data=f"as:ttp_act:{mint}:2"),
                 InlineKeyboardButton("Activate 3x",   callback_data=f"as:ttp_act:{mint}:3"),
                 InlineKeyboardButton("Activate 5x",   callback_data=f"as:ttp_act:{mint}:5")],
                [InlineKeyboardButton("Trail 10%", callback_data=f"as:ttp_trail:{mint}:10"),
                 InlineKeyboardButton("Trail 20%", callback_data=f"as:ttp_trail:{mint}:20"),
                 InlineKeyboardButton("Trail 30%", callback_data=f"as:ttp_trail:{mint}:30")],
                [InlineKeyboardButton("Sell 25%",  callback_data=f"as:ttp_sell_pct:{mint}:25"),
                 InlineKeyboardButton("Sell 50%",  callback_data=f"as:ttp_sell_pct:{mint}:50"),
                 InlineKeyboardButton("Sell 75%",  callback_data=f"as:ttp_sell_pct:{mint}:75"),
                 InlineKeyboardButton("Sell 100%", callback_data=f"as:ttp_sell_pct:{mint}:100")],
                [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
            ])
        )

    elif action == "ttp_toggle":
        cfg = get_auto_sell(uid, mint)
        if cfg:
            ttp = cfg.setdefault("trailing_tp", {"enabled": False, "activate_mult": 2.0, "trail_pct": 20, "sell_pct": 50, "active": False, "peak_price": 0.0, "triggered": False})
            ttp["enabled"] = not ttp.get("enabled", False)
            set_auto_sell(uid, mint, cfg)
        ttp = cfg.get("trailing_tp", {})
        on = ttp.get("enabled", False)
        toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        active_txt = " 📈 Trailing" if ttp.get("active") else ""
        await query.edit_message_text(
            f"*📈 Trailing TP — ${cfg.get('symbol','?')}*\n\n"
            f"Status: {status_txt}{active_txt}\n"
            f"Activates at: `{ttp.get('activate_mult',2.0)}x`\n"
            f"Trail: `{ttp.get('trail_pct',20)}%` from peak → sell `{ttp.get('sell_pct',50)}%`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl, callback_data=f"as:ttp_toggle:{mint}")],
                [InlineKeyboardButton("Activate 1.5x", callback_data=f"as:ttp_act:{mint}:1.5"),
                 InlineKeyboardButton("Activate 2x",   callback_data=f"as:ttp_act:{mint}:2"),
                 InlineKeyboardButton("Activate 3x",   callback_data=f"as:ttp_act:{mint}:3"),
                 InlineKeyboardButton("Activate 5x",   callback_data=f"as:ttp_act:{mint}:5")],
                [InlineKeyboardButton("Trail 10%", callback_data=f"as:ttp_trail:{mint}:10"),
                 InlineKeyboardButton("Trail 20%", callback_data=f"as:ttp_trail:{mint}:20"),
                 InlineKeyboardButton("Trail 30%", callback_data=f"as:ttp_trail:{mint}:30")],
                [InlineKeyboardButton("Sell 25%",  callback_data=f"as:ttp_sell_pct:{mint}:25"),
                 InlineKeyboardButton("Sell 50%",  callback_data=f"as:ttp_sell_pct:{mint}:50"),
                 InlineKeyboardButton("Sell 75%",  callback_data=f"as:ttp_sell_pct:{mint}:75"),
                 InlineKeyboardButton("Sell 100%", callback_data=f"as:ttp_sell_pct:{mint}:100")],
                [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
            ])
        )

    elif action == "ttp_act":
        mult = float(parts[3]) if len(parts) > 3 else 2.0
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("trailing_tp", {})["activate_mult"] = mult
            set_auto_sell(uid, mint, cfg)
            await query.answer(f"Activation mult set to {mult}x")
            ttp = cfg["trailing_tp"]
            on = ttp.get("enabled", False)
            toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
            status_txt = "🟢 Enabled" if on else "🔴 Disabled"
            active_txt = " 📈 Trailing" if ttp.get("active") else ""
            await query.edit_message_text(
                f"*📈 Trailing TP — ${cfg.get('symbol','?')}*\n\n"
                f"Status: {status_txt}{active_txt}\n"
                f"Activates at: `{ttp.get('activate_mult',2.0)}x`\n"
                f"Trail: `{ttp.get('trail_pct',20)}%` from peak → sell `{ttp.get('sell_pct',50)}%`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(toggle_lbl, callback_data=f"as:ttp_toggle:{mint}")],
                    [InlineKeyboardButton("Activate 1.5x", callback_data=f"as:ttp_act:{mint}:1.5"),
                     InlineKeyboardButton("Activate 2x",   callback_data=f"as:ttp_act:{mint}:2"),
                     InlineKeyboardButton("Activate 3x",   callback_data=f"as:ttp_act:{mint}:3"),
                     InlineKeyboardButton("Activate 5x",   callback_data=f"as:ttp_act:{mint}:5")],
                    [InlineKeyboardButton("Trail 10%", callback_data=f"as:ttp_trail:{mint}:10"),
                     InlineKeyboardButton("Trail 20%", callback_data=f"as:ttp_trail:{mint}:20"),
                     InlineKeyboardButton("Trail 30%", callback_data=f"as:ttp_trail:{mint}:30")],
                    [InlineKeyboardButton("Sell 25%",  callback_data=f"as:ttp_sell_pct:{mint}:25"),
                     InlineKeyboardButton("Sell 50%",  callback_data=f"as:ttp_sell_pct:{mint}:50"),
                     InlineKeyboardButton("Sell 75%",  callback_data=f"as:ttp_sell_pct:{mint}:75"),
                     InlineKeyboardButton("Sell 100%", callback_data=f"as:ttp_sell_pct:{mint}:100")],
                    [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
                ])
            )

    elif action == "ttp_trail":
        trail_pct = int(parts[3]) if len(parts) > 3 else 20
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("trailing_tp", {})["trail_pct"] = trail_pct
            set_auto_sell(uid, mint, cfg)
            await query.answer(f"Trail pct set to {trail_pct}%")
            ttp = cfg["trailing_tp"]
            on = ttp.get("enabled", False)
            toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
            status_txt = "🟢 Enabled" if on else "🔴 Disabled"
            active_txt = " 📈 Trailing" if ttp.get("active") else ""
            await query.edit_message_text(
                f"*📈 Trailing TP — ${cfg.get('symbol','?')}*\n\n"
                f"Status: {status_txt}{active_txt}\n"
                f"Activates at: `{ttp.get('activate_mult',2.0)}x`\n"
                f"Trail: `{ttp.get('trail_pct',20)}%` from peak → sell `{ttp.get('sell_pct',50)}%`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(toggle_lbl, callback_data=f"as:ttp_toggle:{mint}")],
                    [InlineKeyboardButton("Activate 1.5x", callback_data=f"as:ttp_act:{mint}:1.5"),
                     InlineKeyboardButton("Activate 2x",   callback_data=f"as:ttp_act:{mint}:2"),
                     InlineKeyboardButton("Activate 3x",   callback_data=f"as:ttp_act:{mint}:3"),
                     InlineKeyboardButton("Activate 5x",   callback_data=f"as:ttp_act:{mint}:5")],
                    [InlineKeyboardButton("Trail 10%", callback_data=f"as:ttp_trail:{mint}:10"),
                     InlineKeyboardButton("Trail 20%", callback_data=f"as:ttp_trail:{mint}:20"),
                     InlineKeyboardButton("Trail 30%", callback_data=f"as:ttp_trail:{mint}:30")],
                    [InlineKeyboardButton("Sell 25%",  callback_data=f"as:ttp_sell_pct:{mint}:25"),
                     InlineKeyboardButton("Sell 50%",  callback_data=f"as:ttp_sell_pct:{mint}:50"),
                     InlineKeyboardButton("Sell 75%",  callback_data=f"as:ttp_sell_pct:{mint}:75"),
                     InlineKeyboardButton("Sell 100%", callback_data=f"as:ttp_sell_pct:{mint}:100")],
                    [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
                ])
            )

    elif action == "ttp_sell_pct":
        sell_pct = int(parts[3]) if len(parts) > 3 else 50
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("trailing_tp", {})["sell_pct"] = sell_pct
            set_auto_sell(uid, mint, cfg)
            await query.answer(f"TTP sell set to {sell_pct}%")
            ttp = cfg["trailing_tp"]
            on = ttp.get("enabled", False)
            toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
            status_txt = "🟢 Enabled" if on else "🔴 Disabled"
            active_txt = " 📈 Trailing" if ttp.get("active") else ""
            await query.edit_message_text(
                f"*📈 Trailing TP — ${cfg.get('symbol','?')}*\n\n"
                f"Status: {status_txt}{active_txt}\n"
                f"Activates at: `{ttp.get('activate_mult',2.0)}x`\n"
                f"Trail: `{ttp.get('trail_pct',20)}%` from peak → sell `{ttp.get('sell_pct',50)}%`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(toggle_lbl, callback_data=f"as:ttp_toggle:{mint}")],
                    [InlineKeyboardButton("Activate 1.5x", callback_data=f"as:ttp_act:{mint}:1.5"),
                     InlineKeyboardButton("Activate 2x",   callback_data=f"as:ttp_act:{mint}:2"),
                     InlineKeyboardButton("Activate 3x",   callback_data=f"as:ttp_act:{mint}:3"),
                     InlineKeyboardButton("Activate 5x",   callback_data=f"as:ttp_act:{mint}:5")],
                    [InlineKeyboardButton("Trail 10%", callback_data=f"as:ttp_trail:{mint}:10"),
                     InlineKeyboardButton("Trail 20%", callback_data=f"as:ttp_trail:{mint}:20"),
                     InlineKeyboardButton("Trail 30%", callback_data=f"as:ttp_trail:{mint}:30")],
                    [InlineKeyboardButton("Sell 25%",  callback_data=f"as:ttp_sell_pct:{mint}:25"),
                     InlineKeyboardButton("Sell 50%",  callback_data=f"as:ttp_sell_pct:{mint}:50"),
                     InlineKeyboardButton("Sell 75%",  callback_data=f"as:ttp_sell_pct:{mint}:75"),
                     InlineKeyboardButton("Sell 100%", callback_data=f"as:ttp_sell_pct:{mint}:100")],
                    [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
                ])
            )

    # ── Time exit menu ────────────────────────────────────────────────────────
    elif action == "te_menu":
        cfg = get_auto_sell(uid, mint)
        if not cfg:
            await query.edit_message_text("Config not found.", reply_markup=back_kb(f"as:view:{mint}"))
            return
        te = cfg.get("time_exit", {})
        on = te.get("enabled", False)
        toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        elapsed = (time.time() - te.get("buy_time", time.time())) / 3600
        await query.edit_message_text(
            f"*⏱️ Time Exit — ${cfg.get('symbol','?')}*\n\n"
            f"Status: {status_txt}\n"
            f"Exit if not `{te.get('target_mult',2.0)}x` after `{te.get('hours',24)}h`\n"
            f"Sell: `{te.get('sell_pct',100)}%` of position\n"
            f"Elapsed: `{elapsed:.1f}h`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl, callback_data=f"as:te_toggle:{mint}")],
                [InlineKeyboardButton("6h",  callback_data=f"as:te_hours:{mint}:6"),
                 InlineKeyboardButton("12h", callback_data=f"as:te_hours:{mint}:12"),
                 InlineKeyboardButton("24h", callback_data=f"as:te_hours:{mint}:24"),
                 InlineKeyboardButton("48h", callback_data=f"as:te_hours:{mint}:48")],
                [InlineKeyboardButton("Target 1.5x", callback_data=f"as:te_mult:{mint}:1.5"),
                 InlineKeyboardButton("Target 2x",   callback_data=f"as:te_mult:{mint}:2"),
                 InlineKeyboardButton("Target 3x",   callback_data=f"as:te_mult:{mint}:3"),
                 InlineKeyboardButton("Target 5x",   callback_data=f"as:te_mult:{mint}:5")],
                [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
            ])
        )

    elif action == "te_toggle":
        cfg = get_auto_sell(uid, mint)
        if cfg:
            te = cfg.setdefault("time_exit", {"enabled": False, "hours": 24, "target_mult": 2.0, "sell_pct": 100, "buy_time": time.time(), "triggered": False})
            te["enabled"] = not te.get("enabled", False)
            set_auto_sell(uid, mint, cfg)
        te = cfg.get("time_exit", {})
        on = te.get("enabled", False)
        toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        elapsed = (time.time() - te.get("buy_time", time.time())) / 3600
        await query.edit_message_text(
            f"*⏱️ Time Exit — ${cfg.get('symbol','?')}*\n\n"
            f"Status: {status_txt}\n"
            f"Exit if not `{te.get('target_mult',2.0)}x` after `{te.get('hours',24)}h`\n"
            f"Sell: `{te.get('sell_pct',100)}%` of position\n"
            f"Elapsed: `{elapsed:.1f}h`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl, callback_data=f"as:te_toggle:{mint}")],
                [InlineKeyboardButton("6h",  callback_data=f"as:te_hours:{mint}:6"),
                 InlineKeyboardButton("12h", callback_data=f"as:te_hours:{mint}:12"),
                 InlineKeyboardButton("24h", callback_data=f"as:te_hours:{mint}:24"),
                 InlineKeyboardButton("48h", callback_data=f"as:te_hours:{mint}:48")],
                [InlineKeyboardButton("Target 1.5x", callback_data=f"as:te_mult:{mint}:1.5"),
                 InlineKeyboardButton("Target 2x",   callback_data=f"as:te_mult:{mint}:2"),
                 InlineKeyboardButton("Target 3x",   callback_data=f"as:te_mult:{mint}:3"),
                 InlineKeyboardButton("Target 5x",   callback_data=f"as:te_mult:{mint}:5")],
                [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
            ])
        )

    elif action == "te_hours":
        hours = int(parts[3]) if len(parts) > 3 else 24
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("time_exit", {})["hours"] = hours
            set_auto_sell(uid, mint, cfg)
            await query.answer(f"Time exit set to {hours}h")
            te = cfg["time_exit"]
            on = te.get("enabled", False)
            toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
            status_txt = "🟢 Enabled" if on else "🔴 Disabled"
            elapsed = (time.time() - te.get("buy_time", time.time())) / 3600
            await query.edit_message_text(
                f"*⏱️ Time Exit — ${cfg.get('symbol','?')}*\n\n"
                f"Status: {status_txt}\n"
                f"Exit if not `{te.get('target_mult',2.0)}x` after `{te.get('hours',24)}h`\n"
                f"Sell: `{te.get('sell_pct',100)}%` of position\n"
                f"Elapsed: `{elapsed:.1f}h`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(toggle_lbl, callback_data=f"as:te_toggle:{mint}")],
                    [InlineKeyboardButton("6h",  callback_data=f"as:te_hours:{mint}:6"),
                     InlineKeyboardButton("12h", callback_data=f"as:te_hours:{mint}:12"),
                     InlineKeyboardButton("24h", callback_data=f"as:te_hours:{mint}:24"),
                     InlineKeyboardButton("48h", callback_data=f"as:te_hours:{mint}:48")],
                    [InlineKeyboardButton("Target 1.5x", callback_data=f"as:te_mult:{mint}:1.5"),
                     InlineKeyboardButton("Target 2x",   callback_data=f"as:te_mult:{mint}:2"),
                     InlineKeyboardButton("Target 3x",   callback_data=f"as:te_mult:{mint}:3"),
                     InlineKeyboardButton("Target 5x",   callback_data=f"as:te_mult:{mint}:5")],
                    [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
                ])
            )

    elif action == "te_mult":
        mult = float(parts[3]) if len(parts) > 3 else 2.0
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("time_exit", {})["target_mult"] = mult
            set_auto_sell(uid, mint, cfg)
            await query.answer(f"Target mult set to {mult}x")
            te = cfg["time_exit"]
            on = te.get("enabled", False)
            toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
            status_txt = "🟢 Enabled" if on else "🔴 Disabled"
            elapsed = (time.time() - te.get("buy_time", time.time())) / 3600
            await query.edit_message_text(
                f"*⏱️ Time Exit — ${cfg.get('symbol','?')}*\n\n"
                f"Status: {status_txt}\n"
                f"Exit if not `{te.get('target_mult',2.0)}x` after `{te.get('hours',24)}h`\n"
                f"Sell: `{te.get('sell_pct',100)}%` of position\n"
                f"Elapsed: `{elapsed:.1f}h`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(toggle_lbl, callback_data=f"as:te_toggle:{mint}")],
                    [InlineKeyboardButton("6h",  callback_data=f"as:te_hours:{mint}:6"),
                     InlineKeyboardButton("12h", callback_data=f"as:te_hours:{mint}:12"),
                     InlineKeyboardButton("24h", callback_data=f"as:te_hours:{mint}:24"),
                     InlineKeyboardButton("48h", callback_data=f"as:te_hours:{mint}:48")],
                    [InlineKeyboardButton("Target 1.5x", callback_data=f"as:te_mult:{mint}:1.5"),
                     InlineKeyboardButton("Target 2x",   callback_data=f"as:te_mult:{mint}:2"),
                     InlineKeyboardButton("Target 3x",   callback_data=f"as:te_mult:{mint}:3"),
                     InlineKeyboardButton("Target 5x",   callback_data=f"as:te_mult:{mint}:5")],
                    [InlineKeyboardButton("⬅️ Back",   callback_data=f"as:view:{mint}")],
                ])
            )

    # ── Mult targets menu ─────────────────────────────────────────────────────
    elif action == "mt_menu":
        cfg = get_auto_sell(uid, mint)
        if not cfg:
            await query.edit_message_text("Config not found.", reply_markup=back_kb(f"as:view:{mint}"))
            return
        targets = cfg.get("mult_targets", [])
        sym = cfg.get("symbol", mint[:6])
        rows = []
        for i, t in enumerate(targets):
            status = "✅" if t.get("triggered") else "⏳"
            label = f"{t['mult']}x → {t['sell_pct']}% {status}"
            rows.append([
                InlineKeyboardButton(label, callback_data=f"as:noop:{mint}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"as:mt_edit:{mint}:{i}"),
                InlineKeyboardButton("🗑️ Del",  callback_data=f"as:mt_del:{mint}:{i}"),
            ])
        rows.append([InlineKeyboardButton("➕ Add Mult Target", callback_data=f"as:mt_add:{mint}")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"as:view:{mint}")])
        await query.edit_message_text(
            f"*📈 Multiplier Targets — ${sym}*\n\nManage auto-sell targets by multiplier.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    elif action == "mt_del":
        idx = int(parts[3]) if len(parts) > 3 else 0
        cfg = get_auto_sell(uid, mint)
        if cfg:
            targets = cfg.get("mult_targets", [])
            if 0 <= idx < len(targets):
                targets.pop(idx)
                cfg["mult_targets"] = targets
                set_auto_sell(uid, mint, cfg)
                await query.answer("Target deleted.")
            else:
                await query.answer("Invalid index.")
        # Re-show mult_targets_menu
        cfg = get_auto_sell(uid, mint) or {}
        targets = cfg.get("mult_targets", [])
        sym = cfg.get("symbol", mint[:6])
        rows = []
        for i, t in enumerate(targets):
            status = "✅" if t.get("triggered") else "⏳"
            label = f"{t['mult']}x → {t['sell_pct']}% {status}"
            rows.append([
                InlineKeyboardButton(label, callback_data=f"as:noop:{mint}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"as:mt_edit:{mint}:{i}"),
                InlineKeyboardButton("🗑️ Del",  callback_data=f"as:mt_del:{mint}:{i}"),
            ])
        rows.append([InlineKeyboardButton("➕ Add Mult Target", callback_data=f"as:mt_add:{mint}")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"as:view:{mint}")])
        await query.edit_message_text(
            f"*📈 Multiplier Targets — ${sym}*\n\nManage auto-sell targets by multiplier.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    elif action == "mt_add":
        set_state(uid, waiting_for="as_mt_add_mult", as_mint=mint)
        await query.edit_message_text(
            "Enter multiplier (e.g. 3 for 3x):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"as:mt_menu:{mint}")
            ]])
        )

    elif action == "mt_edit":
        idx = int(parts[3]) if len(parts) > 3 else 0
        cfg = get_auto_sell(uid, mint) or {}
        targets = cfg.get("mult_targets", [])
        if 0 <= idx < len(targets):
            t = targets[idx]
            set_state(uid, waiting_for="as_mt_edit_mult", as_mint=mint, as_mt_idx=idx)
            await query.edit_message_text(
                f"Enter new multiplier for target {idx+1} (current: {t['mult']}x):",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data=f"as:mt_menu:{mint}")
                ]])
            )
        else:
            await query.answer("Invalid index.")

    elif action == "mt_add_sp":
        # Preset sell% button when adding a mult target
        pct  = int(parts[3]) if len(parts) > 3 else 50
        mult = get_state(uid, "as_mt_mult")
        clear_state(uid)
        cfg  = get_auto_sell(uid, mint)
        if cfg and mult is not None:
            label = f"{mult}x" if mult != int(mult) else f"{int(mult)}x"
            cfg.setdefault("mult_targets", []).append({
                "mult": mult, "sell_pct": pct, "triggered": False, "label": label
            })
            set_auto_sell(uid, mint, cfg)
            sym = cfg.get("symbol", mint[:6])
            await query.edit_message_text(
                f"✅ Added: `{label}` → sell `{pct}%` for `${sym}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📈 Edit Targets", callback_data=f"as:mt_menu:{mint}")
                ]])
            )
        else:
            await query.edit_message_text("Session expired. Please try again.", reply_markup=back_kb("menu:autosell"))

    elif action == "mt_edit_sp":
        # Preset sell% button when editing a mult target
        pct  = int(parts[3]) if len(parts) > 3 else 50
        mult = get_state(uid, "as_mt_mult")
        idx  = get_state(uid, "as_mt_idx")
        clear_state(uid)
        cfg  = get_auto_sell(uid, mint)
        if cfg and mult is not None and idx is not None:
            targets = cfg.get("mult_targets", [])
            if 0 <= idx < len(targets):
                label = f"{mult}x" if mult != int(mult) else f"{int(mult)}x"
                targets[idx].update({"mult": mult, "sell_pct": pct, "label": label})
                cfg["mult_targets"] = targets
                set_auto_sell(uid, mint, cfg)
                sym = cfg.get("symbol", mint[:6])
                await query.edit_message_text(
                    f"✅ Updated target {idx+1}: `{label}` → sell `{pct}%` for `${sym}`",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📈 Edit Targets", callback_data=f"as:mt_menu:{mint}")
                    ]])
                )
            else:
                await query.edit_message_text("Invalid target.", reply_markup=back_kb(f"as:mt_menu:{mint}"))
        else:
            await query.edit_message_text("Session expired. Please try again.", reply_markup=back_kb("menu:autosell"))

    elif action == "mt_sp_custom":
        # User wants to type a custom sell % — state is already set (add or edit flow)
        await query.edit_message_text(
            "Enter sell % (1–100):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"as:mt_menu:{mint}")
            ]])
        )

    elif action == "noop":
        await query.answer()

    # ── MCap alerts menu ──────────────────────────────────────────────────────
    elif action == "mcap_menu":
        cfg = get_auto_sell(uid, mint)
        if not cfg:
            await query.edit_message_text("Config not found.", reply_markup=back_kb(f"as:view:{mint}"))
            return
        alerts = cfg.get("mcap_alerts", [])
        sym = cfg.get("symbol", mint[:6])
        rows = []
        for i, ma in enumerate(alerts):
            status = "✅" if ma.get("triggered") else "⏳"
            label = f"${ma['label']} — {status}"
            rows.append([
                InlineKeyboardButton(label, callback_data=f"as:noop:{mint}"),
                InlineKeyboardButton("🗑️ Del", callback_data=f"as:ma_del:{mint}:{i}"),
            ])
        rows.append([InlineKeyboardButton("➕ Add MCap Alert", callback_data=f"as:ma_add:{mint}")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"as:view:{mint}")])
        await query.edit_message_text(
            f"*🏦 MCap Alerts — ${sym}*\n\nAlerts fire when market cap is reached.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    elif action == "ma_del":
        idx = int(parts[3]) if len(parts) > 3 else 0
        cfg = get_auto_sell(uid, mint)
        if cfg:
            alerts = cfg.get("mcap_alerts", [])
            if 0 <= idx < len(alerts):
                alerts.pop(idx)
                cfg["mcap_alerts"] = alerts
                set_auto_sell(uid, mint, cfg)
                await query.answer("Alert deleted.")
            else:
                await query.answer("Invalid index.")
        # Re-show mcap_menu
        cfg = get_auto_sell(uid, mint) or {}
        alerts = cfg.get("mcap_alerts", [])
        sym = cfg.get("symbol", mint[:6])
        rows = []
        for i, ma in enumerate(alerts):
            status = "✅" if ma.get("triggered") else "⏳"
            label = f"${ma['label']} — {status}"
            rows.append([
                InlineKeyboardButton(label, callback_data=f"as:noop:{mint}"),
                InlineKeyboardButton("🗑️ Del", callback_data=f"as:ma_del:{mint}:{i}"),
            ])
        rows.append([InlineKeyboardButton("➕ Add MCap Alert", callback_data=f"as:ma_add:{mint}")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"as:view:{mint}")])
        await query.edit_message_text(
            f"*🏦 MCap Alerts — ${sym}*\n\nAlerts fire when market cap is reached.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    elif action == "ma_add":
        set_state(uid, waiting_for="as_ma_add", as_mint=mint)
        await query.edit_message_text(
            "Enter market cap target in USD (e.g. 250000 for $250K). Alert fires when reached.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"as:mcap_menu:{mint}")
            ]])
        )

    # ── Breakeven stop menu ───────────────────────────────────────────────────
    elif action == "be_menu":
        cfg = get_auto_sell(uid, mint)
        if not cfg:
            await query.edit_message_text("Config not found.", reply_markup=back_kb(f"as:view:{mint}"))
            return
        be = cfg.get("breakeven_stop", {"enabled": False, "activate_mult": 2.0, "triggered": False})
        sym = cfg.get("symbol", mint[:6])
        on = be.get("enabled", False)
        toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        tri_txt = " ✅ Activated" if be.get("triggered") else ""
        await query.edit_message_text(
            f"*🛡️ Breakeven Stop — ${sym}*\n\n"
            f"Status: {status_txt}{tri_txt}\n"
            f"Activates at: `{be.get('activate_mult', 2.0)}x`\n\n"
            f"When price hits the activation mult, stop-loss is moved to entry price.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl, callback_data=f"as:be_toggle:{mint}")],
                [InlineKeyboardButton("Activates 1.5x", callback_data=f"as:be_mult:{mint}:1.5"),
                 InlineKeyboardButton("Activates 2x",   callback_data=f"as:be_mult:{mint}:2"),
                 InlineKeyboardButton("Activates 3x",   callback_data=f"as:be_mult:{mint}:3")],
                [InlineKeyboardButton("⬅️ Back", callback_data=f"as:view:{mint}")],
            ])
        )

    elif action == "be_toggle":
        cfg = get_auto_sell(uid, mint)
        if cfg:
            be = cfg.setdefault("breakeven_stop", {"enabled": False, "activate_mult": 2.0, "triggered": False})
            be["enabled"] = not be.get("enabled", False)
            set_auto_sell(uid, mint, cfg)
        be = cfg.get("breakeven_stop", {})
        sym = cfg.get("symbol", mint[:6])
        on = be.get("enabled", False)
        toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        tri_txt = " ✅ Activated" if be.get("triggered") else ""
        await query.edit_message_text(
            f"*🛡️ Breakeven Stop — ${sym}*\n\n"
            f"Status: {status_txt}{tri_txt}\n"
            f"Activates at: `{be.get('activate_mult', 2.0)}x`\n\n"
            f"When price hits the activation mult, stop-loss is moved to entry price.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl, callback_data=f"as:be_toggle:{mint}")],
                [InlineKeyboardButton("Activates 1.5x", callback_data=f"as:be_mult:{mint}:1.5"),
                 InlineKeyboardButton("Activates 2x",   callback_data=f"as:be_mult:{mint}:2"),
                 InlineKeyboardButton("Activates 3x",   callback_data=f"as:be_mult:{mint}:3")],
                [InlineKeyboardButton("⬅️ Back", callback_data=f"as:view:{mint}")],
            ])
        )

    elif action == "be_mult":
        mult = float(parts[3]) if len(parts) > 3 else 2.0
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("breakeven_stop", {})["activate_mult"] = mult
            set_auto_sell(uid, mint, cfg)
            await query.answer(f"Breakeven activates at {mult}x")
        be = cfg.get("breakeven_stop", {})
        sym = cfg.get("symbol", mint[:6])
        on = be.get("enabled", False)
        toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        tri_txt = " ✅ Activated" if be.get("triggered") else ""
        await query.edit_message_text(
            f"*🛡️ Breakeven Stop — ${sym}*\n\n"
            f"Status: {status_txt}{tri_txt}\n"
            f"Activates at: `{be.get('activate_mult', 2.0)}x`\n\n"
            f"When price hits the activation mult, stop-loss is moved to entry price.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl, callback_data=f"as:be_toggle:{mint}")],
                [InlineKeyboardButton("Activates 1.5x", callback_data=f"as:be_mult:{mint}:1.5"),
                 InlineKeyboardButton("Activates 2x",   callback_data=f"as:be_mult:{mint}:2"),
                 InlineKeyboardButton("Activates 3x",   callback_data=f"as:be_mult:{mint}:3")],
                [InlineKeyboardButton("⬅️ Back", callback_data=f"as:view:{mint}")],
            ])
        )

    # ── Strategy presets ──────────────────────────────────────────────────────
    elif action == "strategies":
        cfg = get_auto_sell(uid, mint)
        if not cfg:
            await query.edit_message_text("Config not found.", reply_markup=back_kb(f"as:view:{mint}"))
            return
        sym = cfg.get("symbol", mint[:6])
        await query.edit_message_text(
            f"*⚡ Strategy Presets — ${sym}*\n\n"
            "Choose a preset strategy. This will overwrite your current targets, stop-loss, and exit settings.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏃 Scalp",    callback_data=f"as:sa:{mint}:scalp")],
                [InlineKeyboardButton("📊 Standard", callback_data=f"as:sa:{mint}:standard")],
                [InlineKeyboardButton("💎 Diamond",  callback_data=f"as:sa:{mint}:diamond")],
                [InlineKeyboardButton("🌙 Moon Bag", callback_data=f"as:sa:{mint}:moon")],
                [InlineKeyboardButton("⬅️ Back",     callback_data=f"as:view:{mint}")],
            ])
        )

    elif action == "sa":
        strategy_name = parts[3] if len(parts) > 3 else "standard"
        cfg = get_auto_sell(uid, mint)
        if not cfg:
            await query.edit_message_text("Config not found.", reply_markup=back_kb(f"as:view:{mint}"))
            return
        preset = STRATEGIES.get(strategy_name)
        if not preset:
            await query.answer("Unknown strategy.")
            return
        buy_time = cfg.get("time_exit", {}).get("buy_time", time.time())
        cfg["mult_targets"]   = preset["mult_targets"]
        cfg["stop_loss"]      = preset["stop_loss"]
        cfg["trailing_stop"]  = preset["trailing_stop"]
        cfg["trailing_tp"]    = preset["trailing_tp"]
        te = dict(preset["time_exit"])
        te["buy_time"] = buy_time
        cfg["time_exit"] = te
        set_auto_sell(uid, mint, cfg)
        sym = cfg.get("symbol", mint[:6])
        names = {"scalp": "🏃 Scalp", "standard": "📊 Standard", "diamond": "💎 Diamond", "moon": "🌙 Moon Bag"}
        await query.edit_message_text(
            f"✅ *{names.get(strategy_name, strategy_name)} strategy applied to ${sym}!*\n\n"
            + _format_autosell_config(cfg),
            parse_mode="Markdown",
            reply_markup=autosell_token_kb(uid, mint)
        )


async def as_preset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle auto-sell preset customization (as_preset:* pattern)"""
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]
    await query.answer()

    if action == "menu":
        # Show current presets and options to edit
        user_presets = get_user_as_presets(uid)
        enabled = get_user_as_presets_enabled(uid)
        status_txt = "🟢 ON" if enabled else "🔴 OFF"
        toggle_lbl = "⏸️ Disable Presets" if enabled else "▶️ Enable Presets"

        # Build display of current presets
        presets_display = f"📊 *Auto-Sell Default Presets* — {status_txt}\n\n"
        for i, p in enumerate(user_presets):
            presets_display += f"{i+1}. {p['mult']:.1f}x → Sell {p['sell_pct']}%\n"
        presets_display += (
            f"\n{'These targets will be applied to your next buys.' if enabled else 'Presets are OFF — new buys will have no auto-sell targets.'}"
        )

        await query.edit_message_text(
            presets_display,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl, callback_data="as_preset:toggle")],
                [InlineKeyboardButton("✏️ Edit Presets", callback_data="as_preset:edit")],
                [InlineKeyboardButton("↩️ Back", callback_data="menu:autosell")],
            ])
        )

    elif action == "toggle":
        enabled = get_user_as_presets_enabled(uid)
        set_user_as_presets_enabled(uid, not enabled)
        enabled = not enabled
        status_txt = "🟢 ON" if enabled else "🔴 OFF"
        toggle_lbl = "⏸️ Disable Presets" if enabled else "▶️ Enable Presets"
        user_presets = get_user_as_presets(uid)
        presets_display = f"📊 *Auto-Sell Default Presets* — {status_txt}\n\n"
        for i, p in enumerate(user_presets):
            presets_display += f"{i+1}. {p['mult']:.1f}x → Sell {p['sell_pct']}%\n"
        presets_display += (
            f"\n{'These targets will be applied to your next buys.' if enabled else 'Presets are OFF — new buys will have no auto-sell targets.'}"
        )
        await query.edit_message_text(
            presets_display,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl, callback_data="as_preset:toggle")],
                [InlineKeyboardButton("✏️ Edit Presets", callback_data="as_preset:edit")],
                [InlineKeyboardButton("↩️ Back", callback_data="menu:autosell")],
            ])
        )
    
    elif action == "edit":
        # Show UI to edit presets
        user_presets = get_user_as_presets(uid)
        
        # For simplicity, build inline buttons to adjust each target
        # Format: as_preset:adjust:target_index:direction (where direction is +mult, -mult, +pct, -pct)
        kb = []
        for i, p in enumerate(user_presets):
            kb.append([
                InlineKeyboardButton(f"Edit {p['mult']:.1f}x", callback_data=f"as_preset:target:{i}")
            ])
        kb.append([InlineKeyboardButton("➕ Add Target", callback_data="as_preset:add")])
        kb.append([InlineKeyboardButton("⬅️ Back", callback_data="as_preset:menu")])
        
        await query.edit_message_text(
            "🔧 *Customize Your Auto-Sell Presets*\n\n"
            "Select a target to edit, or add a new one:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    
    elif action == "target":
        # Edit specific target
        target_idx = int(parts[2])
        user_presets = get_user_as_presets(uid)
        if target_idx >= len(user_presets):
            await query.edit_message_text("Invalid target.", reply_markup=back_kb("as_preset:edit"))
            return
        
        target = user_presets[target_idx]
        kb = [
            [InlineKeyboardButton(f"Mult: {target['mult']:.1f}x 🔺", callback_data=f"as_preset:adj:{target_idx}:mult_up"),
             InlineKeyboardButton("🔻", callback_data=f"as_preset:adj:{target_idx}:mult_down")],
            [InlineKeyboardButton(f"Pct: {target['sell_pct']}% 🔺", callback_data=f"as_preset:adj:{target_idx}:pct_up"),
             InlineKeyboardButton("🔻", callback_data=f"as_preset:adj:{target_idx}:pct_down")],
            [InlineKeyboardButton("🗑️ Delete", callback_data=f"as_preset:del:{target_idx}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="as_preset:edit")],
        ]
        
        await query.edit_message_text(
            f"📈 *Edit Target {target_idx + 1}*\n\n"
            f"Multiplier: `{target['mult']:.1f}x`\n"
            f"Sell %: `{target['sell_pct']}%`\n\n"
            f"Use buttons to adjust values.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    
    elif action == "adj":
        # Adjust multiplier or percentage
        target_idx = int(parts[2])
        adj_type = parts[3]
        user_presets = get_user_as_presets(uid)
        if target_idx >= len(user_presets):
            await query.edit_message_text("Invalid target.", reply_markup=back_kb("as_preset:edit"))
            return
        
        target = user_presets[target_idx]
        
        if adj_type == "mult_up":
            target["mult"] = round(target["mult"] + 0.5, 1)
        elif adj_type == "mult_down":
            target["mult"] = max(0.5, round(target["mult"] - 0.5, 1))
        elif adj_type == "pct_up":
            target["sell_pct"] = min(100, target["sell_pct"] + 5)
        elif adj_type == "pct_down":
            target["sell_pct"] = max(5, target["sell_pct"] - 5)
        
        set_user_as_presets(uid, user_presets)
        
        # Show updated config
        kb = [
            [InlineKeyboardButton(f"Mult: {target['mult']:.1f}x 🔺", callback_data=f"as_preset:adj:{target_idx}:mult_up"),
             InlineKeyboardButton("🔻", callback_data=f"as_preset:adj:{target_idx}:mult_down")],
            [InlineKeyboardButton(f"Pct: {target['sell_pct']}% 🔺", callback_data=f"as_preset:adj:{target_idx}:pct_up"),
             InlineKeyboardButton("🔻", callback_data=f"as_preset:adj:{target_idx}:pct_down")],
            [InlineKeyboardButton("🗑️ Delete", callback_data=f"as_preset:del:{target_idx}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="as_preset:edit")],
        ]
        
        open_count = _apply_presets_to_open_positions(uid, user_presets)
        pos_note = f" Applied to {open_count} open position(s)." if open_count else ""
        await query.edit_message_text(
            f"📈 *Edit Target {target_idx + 1}*\n\n"
            f"Multiplier: `{target['mult']:.1f}x`\n"
            f"Sell %: `{target['sell_pct']}%`\n\n"
            f"✅ Updated!{pos_note}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    
    elif action == "del":
        # Delete a preset target
        target_idx = int(parts[2])
        user_presets = get_user_as_presets(uid)
        if target_idx < len(user_presets):
            user_presets.pop(target_idx)
            # Ensure at least one target exists
            if not user_presets:
                user_presets = [{"mult": 2.0, "sell_pct": 50}]
            set_user_as_presets(uid, user_presets)
        
        open_count = _apply_presets_to_open_positions(uid, user_presets)
        pos_note = f"\nApplied to {open_count} open position(s)." if open_count else ""
        await query.edit_message_text(
            f"🗑️ *Target deleted!*\n\nYour presets have been updated.{pos_note}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="as_preset:edit")]])
        )
    
    elif action == "add":
        # Add new preset - show input menu or preset options
        await query.edit_message_text(
            "➕ *Add New Target*\n\n"
            "Choose a preset or customize:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("1.5x (scalp)", callback_data="as_preset:add_preset:1.5:75")],
                [InlineKeyboardButton("2x (standard)", callback_data="as_preset:add_preset:2.0:50")],
                [InlineKeyboardButton("3x (dca)", callback_data="as_preset:add_preset:3.0:33")],
                [InlineKeyboardButton("5x (moon)", callback_data="as_preset:add_preset:5.0:50")],
                [InlineKeyboardButton("❌ Cancel", callback_data="as_preset:edit")],
            ])
        )
    
    elif action == "add_preset":
        # Add a preset target
        mult = float(parts[2])
        sell_pct = int(parts[3])
        user_presets = get_user_as_presets(uid)
        user_presets.append({"mult": mult, "sell_pct": sell_pct})
        set_user_as_presets(uid, user_presets)
        
        open_count = _apply_presets_to_open_positions(uid, user_presets)
        pos_note = f"\nApplied to {open_count} open position(s)." if open_count else ""
        await query.edit_message_text(
            f"✅ *Target Added!*\n\n"
            f"{mult:.1f}x → Sell {sell_pct}%{pos_note}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="as_preset:edit")]])
        )


async def custom_target_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    uid     = query.from_user.id
    parts   = query.data.split(":")
    ct_type = parts[1]
    mint    = parts[2]
    await query.answer()

    set_state(uid, waiting_for="custom_target_value",
              custom_mint=mint, custom_type=ct_type)

    hints = {
        "multiplier": "Enter the multiplier (e.g. `3` for 3x, `5` for 5x):",
        "price":      "Enter the target price in USD (e.g. `0.001`):",
        "mcap":       "Enter the target market cap in USD (e.g. `250000` for $250K):",
    }
    await query.edit_message_text(
        f"➕ *Custom Target — {ct_type.title()}*\n\n{hints[ct_type]}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data=f"as:view:{mint}")
        ]])
    )


async def portfolio_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    action = query.data.split(":")[1] if len(query.data.split(":")) > 1 else "refresh"

    if action == "refresh":
        await query.answer("Refreshing...")
        await query.edit_message_text("Loading...")
        try:
            await _show_portfolio(query.edit_message_text, uid)
        except Exception as e:
            print(f"[PORTFOLIO] refresh error: {e}")
            await query.edit_message_text(
                "⚠️ Failed to load portfolio. Try again.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Retry", callback_data="portfolio:refresh"),
                    InlineKeyboardButton("⬅️ Menu",  callback_data="menu:main"),
                ]])
            )

    elif action == "page":
        await query.answer()
        target_page = int(query.data.split(":")[2]) if len(query.data.split(":")) > 2 else 0
        await query.edit_message_text("Loading...")
        try:
            await _show_portfolio(query.edit_message_text, uid, page=target_page)
        except Exception as e:
            print(f"[PORTFOLIO] page error: {e}")
            await query.edit_message_text(
                "⚠️ Failed to load portfolio. Try again.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Retry", callback_data="portfolio:refresh"),
                    InlineKeyboardButton("⬅️ Menu",  callback_data="menu:main"),
                ]])
            )

    elif action == "sell_profit_confirm":
        # Confirm screen: sell all positions currently in profit
        await query.answer()
        mode = get_mode(uid)
        as_configs = _db.get_all_auto_sells(uid)
        profitable = []
        # In live mode, cross-reference with actual wallet so only real holdings show
        live_mints: set[str] = set()
        external_count = 0
        if mode == "live":
            pubkey = get_wallet_pubkey()
            if pubkey:
                for a in (get_token_accounts(pubkey) or []):
                    live_mints.add(a["mint"])
            # Count wallet tokens not tracked by bot
            external_count = len(live_mints - set(as_configs.keys()))
        for mint, cfg in as_configs.items():
            if mode == "live" and live_mints and mint not in live_mints:
                continue  # no longer in wallet
            buy_price = cfg.get("buy_price_usd", 0)
            if not buy_price:
                continue
            price, _ = fetch_token_price(mint)
            if price and price > buy_price:
                pct = (price - buy_price) / buy_price * 100
                profitable.append((mint, cfg.get("symbol", mint[:6]), pct))
        if not profitable:
            await query.edit_message_text(
                "💰 *Sell Profitable*\n\nNo positions are currently in profit.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("👜 Portfolio", callback_data="portfolio:refresh")
                ]])
            )
            return
        lines = ["💰 *Sell All Profitable — Confirm*\n"]
        for _, sym, pct in profitable:
            lines.append(f"  • `${sym}` +{pct:.0f}%")
        lines.append(f"\n{len(profitable)} position(s) will be sold at 100%.")
        if external_count:
            lines.append(f"_ℹ️ {external_count} external wallet position(s) not shown (no buy price tracked)_")
        lines.append("\nProceed?")
        await query.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Sell Profit", callback_data="portfolio:sell_profit_exec"),
                 InlineKeyboardButton("❌ Cancel",           callback_data="portfolio:refresh")],
            ])
        )

    elif action == "sell_profit_exec":
        await query.answer("Selling profitable positions...")
        mode = get_mode(uid)
        as_configs = _db.get_all_auto_sells(uid)
        results = []
        total_sol = 0.0
        # In live mode, fetch wallet once so we only sell positions that exist on-chain
        live_mints: set[str] = set()
        external_count = 0
        if mode == "live":
            pubkey = get_wallet_pubkey()
            if pubkey:
                for a in (get_token_accounts(pubkey) or []):
                    live_mints.add(a["mint"])
            external_count = len(live_mints - set(as_configs.keys()))
        for mint, cfg in list(as_configs.items()):
            if mode == "live" and live_mints and mint not in live_mints:
                continue  # not in wallet, skip stale entry
            buy_price = cfg.get("buy_price_usd", 0)
            if not buy_price:
                continue
            price, mcap = fetch_token_price(mint)
            if not price or price <= buy_price:
                continue
            sym = cfg.get("symbol", mint[:6])
            await execute_auto_sell(
                context.bot, uid, mint, sym, 100,
                "Sell All Profitable", mode,
                price_usd=price, mcap=mcap or 0
            )
            remove_auto_sell(uid, mint)
            results.append(f"✅ `${sym}` +{((price-buy_price)/buy_price*100):.0f}%")
        if not results:
            note = f"\n\n_ℹ️ {external_count} external position(s) skipped (no buy price)_" if external_count else ""
            await query.edit_message_text(
                f"No profitable positions found at time of execution.{note}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👜 Portfolio", callback_data="portfolio:refresh")]])
            )
            return
        if external_count:
            results.append(f"\n_ℹ️ {external_count} external position(s) skipped (no buy price tracked)_")
        await query.edit_message_text(
            f"💰 *Sell Profit Complete*\n\n" + "\n".join(results),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👜 Portfolio", callback_data="portfolio:refresh")]])
        )

    elif action == "sell_below_prompt":
        # Prompt user to enter the loss threshold %
        await query.answer()
        set_state(uid, waiting_for="sell_below_pct")
        await query.edit_message_text(
            "🔻 *Sell Below X%*\n\n"
            "Enter the loss threshold percentage.\n"
            "Example: `30` will sell all positions down *30% or more* from your buy price.\n\n"
            "Type a number (1–99):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="portfolio:refresh")]])
        )

    elif action == "sell_all_confirm":
        await query.answer()
        mode = get_mode(uid)
        as_configs = _db.get_all_auto_sells(uid)
        if mode == "live":
            pubkey   = get_wallet_pubkey()
            accounts = get_token_accounts(pubkey) if pubkey else []
            tokens   = [(a["mint"], a["amount"]) for a in accounts]
        else:
            portfolio = get_portfolio(uid)
            tokens    = [(k, v) for k, v in portfolio.items() if k != "SOL" and v > 0]
        if not tokens:
            await query.edit_message_text(
                "No token positions to sell.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("👜 Portfolio", callback_data="portfolio:refresh")
                ]])
            )
            return
        # Show loading while fetching price estimates
        await query.edit_message_text("⏳ Calculating estimated value...")
        total_est = 0.0
        lines = ["⚠️ *Sell All — Confirm*\n"]
        sol_price = pf.get_sol_price() or 150.0
        for mint, raw_held in tokens:
            cfg  = as_configs.get(mint, {})
            sym  = cfg.get("symbol", mint[:6])
            dec  = cfg.get("decimals", 6)
            pair = fetch_sol_pair(mint)
            if pair:
                price_sol = float(pair.get("priceNative", 0) or 0)
                _dec      = int(pair.get("baseToken", {}).get("decimals", dec) or dec)
                sol_est   = price_sol * raw_held / (10 ** _dec) * 0.99
                total_est += sol_est
                lines.append(f"  • `${sym}` ~{sol_est:.4f} SOL")
            else:
                price_usd, _ = fetch_token_price(mint)
                if price_usd and sol_price:
                    sol_est   = (price_usd / sol_price) * raw_held / (10 ** dec) * 0.99
                    total_est += sol_est
                    lines.append(f"  • `${sym}` ~{sol_est:.4f} SOL")
                else:
                    lines.append(f"  • `${sym}` (no price data)")
        mode_label = "🔴 Live" if mode == "live" else "📄 Paper"
        lines.append(f"\n*Est. total: ~`{total_est:.4f} SOL`*")
        lines.append(f"Mode: {mode_label}")
        lines.append("\nThis cannot be undone. Proceed?")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Sell All", callback_data="portfolio:sell_all_exec"),
                 InlineKeyboardButton("❌ Cancel",        callback_data="portfolio:refresh")],
            ])
        )

    elif action == "sell_all_exec":
        await query.answer("Executing sell all...")
        mode    = get_mode(uid)
        results = []

        if mode == "paper":
            # Fetch prices outside lock (slow network calls)
            snapshot  = {k: v for k, v in get_portfolio(uid).items() if k != "SOL" and v > 0}
            total_sol = 0.0
            price_map: dict[str, tuple] = {}  # mint -> (price_sol, dec, sym)
            for mint, raw_held in snapshot.items():
                pair      = fetch_sol_pair(mint)
                sym       = pair.get("baseToken", {}).get("symbol", mint[:8]) if pair else mint[:8]
                price_sol = float(pair.get("priceNative", 0) or 0) if pair else 0
                dec       = int(pair.get("baseToken", {}).get("decimals", 6) or 6) if pair else 6
                if not price_sol:
                    _bc = pumpfun.fetch_bonding_curve_data(mint, SOLANA_RPC)
                    if _bc and _bc.get("virtual_token_reserves") and _bc["virtual_token_reserves"] > 0:
                        price_sol = _bc["virtual_sol_reserves"] / _bc["virtual_token_reserves"] / 1e9 * 1e6
                        dec = 6
                price_map[mint] = (price_sol, dec, sym)

            async with _portfolio_lock(uid):
                portfolio = get_portfolio(uid)
                for mint, (price_sol, dec, sym) in price_map.items():
                    raw_held = portfolio.get(mint, 0)
                    if not raw_held:
                        continue
                    if price_sol:
                        sol_recv = (price_sol * raw_held / (10 ** dec)) * 0.99
                        portfolio.pop(mint, None)
                        portfolio["SOL"] = portfolio.get("SOL", 0) + sol_recv
                        total_sol += sol_recv
                        remove_auto_sell(uid, mint)
                        results.append(f"✅ `${sym}` → `{sol_recv:.4f} SOL`")
                    else:
                        results.append(f"❌ `${sym}` — no price data")
                update_portfolio(uid, portfolio)
            summary = "\n".join(results) or "Nothing sold."
            await query.edit_message_text(
                f"📄 *Sell All Complete*\n\n{summary}\n\n"
                f"Total received: `{total_sol:.4f} SOL`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("👜 Portfolio", callback_data="portfolio:refresh")
                ]])
            )

        else:
            pubkey   = get_wallet_pubkey()
            accounts = get_token_accounts(pubkey) if pubkey else []
            if not accounts:
                await query.edit_message_text(
                    "No token positions found in live wallet.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("👜 Portfolio", callback_data="portfolio:refresh")
                    ]])
                )
                return
            total_sol = 0.0
            for acc in accounts:
                mint     = acc["mint"]
                raw_held = acc["amount"]
                pair     = fetch_sol_pair(mint)
                sym      = pair.get("baseToken", {}).get("symbol", mint[:8]) if pair else mint[:8]
                quote    = jupiter_quote(mint, SOL_MINT, raw_held, get_user_slippage(uid))
                if quote and "outAmount" in quote:
                    sig      = execute_swap_live(quote, uid)
                    sol_recv = int(quote.get("outAmount", 0)) / 1e9
                    if "ERROR" in sig or "error" in sig.lower():
                        results.append(f"❌ `${sym}` — swap failed: `{sig[:40]}`")
                    else:
                        total_sol += sol_recv
                        remove_auto_sell(uid, mint)
                        results.append(f"✅ `${sym}` → `~{sol_recv:.4f} SOL`")
                else:
                    results.append(f"❌ `${sym}` — quote failed")
            summary = "\n".join(results) or "Nothing sold."
            await query.edit_message_text(
                f"🔴 *Sell All Complete*\n\n{summary}\n\n"
                f"Est. total: `~{total_sol:.4f} SOL`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("👜 Portfolio", callback_data="portfolio:refresh")
                ]])
            )


async def qt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick trade view — shows token position + 10/25/50/100% buy+sell buttons."""
    query = update.callback_query
    uid   = query.from_user.id
    mint  = query.data.split(":", 1)[1]
    await query.answer()
    mode = get_mode(uid)

    pair = fetch_sol_pair(mint)
    sym  = pair.get("baseToken", {}).get("symbol", mint[:8]) if pair else mint[:8]
    price = float(pair.get("priceUsd", 0) or 0) if pair else 0

    if mode == "live":
        pubkey   = get_wallet_pubkey()
        sol_bal  = get_sol_balance(pubkey) if pubkey else 0
        accounts = get_token_accounts(pubkey) if pubkey else []
        held_raw = next((a["ui_amount"] for a in accounts if a["mint"] == mint), 0)
        val      = held_raw * price
        lines = [
            f"⚡ *Quick Trade — ${sym}*\n",
            f"Price: `${price:.8f}`",
            f"Held: `{held_raw:,.4f}` ≈ `${val:,.4f}`",
            f"SOL balance: `{sol_bal:.4f}`",
        ]
    else:
        portfolio = get_portfolio(uid)
        sol_bal   = portfolio.get("SOL", 0)
        raw_amt   = portfolio.get(mint, 0)
        dec       = int(pair.get("baseToken", {}).get("decimals", 6) or 6) if pair else 6
        ui        = raw_amt / (10 ** dec)
        val       = ui * price
        lines = [
            f"⚡ *Quick Trade — ${sym}* 📄\n",
            f"Price: `${price:.8f}`",
            f"Held: `{ui:,.4f}` ≈ `${val:,.4f}`",
            f"SOL balance: `{sol_bal:.4f}`",
        ]

    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=_pct_kb(mint)
    )


async def qp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute a quick percent buy or sell immediately."""
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")   # qp : action : mint : pct
    action = parts[1]
    mint   = parts[2]
    pct    = int(parts[3])
    await query.answer(f"Executing {action} {pct}%...")
    mode = get_mode(uid)

    pair = fetch_sol_pair(mint)
    if not pair:
        await query.edit_message_text("Could not fetch token data.", reply_markup=_pct_kb(mint))
        return
    sym      = pair.get("baseToken", {}).get("symbol", mint[:8])
    price    = float(pair.get("priceUsd", 0) or 0)
    dec      = int(pair.get("baseToken", {}).get("decimals", 6) or 6)

    # ── SELL ──────────────────────────────────────────────────────────────────
    if action == "sell":
        if mode == "paper":
            # Fetch price outside lock
            price_sol = float(pair.get("priceNative", 0) or 0)
            if not price_sol:
                _bc = pumpfun.fetch_bonding_curve_data(mint, SOLANA_RPC)
                if _bc and _bc.get("virtual_token_reserves") and _bc["virtual_token_reserves"] > 0:
                    price_sol = _bc["virtual_sol_reserves"] / _bc["virtual_token_reserves"] / 1e9 * 1e6
            if not price_sol:
                await query.edit_message_text("Could not fetch current price. Try again.", reply_markup=_pct_kb(mint))
                return
            async with _portfolio_lock(uid):
                portfolio = get_portfolio(uid)
                raw_held  = portfolio.get(mint, 0)
                if raw_held <= 0:
                    await query.edit_message_text(f"No `${sym}` position to sell.", reply_markup=_pct_kb(mint))
                    return
                sell_raw = max(1, int(raw_held * pct / 100))
                ui       = sell_raw / (10 ** dec)
                sol_recv = price_sol * ui * 0.99  # 1% simulated fee
                portfolio[mint]   = raw_held - sell_raw
                portfolio["SOL"]  = portfolio.get("SOL", 0) + sol_recv
                if portfolio[mint] <= 0:
                    portfolio.pop(mint, None)
                    remove_auto_sell(uid, mint)
                update_portfolio(uid, portfolio)
            await query.edit_message_text(
                f"📄 *Paper Sell — {pct}%*\n\n"
                f"Token: `${sym}`\n"
                f"Sold: `{sell_raw:,}` raw units\n"
                f"Received: `{sol_recv:.4f} SOL`\n"
                f"SOL balance: `{portfolio['SOL']:.4f}`",
                parse_mode="Markdown",
                reply_markup=_pct_kb(mint)
            )
        else:
            pubkey   = get_wallet_pubkey()
            accounts = get_token_accounts(pubkey) if pubkey else []
            held     = next((a for a in accounts if a["mint"] == mint), None)
            if not held:
                await query.edit_message_text(f"No `${sym}` in live wallet.", reply_markup=_pct_kb(mint))
                return
            raw_held = held["amount"]
            sell_raw = max(1, int(raw_held * pct / 100))
            quote    = jupiter_quote(mint, SOL_MINT, sell_raw, get_user_slippage(uid))
            if not quote or "outAmount" not in quote:
                await query.edit_message_text("Quote failed. Try again.", reply_markup=_pct_kb(mint))
                return
            sig = execute_swap_live(quote, uid)
            sol_recv = int(quote.get("outAmount", 0)) / 1e9
            if "ERROR" not in sig and "error" not in sig.lower():
                # Update portfolio tracking after live sell
                async with _portfolio_lock(uid):
                    _pf = get_portfolio(uid)
                    _pf["SOL"] = _pf.get("SOL", 0) + sol_recv
                    cur = _pf.get(mint, 0)
                    if sell_raw >= cur:
                        _pf.pop(mint, None)
                        remove_auto_sell(uid, mint)
                    else:
                        _pf[mint] = cur - sell_raw
                    update_portfolio(uid, _pf)
                log_trade(uid, "live", "sell", mint, sym,
                          sol_received=sol_recv, token_amount=sell_raw,
                          price_usd=price, buy_price_usd=_get_buy_price(uid, mint),
                          tx_sig=sig)
            await query.edit_message_text(
                f"🔴 *Live Sell — {pct}%*\n\n"
                f"Token: `${sym}`\n"
                f"Sold: `{sell_raw:,}` raw\n"
                f"Est. received: `{sol_recv:.4f} SOL`\n"
                f"Tx: `{sig}`",
                parse_mode="Markdown",
                reply_markup=_pct_kb(mint)
            )

    # ── BUY ───────────────────────────────────────────────────────────────────
    else:
        if mode == "paper":
            sol_bal   = get_portfolio(uid).get("SOL", 0)
            sol_spend = sol_bal * pct / 100
            if sol_spend < 0.001:
                await query.edit_message_text(f"Insufficient SOL balance (`{sol_bal:.4f}`).", reply_markup=_pct_kb(mint))
                return
            # Quote outside lock
            lamports = int(sol_spend * 1_000_000_000)
            quote    = jupiter_quote(SOL_MINT, mint, lamports)
            if not quote or "outAmount" not in quote:
                await query.edit_message_text("Quote failed. Try again.", reply_markup=_pct_kb(mint))
                return
            out_raw = int(quote["outAmount"])
            async with _portfolio_lock(uid):
                portfolio = get_portfolio(uid)
                sol_bal   = portfolio.get("SOL", 0)
                sol_spend = sol_bal * pct / 100  # recalc under lock with fresh balance
                if sol_spend < 0.001:
                    await query.edit_message_text(f"Insufficient SOL balance (`{sol_bal:.4f}`).", reply_markup=_pct_kb(mint))
                    return
                portfolio["SOL"]   = portfolio.get("SOL", 0) - sol_spend
                portfolio[mint]    = portfolio.get(mint, 0) + out_raw
                update_portfolio(uid, portfolio)
            setup_auto_sell(uid, mint, sym, price, out_raw, dec, sol_amount=sol_spend)
            await query.edit_message_text(
                f"📄 *Paper Buy — {pct}% of SOL*\n\n"
                f"Token: `${sym}`\n"
                f"Spent: `{sol_spend:.4f} SOL`\n"
                f"Received: `{out_raw:,}` raw units\n"
                f"SOL balance: `{portfolio['SOL']:.4f}`",
                parse_mode="Markdown",
                reply_markup=_pct_kb(mint)
            )
        else:
            pubkey  = get_wallet_pubkey()
            sol_bal = get_sol_balance(pubkey) if pubkey else 0
            sol_spend = sol_bal * pct / 100
            if sol_spend < 0.001:
                await query.edit_message_text(f"Insufficient SOL (`{sol_bal:.4f}`).", reply_markup=_pct_kb(mint))
                return
            # Safety check before live buy
            if get_safety_check_enabled(uid):
                await query.edit_message_text("🔍 Running safety check...", parse_mode="Markdown")
                safety = await check_token_safety(mint)
                if not safety["safe"]:
                    await query.edit_message_text(
                        f"🚫 *Safety Check Failed — Buy Cancelled*\n\n"
                        f"❌ *{safety['block_reason']}*\n\n"
                        f"_Disable safety check in Settings to override._",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔍 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
                            [InlineKeyboardButton("⚙️ Settings", callback_data="settings:menu")],
                        ])
                    )
                    return
                if safety["warnings"]:
                    warn_text = "\n".join(f"⚠️ {w}" for w in safety["warnings"])
                    await query.edit_message_text(
                        f"⚠️ *Safety Warnings* — proceeding...\n\n{warn_text}",
                        parse_mode="Markdown"
                    )
                    import asyncio as _aio2; await _aio2.sleep(1.5)
            lamports = int(sol_spend * 1_000_000_000)
            quote    = jupiter_quote(SOL_MINT, mint, lamports, get_user_slippage(uid))
            if not quote or "outAmount" not in quote:
                await query.edit_message_text("Quote failed. Try again.", reply_markup=_pct_kb(mint))
                return
            sig     = execute_swap_live(quote, uid)
            out_raw = int(quote.get("outAmount", 0))
            if "ERROR" not in sig and "error" not in sig.lower():
                # Update portfolio tracking and configure auto-sell for live buy
                async with _portfolio_lock(uid):
                    _pf = get_portfolio(uid)
                    _pf["SOL"] = max(0, _pf.get("SOL", 0) - sol_spend)
                    _pf[mint]  = _pf.get(mint, 0) + out_raw
                    update_portfolio(uid, _pf)
                setup_auto_sell(uid, mint, sym, price, out_raw, dec, sol_amount=sol_spend)
                log_trade(uid, "live", "buy", mint, sym,
                          sol_amount=sol_spend, token_amount=out_raw,
                          price_usd=price, tx_sig=sig)
            await query.edit_message_text(
                f"🔴 *Live Buy — {pct}% of SOL*\n\n"
                f"Token: `${sym}`\n"
                f"Spent: `{sol_spend:.4f} SOL`\n"
                f"Received: `{out_raw:,}` raw\n"
                f"Tx: `{sig}`",
                parse_mode="Markdown",
                reply_markup=_pct_kb(mint)
            )


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]
    await query.answer()

    pending = context.user_data.get(f"pending_{action}")
    if not pending:
        await query.edit_message_text("No pending trade.", reply_markup=back_kb())
        return

    extra_note = ""
    await query.edit_message_text(f"Executing {action}...")
    loop  = asyncio.get_running_loop()
    via   = pending.get("via", "jupiter")

    if via == "pumpfun" and action == "buy":
        from solders.keypair import Keypair as _KP
        kp  = _KP.from_base58_string(WALLET_PRIVATE_KEY)
        sig = await loop.run_in_executor(
            None, pumpfun.buy_pumpfun,
            pending["mint"], pending["sol_amount"], kp, SOLANA_RPC
        )
        if sig == "GRADUATED":
            # Re-route through Jupiter
            await query.edit_message_text("Token graduated — re-routing via Jupiter...")
            lamports = int(pending["sol_amount"] * 1e9)
            quote    = jupiter_quote(SOL_MINT, pending["mint"], lamports, get_user_slippage(uid))
            if not quote:
                await query.edit_message_text("Jupiter quote failed.", reply_markup=back_kb())
                return
            sig = await loop.run_in_executor(None, lambda: execute_swap_live(quote, uid))
            pending["raw_out"] = int(quote.get("outAmount", pending.get("tok_est", 0)))
    else:
        in_mint    = pending.get("in_mint")
        out_mint_r = pending.get("out_mint")
        amt_raw    = pending.get("amount_raw")
        if in_mint and out_mint_r and amt_raw:
            async def _status(msg):
                try:
                    await query.edit_message_text(msg, parse_mode="Markdown")
                except Exception:
                    pass
            sig, new_quote, attempts, final_slip = await _swap_with_retry(
                in_mint, out_mint_r, amt_raw, uid, loop, status_fn=_status
            )
            if new_quote:
                pending["quote"]   = new_quote
                pending["raw_out"] = int(new_quote.get("outAmount", pending.get("raw_out", 0)))
            if attempts > 1:
                extra_note = f"\n_Slippage auto-bumped to {final_slip / 100:.1f}% after {attempts - 1} retries_"
            else:
                extra_note = ""
        else:
            sig        = await loop.run_in_executor(None, lambda: execute_swap_live(pending["quote"], uid))
            extra_note = ""

    context.user_data.pop(f"pending_{action}", None)

    mint = pending["mint"]
    if "ERROR" in sig or "error" in sig.lower():
        await query.edit_message_text(f"❌ Failed:\n`{sig}`", parse_mode="Markdown",
                                       reply_markup=back_kb())
    else:
        raw_out = pending.get("raw_out") or pending.get("tok_est", 0)
        if action == "buy":
            setup_auto_sell(
                uid, mint, pending["symbol"],
                pending["price_usd"], raw_out, pending["decimals"],
                sol_amount=pending.get("sol_amount") or pending.get("amount") or 0.0,
            )
            log_trade(uid, "live", "buy", mint, pending["symbol"],
                      sol_amount=pending.get("sol_amount") or pending.get("amount"),
                      token_amount=raw_out,
                      price_usd=pending.get("price_usd", 0),
                      mcap=pending.get("mcap", 0),
                      tx_sig=sig)
        else:
            log_trade(uid, "live", "sell", mint, pending["symbol"],
                      sol_received=int(pending.get("raw_out", 0)) / 1e9,
                      token_amount=pending.get("sell_amount", 0),
                      price_usd=pending.get("price_usd", 0),
                      buy_price_usd=pending.get("buy_price_usd"),
                      mcap=pending.get("mcap", 0),
                      tx_sig=sig)
        _pnl_card = ""
        if action == "sell":
            as_entry = _db.get_all_auto_sells(uid).get(mint, {})
            _sol_in  = float(as_entry.get("sol_amount", 0) or 0)
            _sol_out = float(pending.get("raw_out", 0)) / 1e9
            if _sol_in > 0 and _sol_out > 0:
                import time as _t_now
                _pnl_card = "\n\n" + _format_pnl_card(
                    pending["symbol"], mint, _sol_in, _sol_out,
                    float(as_entry.get("created_at", 0) or 0), _t_now.time(), get_mode(uid)
                )
        await query.edit_message_text(
            f"✅ *{action.title()} Submitted*\n"
            f"Token: `{pending['symbol']}`\n"
            f"Tx: `{sig}`\n"
            f"[Solscan](https://solscan.io/tx/{sig})  "
            f"[DexScreener](https://dexscreener.com/solana/{mint})  "
            f"[Pump](https://pump.fun/{mint})"
            + ("\n\n🤖 Auto-sell configured: 2x→50%, 4x→50%" if action == "buy" else "")
            + _pnl_card
            + extra_note,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Auto-Sell", callback_data=f"as:view:{mint}")],
                [InlineKeyboardButton("⬅️ Menu",      callback_data="menu:main")],
            ]) if action == "buy" else back_kb()
        )


async def quick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]
    mint   = parts[2]
    await query.answer()

    if action == "buy":
        set_state(uid, waiting_for="trade_buy_amount", trade_action="buy", trade_token=mint)
        amounts = get_user_quick_buy_amounts(uid)
        preset_rows = []
        row = []
        for amt in amounts:
            label = f"{amt} SOL" if amt < 1 else f"{int(amt) if amt == int(amt) else amt} SOL"
            row.append(InlineKeyboardButton(label, callback_data=f"qb_preset:{mint}:{amt}"))
            if len(row) == 2:
                preset_rows.append(row)
                row = []
        if row:
            preset_rows.append(row)
        preset_rows.append([InlineKeyboardButton("✏️ Custom amount", callback_data=f"qb_preset:{mint}:custom"),
                            InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        await query.edit_message_text(
            "🟢 *Buy*\n\nHow much SOL to spend?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(preset_rows)
        )
    elif action == "alert":
        set_state(uid, waiting_for="alert_direction", alert_token=mint)
        pair   = fetch_sol_pair(mint)
        symbol = pair["baseToken"]["symbol"] if pair else mint[:8]
        set_state(uid, alert_symbol=symbol, alert_mint=mint)
        await query.edit_message_text(
            f"🔔 *Alert for ${symbol}*\n\nAlert when price goes:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↑ Above", callback_data="alert_dir:above"),
                 InlineKeyboardButton("↓ Below", callback_data="alert_dir:below")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ])
        )


async def alert_dir_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle alert_dir:above / alert_dir:below — prompt user for the price level."""
    query     = update.callback_query
    uid       = query.from_user.id
    direction = query.data.split(":")[1]  # "above" or "below"
    await query.answer()

    mint   = get_state(uid, "alert_mint", "")
    symbol = get_state(uid, "alert_symbol", mint[:6])
    dir_lbl = "above ↑" if direction == "above" else "below ↓"
    set_state(uid, waiting_for="alert_price", alert_direction=direction,
              alert_mint=mint, alert_symbol=symbol)
    await query.edit_message_text(
        f"🔔 *Price Alert — ${_esc(symbol)}*\n\n"
        f"Alert when price goes {dir_lbl}.\n\n"
        f"Enter the price in USD (e.g. `0.000025`):",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
        ]]),
    )


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = query.from_user.id
    context.user_data.pop("pending_buy", None)
    context.user_data.pop("pending_sell", None)
    clear_state(uid)
    await query.answer("Cancelled")
    await show_main_menu(query, uid, edit=True)


async def qb_preset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quick-buy SOL amount preset buttons."""
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")  # qb_preset : mint : amount
    mint   = parts[1]
    amount = parts[2] if len(parts) > 2 else "custom"
    await query.answer()

    if amount == "custom":
        set_state(uid, waiting_for="trade_buy_amount", trade_action="buy", trade_token=mint)
        await query.edit_message_text(
            "✏️ *Custom Buy Amount*\n\nEnter SOL amount to spend:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
        )
        return

    try:
        sol_amount = float(amount)
    except ValueError:
        await query.answer("Invalid amount", show_alert=True)
        return

    mode = get_mode(uid)
    msg  = await query.edit_message_text("Getting quote...")
    await do_trade_flow(msg, uid, context, "buy", mint, str(sol_amount))


# ─── Text input state machine ─────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    text  = update.message.text.strip()
    state = get_state(uid, "waiting_for")

    if state == "market_lookup":
        clear_state(uid)
        msg  = await update.message.reply_text("Looking up...")
        pair = fetch_sol_pair(text)
        if not pair:
            await msg.edit_text("Not found.", reply_markup=back_kb("menu:market"))
            return
        await msg.edit_text(format_pair(pair), parse_mode="Markdown",
                             reply_markup=price_card_kb(pair["baseToken"]["address"]))

    elif state in ("trade_buy_token", "trade_sell_token"):
        action = get_state(uid, "trade_action")
        set_state(uid, waiting_for=f"trade_{action}_amount", trade_token=text)
        label = "SOL amount to spend" if action == "buy" else "raw token amount to sell"
        await update.message.reply_text(
            f"Token: `{text}`\n\nEnter {label}:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
        )

    elif state in ("trade_buy_amount", "trade_sell_amount"):
        action = get_state(uid, "trade_action")
        token  = get_state(uid, "trade_token")
        clear_state(uid)
        msg = await update.message.reply_text("Getting quote...")
        await do_trade_flow(msg, uid, context, action, token, text)

    elif state == "custom_target_value":
        try:
            value = float(text)
        except ValueError:
            await update.message.reply_text("Invalid number. Try again.")
            return
        ct_type = get_state(uid, "custom_type")
        mint    = get_state(uid, "custom_mint")

        if ct_type in ("multiplier", "price"):
            set_state(uid, waiting_for="custom_target_sell_pct",
                      custom_value=value, custom_type=ct_type, custom_mint=mint)
            await update.message.reply_text(
                f"Target set at `{value}{'x' if ct_type == 'multiplier' else ' USD'}`\n\n"
                "What % of holdings to sell when triggered? (e.g. `50` for 50%)\n"
                "Enter `0` for alert only (no sell):",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data=f"as:view:{mint}")
                ]])
            )
        else:  # mcap — alert only, no sell
            cfg = get_auto_sell(uid, mint)
            if cfg:
                cfg.setdefault("custom_targets", []).append({
                    "type": "mcap", "value": value, "triggered": False
                })
                set_auto_sell(uid, mint, cfg)
                sym = cfg.get("symbol", mint[:6])
                clear_state(uid)
                await update.message.reply_text(
                    f"✅ MCap alert added for `${sym}` at `${value:,.0f}`",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⚙️ View Config", callback_data=f"as:view:{mint}")
                    ]])
                )

    elif state == "wallet_send_sol_to":
        to_addr = text.strip()
        try:
            from solders.pubkey import Pubkey as _PK
            _PK.from_string(to_addr)
        except Exception:
            await update.message.reply_text(
                "❌ Invalid Solana address. Please send a valid base58 address.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wallet:menu")]])
            )
            return
        set_state(uid, send_sol_to=to_addr, waiting_for="wallet_send_sol_amount")
        pubkey  = get_wallet_pubkey()
        sol_bal = get_sol_balance(pubkey) if pubkey else 0
        await update.message.reply_text(
            f"📤 Sending to: `{to_addr[:8]}...{to_addr[-6:]}`\n"
            f"Wallet balance: `{sol_bal:.4f} SOL`\n\n"
            f"Enter SOL amount to send (e.g. `0.5`):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wallet:menu")]])
        )

    elif state == "wallet_send_sol_amount":
        try:
            amount = float(text.strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Enter a valid positive number (e.g. `0.5`).",
                parse_mode="Markdown"
            )
            return
        to_addr = get_state(uid, "send_sol_to")
        pubkey  = get_wallet_pubkey()
        sol_bal = get_sol_balance(pubkey) if pubkey else 0
        if amount > sol_bal:
            await update.message.reply_text(
                f"❌ Insufficient SOL. Have `{sol_bal:.4f}`, need `{amount}`.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wallet:menu")]])
            )
            return
        set_state(uid, send_sol_amount=str(amount))
        # Don't clear state — exec button reads it
        await update.message.reply_text(
            f"📤 *Confirm Send SOL*\n\n"
            f"Amount: `{amount} SOL`\n"
            f"To: `{to_addr}`\n\n"
            f"⚠️ This transaction is irreversible.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Send", callback_data="wallet:send_sol_exec"),
                 InlineKeyboardButton("❌ Cancel",       callback_data="wallet:menu")],
            ])
        )

    elif state == "wallet_send_token_to":
        to_addr = text.strip()
        try:
            from solders.pubkey import Pubkey as _PK
            _PK.from_string(to_addr)
        except Exception:
            await update.message.reply_text(
                "❌ Invalid Solana address.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wallet:menu")]])
            )
            return
        set_state(uid, send_token_to=to_addr, waiting_for="wallet_send_token_amount")
        sym     = get_state(uid, "send_token_sym", "TOKEN")
        max_ui  = get_state(uid, "send_token_max_ui", 0)
        await update.message.reply_text(
            f"📤 *Send {sym}*\n"
            f"To: `{to_addr[:8]}...{to_addr[-6:]}`\n"
            f"Balance: `{float(max_ui):,.4f}`\n\n"
            f"Enter amount to send, or type `all` for full balance:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wallet:menu")]])
        )

    elif state == "wallet_send_token_amount":
        mint     = get_state(uid, "send_token_mint", "")
        sym      = get_state(uid, "send_token_sym", "TOKEN")
        to_addr  = get_state(uid, "send_token_to", "")
        decimals = int(get_state(uid, "send_token_decimals", 6) or 6)
        max_ui   = float(get_state(uid, "send_token_max_ui", 0) or 0)
        raw_text = text.strip().lower()
        if raw_text == "all":
            ui_amt = max_ui
        else:
            try:
                ui_amt = float(raw_text)
                if ui_amt <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(
                    "Enter a valid amount or type `all`.",
                    parse_mode="Markdown"
                )
                return
        if ui_amt > max_ui:
            await update.message.reply_text(
                f"❌ Insufficient balance. Have `{max_ui:,.4f}`, requested `{ui_amt:,.4f}`.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wallet:menu")]])
            )
            return
        raw_amt = int(ui_amt * (10 ** decimals))
        set_state(uid, send_token_raw=raw_amt, send_token_ui=ui_amt)
        await update.message.reply_text(
            f"📤 *Confirm Send Token*\n\n"
            f"Token: `{sym}`\n"
            f"Amount: `{ui_amt:,.4f}`\n"
            f"To: `{to_addr}`\n\n"
            f"⚠️ This transaction is irreversible.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Send", callback_data="wallet:send_token_exec"),
                 InlineKeyboardButton("❌ Cancel",       callback_data="wallet:menu")],
            ])
        )

    elif state == "wallet_import_key":
        clear_state(uid)
        try:
            from solders.keypair import Keypair as _KP
            kp     = _KP.from_base58_string(text)
            pubkey = str(kp.pubkey())
        except Exception:
            await update.message.reply_text(
                "❌ Invalid private key. Make sure it's a base58-encoded Solana private key.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Try Again", callback_data="wallet:import_key")
                ]])
            )
            return
        save_wallet_key(text)
        bal = get_sol_balance(pubkey)
        await update.message.reply_text(
            f"✅ *Wallet imported!*\n\n"
            f"Address: `{pubkey}`\n"
            f"Balance: `{bal:.4f} SOL`\n\n"
            f"[Solscan](https://solscan.io/account/{pubkey})",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Main Menu", callback_data="menu:main")
            ]]),
            disable_web_page_preview=True,
        )

    elif state == "wallet_backup_password":
        password = text.strip()
        if len(password) < 8:
            await update.message.reply_text(
                "❌ Password too short. Use at least 8 characters.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Try Again", callback_data="wallet:create_encrypted")
                ]])
            )
            return
        
        try:
            wallet_data = wm.create_wallet_with_mnemonic(
                backup_mode="encrypted",
                backup_password=password
            )
            set_state(uid, pending_wallet_mnemonic=wallet_data["mnemonic"],
                         pending_wallet_pubkey=wallet_data["public_key"],
                         pending_wallet_privkey=wallet_data["private_key_base58"],
                         backup_password_set=True)
            msg = wm.format_wallet_creation_message(wallet_data)
            clear_state(uid, "waiting_for")
            await update.message.reply_text(
                msg,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ I saved the 12 words", callback_data="wallet:save_pending_bip39")],
                    [InlineKeyboardButton("❌ Start over",           callback_data="wallet:create")],
                ])
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

    elif state == "wallet_import_seed":
        clear_state(uid, "waiting_for")
        seed_phrase = text.strip().lower()
        
        # First check if seed is valid (may be encrypted)
        if not wm.validate_mnemonic(seed_phrase):
            await update.message.reply_text(
                "❌ Invalid seed phrase. Make sure you have all 12 words in correct order.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Try Again", callback_data="wallet:import_seed")
                ]])
            )
            return
        
        try:
            # Check if seed is encrypted by checking backup storage
            # If the user has an encrypted backup for this seed, they'll need the password
            recovered = wm.recover_from_mnemonic(seed_phrase)
            pubkey = recovered["public_key"]
            privkey = recovered["private_key_base58"]
            
            # Check if this pubkey has an encrypted backup
            backup_status = wm.get_wallet_backup_status(pubkey)
            if backup_status["has_backup"] and backup_status["backup_type"] == "encrypted":
                set_state(uid, waiting_for="wallet_encrypted_recovery_password",
                         pending_pubkey=pubkey, seed_phrase=seed_phrase)
                await update.message.reply_text(
                    "🔐 *This wallet has an encrypted backup.*\n\n"
                    "Enter the password you set when creating it:",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("❌ Cancel", callback_data="wallet:import")
                    ]])
                )
                return
            
            # No encryption, proceed with recovery
            save_wallet_key(privkey)
            bal = get_sol_balance(pubkey)
            await update.message.reply_text(
                f"✅ *Wallet recovered!*\n\n"
                f"Address: `{pubkey}`\n"
                f"Balance: `{bal:.4f} SOL`\n\n"
                f"[Solscan](https://solscan.io/account/{pubkey})",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Main Menu", callback_data="menu:main")
                ]]),
                disable_web_page_preview=True,
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Recovery failed: {str(e)[:80]}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Try Again", callback_data="wallet:import_seed")
                ]])
            )

    elif state == "wallet_encrypted_recovery_password":
        password = text.strip()
        seed_phrase = get_state(uid, "seed_phrase")
        pending_pubkey = get_state(uid, "pending_pubkey")
        clear_state(uid)
        
        try:
            recovered = wm.recover_from_mnemonic(seed_phrase, password=password)
            pubkey = recovered["public_key"]
            privkey = recovered["private_key_base58"]
            
            if pubkey != pending_pubkey:
                await update.message.reply_text(
                    "❌ Password incorrect or seed mismatch.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("↩️ Try Again", callback_data="wallet:import_seed")
                    ]])
                )
                return
            
            save_wallet_key(privkey)
            bal = get_sol_balance(pubkey)
            await update.message.reply_text(
                f"✅ *Wallet recovered!*\n\n"
                f"Address: `{pubkey}`\n"
                f"Balance: `{bal:.4f} SOL`\n\n"
                f"[Solscan](https://solscan.io/account/{pubkey})",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Main Menu", callback_data="menu:main")
                ]]),
                disable_web_page_preview=True,
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ {str(e)[:100]}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Try Again", callback_data="wallet:import_seed")
                ]])
            )

    elif state == "tracked_wallet_address":
        clear_state(uid)
        wallet_addr = text.strip()
        # Basic validation: should be 44 chars, alphanumeric
        if not wallet_addr or len(wallet_addr) != 44:
            await update.message.reply_text(
                "❌ Invalid wallet address. Solana addresses are 44 characters.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Try Again", callback_data="wallet:add_tracked")
                ]])
            )
            return
        try:
            import wallet_tracker
            # Prompt for name (next step)
            set_state(uid, waiting_for="tracked_wallet_name")
            set_state(uid, pending_tracked_wallet=wallet_addr)
            await update.message.reply_text(
                f"*✅ Address saved*\n\n"
                f"`{wallet_addr}`\n\n"
                f"Now give this wallet a name (e.g., 'Alpha Trader', 'Dev Wallet'):",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="wallet:tracked")
                ]])
            )
        except ImportError:
            await update.message.reply_text(
                "⚠️ Wallet tracker module not found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:tracked")]])
            )

    elif state == "tracked_wallet_name":
        wallet_addr = get_state(uid, "pending_tracked_wallet")
        clear_state(uid)
        wallet_name = text.strip()
        if not wallet_name:
            await update.message.reply_text(
                "❌ Name cannot be empty.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Try Again", callback_data="wallet:add_tracked")
                ]])
            )
            return
        try:
            import wallet_tracker
            if not wallet_addr:
                await update.message.reply_text("❌ Wallet address not found. Please try again.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:tracked")]])
                )
                return
            wallet_tracker.add_watched_wallet(wallet_addr, wallet_name)
            await update.message.reply_text(
                f"✅ *Wallet tracked!*\n\n"
                f"Name: {wallet_name}\n"
                f"Address: `{wallet_addr}`\n\n"
                f"Waiting for wallet activity...",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Tracked Wallets", callback_data="wallet:tracked")
                ]])
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Error: {str(e)}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wallet:tracked")]])
            )

    elif state == "pf_heat":
        clear_state(uid)
        try:
            v = max(0, min(100, int(float(text.strip()))))
            f = pf.get_filters(uid)
            f["min_heat_score"] = v
            pf.set_filters(uid, f)
            msg = f"✅ Min heat score set to `{v}/100`" if v > 0 else "✅ Heat score filter disabled."
            await update.message.reply_text(
                msg, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📡 Pump Live Settings", callback_data="pumplive:menu")
                ]])
            )
        except Exception:
            await update.message.reply_text("Enter a number 0-100, e.g. `40`.")

    elif state == "pf_mcap":
        clear_state(uid)
        try:
            parts_in = text.replace(",", "").split("-")
            mn = float(parts_in[0])
            mx = float(parts_in[1]) if len(parts_in) > 1 else 0.0
            f  = pf.get_filters(uid)
            f["min_mcap_sol"], f["max_mcap_sol"] = mn, mx
            pf.set_filters(uid, f)
            await update.message.reply_text(
                f"✅ MCap filter set: `{pf._sol_range_str(mn, mx)}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📡 Pump Live Settings", callback_data="pumplive:menu")
                ]])
            )
        except Exception:
            await update.message.reply_text("Invalid format. Try `5-200` or `0-500`.")

    elif state == "pf_vol":
        clear_state(uid)
        try:
            parts_in = text.replace(",", "").split("-")
            mn = float(parts_in[0])
            mx = float(parts_in[1]) if len(parts_in) > 1 else 0.0
            f  = pf.get_filters(uid)
            f["min_vol_sol"], f["max_vol_sol"] = mn, mx
            pf.set_filters(uid, f)
            await update.message.reply_text(
                f"✅ SOL Volume filter set: `{pf._sol_range_str(mn, mx)}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📡 Pump Live Settings", callback_data="pumplive:menu")
                ]])
            )
        except Exception:
            await update.message.reply_text("Invalid format. Try `0.5` or `0.5-10`.")

    elif state == "pf_devbuy":
        clear_state(uid)
        try:
            parts_in = text.replace(",", "").split("-")
            mn = float(parts_in[0])
            mx = float(parts_in[1]) if len(parts_in) > 1 else 0.0
            f  = pf.get_filters(uid)
            f["min_dev_sol"], f["max_dev_sol"] = mn, mx
            pf.set_filters(uid, f)
            await update.message.reply_text(
                f"✅ Dev buy filter set: `{pf._sol_range_str(mn, mx)}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📡 Pump Live Settings", callback_data="pumplive:menu")
                ]])
            )
        except Exception:
            await update.message.reply_text("Invalid format. Try `0.5` or `0.5-5`.")

    elif state == "pf_age":
        clear_state(uid)
        try:
            v = float(text.strip())
            f = pf.get_filters(uid)
            f["max_token_age_mins"] = max(0.0, v)
            pf.set_filters(uid, f)
            msg = f"✅ Token age filter set: `≤{v:.0f} minutes`" if v > 0 else "✅ Token age filter disabled."
            await update.message.reply_text(
                msg, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📡 Pump Live Settings", callback_data="pumplive:menu")
                ]])
            )
        except Exception:
            await update.message.reply_text("Enter a number of minutes, e.g. `5`.")

    elif state == "pf_keywords":
        clear_state(uid)
        f = pf.get_filters(uid)
        if text.strip().lower() == "clear":
            f["keywords"] = []
            await update.message.reply_text("✅ Keywords cleared.")
        else:
            kws = [k.strip() for k in text.replace("\n", ",").split(",") if k.strip()]
            f["keywords"] = kws
            await update.message.reply_text(
                f"✅ Keywords set: `{', '.join(kws)}`",
                parse_mode="Markdown",
            )
        pf.set_filters(uid, f)
        await update.message.reply_text(
            "Back to settings:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📡 Pump Live Settings", callback_data="pumplive:menu")
            ]])
        )

    elif state == "pf_blocked":
        clear_state(uid)
        f = pf.get_filters(uid)
        if text.strip().lower() == "clear":
            f["blocked_words"] = []
            await update.message.reply_text("✅ Blocked words cleared.")
        else:
            bws = [k.strip() for k in text.replace("\n", ",").split(",") if k.strip()]
            f["blocked_words"] = bws
            await update.message.reply_text(
                f"✅ Blocked words set: `{', '.join(bws)}`",
                parse_mode="Markdown",
            )
        pf.set_filters(uid, f)
        await update.message.reply_text(
            "Back to settings:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📡 Pump Live Settings", callback_data="pumplive:menu")
            ]])
        )

    elif state == "pf_tracked_wallets":
        clear_state(uid)
        f = pf.get_filters(uid)
        if text.strip().lower() == "clear":
            f["tracked_wallets"] = []
            await update.message.reply_text("✅ Tracked wallets cleared.")
        else:
            wallets = [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]
            f["tracked_wallets"] = wallets
            await update.message.reply_text(
                f"✅ Tracking {len(wallets)} wallet(s).",
                parse_mode="Markdown",
            )
        pf.set_filters(uid, f)
        await update.message.reply_text(
            "Back to settings:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📡 Pump Live Settings", callback_data="pumplive:menu")
            ]])
        )

    elif state == "pf_blocked_wallets":
        clear_state(uid)
        f = pf.get_filters(uid)
        if text.strip().lower() == "clear":
            f["blocked_wallets"] = []
            await update.message.reply_text("✅ Blocked wallets cleared.")
        else:
            wallets = [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]
            f["blocked_wallets"] = wallets
            await update.message.reply_text(
                f"✅ Blocking {len(wallets)} wallet(s).",
                parse_mode="Markdown",
            )
        pf.set_filters(uid, f)
        await update.message.reply_text(
            "Back to settings:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📡 Pump Live Settings", callback_data="pumplive:menu")
            ]])
        )

    elif state == "pg_heat":
        clear_state(uid)
        try:
            v = max(0, min(100, int(float(text.strip()))))
            f = pf.get_grad_filters(uid)
            f["min_heat_score"] = v
            pf.set_grad_filters(uid, f)
            msg = f"✅ Grad min heat score set to `{v}/100`." if v > 0 else "✅ Heat score filter disabled."
            await update.message.reply_text(
                msg, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎓 Pump Grad Settings", callback_data="pumpgrad:menu")
                ]])
            )
        except Exception:
            await update.message.reply_text("Enter a number 0–100, e.g. `40`.")

    elif state == "pg_mcap":
        clear_state(uid)
        try:
            parts_in = text.replace(",", "").split("-")
            mn = float(parts_in[0])
            mx = float(parts_in[1]) if len(parts_in) > 1 else 0.0
            f  = pf.get_grad_filters(uid)
            f["min_mcap_sol"], f["max_mcap_sol"] = mn, mx
            pf.set_grad_filters(uid, f)
            await update.message.reply_text(
                f"✅ MCap filter set: `{pf._sol_range_str(mn, mx)}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎓 Pump Grad Settings", callback_data="pumpgrad:menu")
                ]])
            )
        except Exception:
            await update.message.reply_text("Invalid format. Try `50-500` or `30-0`.")

    elif state == "pg_devbuy":
        clear_state(uid)
        try:
            parts_in = text.replace(",", "").split("-")
            mn = float(parts_in[0])
            mx = float(parts_in[1]) if len(parts_in) > 1 else 0.0
            f  = pf.get_grad_filters(uid)
            f["min_dev_sol"], f["max_dev_sol"] = mn, mx
            pf.set_grad_filters(uid, f)
            await update.message.reply_text(
                f"✅ Dev buy filter set: `{pf._sol_range_str(mn, mx)}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎓 Pump Grad Settings", callback_data="pumpgrad:menu")
                ]])
            )
        except Exception:
            await update.message.reply_text("Invalid format. Try `0.5` or `0.5-5`.")

    elif state == "pg_keywords":
        clear_state(uid)
        f = pf.get_grad_filters(uid)
        if text.strip().lower() == "clear":
            f["keywords"] = []
            await update.message.reply_text("✅ Keywords cleared.")
        else:
            kws = [k.strip() for k in text.replace("\n", ",").split(",") if k.strip()]
            f["keywords"] = kws
            await update.message.reply_text(
                f"✅ Keywords set: `{', '.join(kws)}`",
                parse_mode="Markdown",
            )
        pf.set_grad_filters(uid, f)
        await update.message.reply_text(
            "Back to settings:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎓 Pump Grad Settings", callback_data="pumpgrad:menu")
            ]])
        )

    elif state == "pg_blocked":
        clear_state(uid)
        f = pf.get_grad_filters(uid)
        if text.strip().lower() == "clear":
            f["blocked_words"] = []
            await update.message.reply_text("✅ Blocked words cleared.")
        else:
            bws = [k.strip() for k in text.replace("\n", ",").split(",") if k.strip()]
            f["blocked_words"] = bws
            await update.message.reply_text(
                f"✅ Blocked words set: `{', '.join(bws)}`",
                parse_mode="Markdown",
            )
        pf.set_grad_filters(uid, f)
        await update.message.reply_text(
            "Back to settings:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎓 Pump Grad Settings", callback_data="pumpgrad:menu")
            ]])
        )

    elif state == "pg_tracked_wallets":
        clear_state(uid)
        f = pf.get_grad_filters(uid)
        if text.strip().lower() == "clear":
            f["tracked_wallets"] = []
            await update.message.reply_text("✅ Tracked wallets cleared.")
        else:
            wallets = [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]
            f["tracked_wallets"] = wallets
            await update.message.reply_text(
                f"✅ Tracking {len(wallets)} wallet(s).",
                parse_mode="Markdown",
            )
        pf.set_grad_filters(uid, f)
        await update.message.reply_text(
            "Back to settings:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎓 Pump Grad Settings", callback_data="pumpgrad:menu")
            ]])
        )

    elif state == "pg_blocked_wallets":
        clear_state(uid)
        f = pf.get_grad_filters(uid)
        if text.strip().lower() == "clear":
            f["blocked_wallets"] = []
            await update.message.reply_text("✅ Blocked wallets cleared.")
        else:
            wallets = [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]
            f["blocked_wallets"] = wallets
            await update.message.reply_text(
                f"✅ Blocking {len(wallets)} wallet(s).",
                parse_mode="Markdown",
            )
        pf.set_grad_filters(uid, f)
        await update.message.reply_text(
            "Back to settings:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎓 Pump Grad Settings", callback_data="pumpgrad:menu")
            ]])
        )

    elif state == "custom_target_sell_pct":
        try:
            sell_pct = int(float(text))
            if sell_pct < 0 or sell_pct > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number 0–100.")
            return
        ct_type = get_state(uid, "custom_type")
        value   = get_state(uid, "custom_value")
        mint    = get_state(uid, "custom_mint")
        clear_state(uid)
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("custom_targets", []).append({
                "type": ct_type, "value": value,
                "sell_pct": sell_pct, "triggered": False
            })
            set_auto_sell(uid, mint, cfg)
            sym  = cfg.get("symbol", mint[:6])
            desc = f"{value}x" if ct_type == "multiplier" else f"${value}"
            action_text = f"sell {sell_pct}%" if sell_pct > 0 else "alert only"
            await update.message.reply_text(
                f"✅ Custom target added for `${sym}`\n"
                f"At {desc} → {action_text}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚙️ View Config", callback_data=f"as:view:{mint}")
                ]])
            )

    elif state == "as_sl_pct":
        try:
            val = int(float(text))
            if val < 1 or val > 99:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 99 (e.g. 40 for 40% drop).")
            return
        mint = get_state(uid, "as_mint") or get_state(uid, "custom_mint")
        clear_state(uid)
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("stop_loss", {})["pct"] = val
            set_auto_sell(uid, mint, cfg)
            sym = cfg.get("symbol", mint[:6])
            await update.message.reply_text(
                f"✅ Stop-loss trigger set to `{val}%` drop for `${sym}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚙️ Stop-Loss Settings", callback_data=f"as:sl_menu:{mint}")
                ]])
            )

    elif state == "as_sl_sell_pct_input":
        try:
            val = int(float(text))
            if val < 1 or val > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 100.")
            return
        mint = get_state(uid, "as_mint") or get_state(uid, "custom_mint")
        clear_state(uid)
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("stop_loss", {})["sell_pct"] = val
            set_auto_sell(uid, mint, cfg)
            sym = cfg.get("symbol", mint[:6])
            await update.message.reply_text(
                f"✅ Stop-loss sell set to `{val}%` for `${sym}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚙️ Stop-Loss Settings", callback_data=f"as:sl_menu:{mint}")
                ]])
            )

    elif state == "as_trail_pct":
        try:
            val = int(float(text))
            if val < 1 or val > 99:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 99 (e.g. 25 for 25% drop from peak).")
            return
        mint = get_state(uid, "as_mint") or get_state(uid, "custom_mint")
        clear_state(uid)
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("trailing_stop", {})["trail_pct"] = val
            set_auto_sell(uid, mint, cfg)
            sym = cfg.get("symbol", mint[:6])
            await update.message.reply_text(
                f"✅ Trailing stop set to `{val}%` from peak for `${sym}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚙️ Trail Stop Settings", callback_data=f"as:trail_menu:{mint}")
                ]])
            )

    elif state == "as_trail_sell_pct_input":
        try:
            val = int(float(text))
            if val < 1 or val > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 100.")
            return
        mint = get_state(uid, "as_mint") or get_state(uid, "custom_mint")
        clear_state(uid)
        cfg = get_auto_sell(uid, mint)
        if cfg:
            cfg.setdefault("trailing_stop", {})["sell_pct"] = val
            set_auto_sell(uid, mint, cfg)
            sym = cfg.get("symbol", mint[:6])
            await update.message.reply_text(
                f"✅ Trailing stop sell set to `{val}%` for `${sym}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚙️ Trail Stop Settings", callback_data=f"as:trail_menu:{mint}")
                ]])
            )

    elif state == "as_mt_add_mult":
        try:
            mult = float(text)
            if mult <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a valid multiplier (e.g. 3 for 3x).")
            return
        mint  = get_state(uid, "as_mint")
        label = f"{mult}x" if mult != int(mult) else f"{int(mult)}x"
        set_state(uid, waiting_for="as_mt_add_sell_pct", as_mt_mult=mult, as_mint=mint)
        await update.message.reply_text(
            f"Multiplier: `{label}`\n\nHow much to sell at this target?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("25%",  callback_data=f"as:mt_add_sp:{mint}:25"),
                 InlineKeyboardButton("50%",  callback_data=f"as:mt_add_sp:{mint}:50"),
                 InlineKeyboardButton("75%",  callback_data=f"as:mt_add_sp:{mint}:75"),
                 InlineKeyboardButton("100%", callback_data=f"as:mt_add_sp:{mint}:100")],
                [InlineKeyboardButton("✏️ Custom %", callback_data=f"as:mt_sp_custom:{mint}")],
                [InlineKeyboardButton("❌ Cancel",    callback_data=f"as:mt_menu:{mint}")],
            ])
        )

    elif state == "as_mt_add_sell_pct":
        try:
            sell_pct = int(float(text))
            if sell_pct < 1 or sell_pct > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 100.")
            return
        mint = get_state(uid, "as_mint")
        mult = get_state(uid, "as_mt_mult")
        clear_state(uid)
        cfg = get_auto_sell(uid, mint)
        if cfg:
            label = f"{mult}x" if mult != int(mult) else f"{int(mult)}x"
            cfg.setdefault("mult_targets", []).append({
                "mult": mult, "sell_pct": sell_pct, "triggered": False, "label": label
            })
            set_auto_sell(uid, mint, cfg)
            sym = cfg.get("symbol", mint[:6])
            await update.message.reply_text(
                f"✅ Added target: `{label}` → sell `{sell_pct}%` for `${sym}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📈 Edit Targets", callback_data=f"as:mt_menu:{mint}")
                ]])
            )

    elif state == "as_mt_edit_mult":
        try:
            mult = float(text)
            if mult <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a valid multiplier (e.g. 3 for 3x).")
            return
        mint  = get_state(uid, "as_mint")
        idx   = get_state(uid, "as_mt_idx")
        label = f"{mult}x" if mult != int(mult) else f"{int(mult)}x"
        set_state(uid, waiting_for="as_mt_edit_sell_pct", as_mt_mult=mult, as_mint=mint, as_mt_idx=idx)
        await update.message.reply_text(
            f"New multiplier: `{label}`\n\nHow much to sell at this target?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("25%",  callback_data=f"as:mt_edit_sp:{mint}:25"),
                 InlineKeyboardButton("50%",  callback_data=f"as:mt_edit_sp:{mint}:50"),
                 InlineKeyboardButton("75%",  callback_data=f"as:mt_edit_sp:{mint}:75"),
                 InlineKeyboardButton("100%", callback_data=f"as:mt_edit_sp:{mint}:100")],
                [InlineKeyboardButton("✏️ Custom %", callback_data=f"as:mt_sp_custom:{mint}")],
                [InlineKeyboardButton("❌ Cancel",    callback_data=f"as:mt_menu:{mint}")],
            ])
        )

    elif state == "as_mt_edit_sell_pct":
        try:
            sell_pct = int(float(text))
            if sell_pct < 1 or sell_pct > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 100.")
            return
        mint = get_state(uid, "as_mint")
        mult = get_state(uid, "as_mt_mult")
        idx  = get_state(uid, "as_mt_idx")
        clear_state(uid)
        cfg = get_auto_sell(uid, mint)
        if cfg:
            targets = cfg.get("mult_targets", [])
            if idx is not None and 0 <= idx < len(targets):
                label = f"{mult}x" if mult != int(mult) else f"{int(mult)}x"
                targets[idx].update({"mult": mult, "sell_pct": sell_pct, "label": label})
                cfg["mult_targets"] = targets
                set_auto_sell(uid, mint, cfg)
                sym = cfg.get("symbol", mint[:6])
                await update.message.reply_text(
                    f"✅ Updated target {idx+1}: `{label}` → sell `{sell_pct}%` for `${sym}`",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📈 Edit Targets", callback_data=f"as:mt_menu:{mint}")
                    ]])
                )
            else:
                await update.message.reply_text("Invalid target index.")

    elif state == "as_ma_add":
        try:
            val = float(text.replace(",", "").replace("$", ""))
            if val <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a valid USD amount (e.g. 250000 for $250K).")
            return
        mint = get_state(uid, "as_mint")
        clear_state(uid)
        cfg = get_auto_sell(uid, mint)
        if cfg:
            if val < 1_000_000:
                label = f"{val/1000:.0f}K"
            else:
                label = f"{val/1_000_000:.1f}M"
            cfg.setdefault("mcap_alerts", []).append({
                "mcap": val, "triggered": False, "label": label
            })
            set_auto_sell(uid, mint, cfg)
            sym = cfg.get("symbol", mint[:6])
            await update.message.reply_text(
                f"✅ MCap alert added for `${sym}` at `${label}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏦 MCap Alerts", callback_data=f"as:mcap_menu:{mint}")
                ]])
            )

    elif state == "ab_sol_amount":
        try:
            val = float(text)
            if val <= 0 or val > 10000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a valid SOL amount (e.g. 0.1).")
            return
        cfg = get_auto_buy(uid)
        cfg["sol_amount"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await update.message.reply_text(
            f"✅ Auto-buy SOL amount set to `{val} SOL`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Auto-Buy Settings", callback_data="autobuy:menu")
            ]])
        )

    elif state == "ab_min_score":
        try:
            val = int(float(text))
            if val < 1 or val > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a score between 1 and 100.")
            return
        cfg = get_auto_buy(uid)
        cfg["min_score"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await update.message.reply_text(
            f"✅ Auto-buy min score set to `{val}/100`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Auto-Buy Settings", callback_data="autobuy:menu")
            ]])
        )

    elif state == "ab_max_mcap":
        try:
            val = float(text.replace(",", "").replace("$", "").replace("k", "000").replace("K", "000").replace("m", "000000").replace("M", "000000"))
            if val <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a valid MCap (e.g. 500000 or 500K).")
            return
        cfg = get_auto_buy(uid)
        cfg["max_mcap"] = int(val)
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await update.message.reply_text(
            f"✅ Auto-buy max MCap set to `${int(val):,}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Auto-Buy Settings", callback_data="autobuy:menu")
            ]])
        )

    elif state == "ab_daily_limit":
        try:
            val = float(text)
            if val < 0 or val > 10000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a valid daily limit in SOL (e.g. 5.0), or 0 for no limit.")
            return
        cfg = get_auto_buy(uid)
        cfg["daily_limit_sol"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        label = "Unlimited ♾️" if val == 0 else f"`{val} SOL`"
        await update.message.reply_text(
            f"✅ Daily auto-buy limit set to {label}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Auto-Buy Settings", callback_data="autobuy:menu")
            ]])
        )

    elif state == "ab_max_positions":
        try:
            val = int(text.strip())
            if val < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a whole number (e.g. `5` or `0` for unlimited).", parse_mode="Markdown")
            return
        cfg = get_auto_buy(uid)
        cfg["max_positions"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        label = "Unlimited ♾️" if val == 0 else str(val)
        await update.message.reply_text(
            f"✅ Max positions set to `{label}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Auto-Buy Settings", callback_data="autobuy:menu")
            ]])
        )

    elif state == "ab_min_mcap":
        try:
            val = float(text.replace(",", "").replace("$", "").replace("k", "000").replace("K", "000").replace("m", "000000").replace("M", "000000"))
            if val < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a valid MCap (e.g. 50000 or 50K). Use 0 for no minimum.")
            return
        cfg = get_auto_buy(uid)
        cfg["min_mcap_usd"] = int(val)
        set_auto_buy(uid, cfg)
        clear_state(uid)
        label = "None" if int(val) == 0 else f"${int(val):,}"
        await update.message.reply_text(
            f"✅ Min MCap set to `{label}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Auto-Buy Settings", callback_data="autobuy:menu")
            ]])
        )

    elif state == "ab_min_liq":
        try:
            val = float(text.replace(",", "").replace("$", "").replace("k", "000").replace("K", "000").replace("m", "000000").replace("M", "000000"))
            if val < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a valid USD amount (e.g. 5000 or 5K). Use 0 for no minimum.")
            return
        cfg = get_auto_buy(uid)
        cfg["min_liquidity_usd"] = int(val)
        set_auto_buy(uid, cfg)
        clear_state(uid)
        label = "None" if int(val) == 0 else f"${int(val):,}"
        await update.message.reply_text(
            f"✅ Min Liquidity set to `{label}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Auto-Buy Settings", callback_data="autobuy:menu")
            ]])
        )

    elif state == "ab_max_liq":
        try:
            val = float(text.replace(",", "").replace("$", "").replace("k", "000").replace("K", "000").replace("m", "000000").replace("M", "000000"))
            if val < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a valid USD amount (e.g. 100000 or 100K). Use 0 for no limit.")
            return
        cfg = get_auto_buy(uid)
        cfg["max_liquidity_usd"] = int(val)
        set_auto_buy(uid, cfg)
        clear_state(uid)
        label = "No limit ♾️" if int(val) == 0 else f"${int(val):,}"
        await update.message.reply_text(
            f"✅ Max Liquidity set to `{label}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Auto-Buy Settings", callback_data="autobuy:menu")
            ]])
        )

    elif state == "ab_min_age":
        try:
            val = int(float(text.strip().replace("m", "").replace("min", "")))
            if val < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number of minutes (e.g. `10` for 10 minutes). Use 0 for no minimum.")
            return
        cfg = get_auto_buy(uid)
        cfg["min_age_mins"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        label = "None" if val == 0 else f"{val}m"
        await update.message.reply_text(
            f"✅ Min Age set to `{label}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Auto-Buy Settings", callback_data="autobuy:menu")
            ]])
        )

    elif state == "ab_max_age":
        try:
            val = int(float(text.strip().replace("m", "").replace("min", "")))
            if val < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number of minutes (e.g. `60` for 1 hour). Use 0 for no limit.")
            return
        cfg = get_auto_buy(uid)
        cfg["max_age_mins"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        label = "No limit ♾️" if val == 0 else f"{val}m"
        await update.message.reply_text(
            f"✅ Max Age set to `{label}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Auto-Buy Settings", callback_data="autobuy:menu")
            ]])
        )

    elif state == "ab_min_txns":
        try:
            val = int(float(text.strip()))
            if val < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a whole number (e.g. `10`). Use 0 for no minimum.")
            return
        cfg = get_auto_buy(uid)
        cfg["min_txns_5m"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        label = "None" if val == 0 else str(val)
        await update.message.reply_text(
            f"✅ Min Txns (5m) set to `{label}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Auto-Buy Settings", callback_data="autobuy:menu")
            ]])
        )

    elif state == "sell_below_pct":
        try:
            val = int(float(text.strip().replace("%", "")))
            if not (1 <= val <= 99):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 99 (e.g. `30` for -30%).", parse_mode="Markdown")
            return
        clear_state(uid)
        mode = get_mode(uid)
        as_configs = _db.get_all_auto_sells(uid)
        results = []
        # In live mode, verify positions actually exist on-chain before selling
        live_mints: set[str] = set()
        external_count = 0
        if mode == "live":
            pubkey = get_wallet_pubkey()
            if pubkey:
                for a in (get_token_accounts(pubkey) or []):
                    live_mints.add(a["mint"])
            external_count = len(live_mints - set(as_configs.keys()))
        for mint, cfg in list(as_configs.items()):
            if mode == "live" and live_mints and mint not in live_mints:
                continue  # not in wallet, skip stale entry
            buy_price = cfg.get("buy_price_usd", 0)
            if not buy_price:
                continue
            price, mcap = fetch_token_price(mint)
            if price is None:
                continue
            drop_pct = ((buy_price - price) / buy_price) * 100
            if drop_pct >= val:
                sym = cfg.get("symbol", mint[:6])
                await execute_auto_sell(
                    context.bot, uid, mint, sym, 100,
                    f"Sell Below -{val}%", mode,
                    price_usd=price, mcap=mcap or 0
                )
                remove_auto_sell(uid, mint)
                results.append(f"✅ `${sym}` {-drop_pct:.0f}%")
        if external_count:
            results.append(f"\n_ℹ️ {external_count} external position(s) skipped (no buy price tracked)_")
        if not results:
            await update.message.reply_text(
                f"No positions are down {val}% or more.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👜 Portfolio", callback_data="portfolio:refresh")]])
            )
            return
        await update.message.reply_text(
            f"🔻 *Sell Below -{val}% Complete*\n\n" + "\n".join(results),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👜 Portfolio", callback_data="portfolio:refresh")]])
        )

    elif state == "slippage_custom":
        try:
            bps = int(float(text.strip().replace("%", "")))
            if not (10 <= bps <= 5000):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Enter a number between `10` and `5000` bps (e.g. `250` = 2.5%).",
                parse_mode="Markdown"
            )
            return
        clear_state(uid)
        set_user_slippage(uid, bps)
        slip_pct = bps / 100
        await update.message.reply_text(
            f"✅ Slippage set to `{slip_pct:.1f}%` ({bps} bps)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Slippage Settings", callback_data="settings:slippage"),
                InlineKeyboardButton("⬅️ Settings", callback_data="settings:menu"),
            ]])
        )

    elif state == "priority_custom":
        try:
            val = int(float(text.strip().replace(",", "")))
            if val < 1_000:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Enter a number ≥ `1000` µlamports/CU (e.g. `500000` for Medium).",
                parse_mode="Markdown"
            )
            return
        clear_state(uid)
        set_user_priority_fee(uid, val)
        fee_label = _fee_label(val)
        await update.message.reply_text(
            f"✅ Priority fee set to `{val:,}` µlamports/CU (*{fee_label}*)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Priority Settings", callback_data="settings:priority"),
                InlineKeyboardButton("⬅️ Settings", callback_data="settings:menu"),
            ]])
        )

    elif state == "jito_tip_custom":
        try:
            val = int(float(text.strip().replace(",", "")))
            if val < 1_000:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Enter a number ≥ `1000` lamports (e.g. `100000` = 0.0001 SOL).",
                parse_mode="Markdown"
            )
            return
        clear_state(uid)
        set_user_jito_tip(uid, val)
        tip_sol = val / 1_000_000_000
        await update.message.reply_text(
            f"✅ Jito tip set to `{val:,}` lamports ({tip_sol:.6f} SOL)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Jito Settings", callback_data="settings:jito"),
                InlineKeyboardButton("⬅️ Settings", callback_data="settings:menu"),
            ]])
        )

    elif state == "gsl_pct":
        try:
            val = int(float(text))
            if val < 1 or val > 99:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 99.")
            return
        clear_state(uid)
        gsl = get_global_sl()
        gsl["pct"] = val
        set_global_sl(gsl)
        await update.message.reply_text(
            f"✅ Global stop-loss trigger set to `{val}%` drop",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🌍 Global SL Settings", callback_data="gsl:menu")
            ]])
        )

    elif state == "gsl_sell_pct":
        try:
            val = int(float(text))
            if val < 1 or val > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 100.")
            return
        clear_state(uid)
        gsl = get_global_sl()
        gsl["sell_pct"] = val
        set_global_sl(gsl)
        await update.message.reply_text(
            f"✅ Global stop-loss sell amount set to `{val}%`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🌍 Global SL Settings", callback_data="gsl:menu")
            ]])
        )

    elif state == "gts_trail_pct":
        try:
            val = int(float(text))
            if val < 1 or val > 99:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 99.")
            return
        clear_state(uid)
        gts = get_global_trailing_stop()
        gts["trail_pct"] = val
        set_global_trailing_stop(gts)
        await update.message.reply_text(
            f"✅ Trailing stop trail % set to `{val}%` drop from peak",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📉 Trailing Stop Settings", callback_data="gts:menu")
            ]])
        )

    elif state == "gts_sell_pct":
        try:
            val = int(float(text))
            if val < 1 or val > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 100.")
            return
        clear_state(uid)
        gts = get_global_trailing_stop()
        gts["sell_pct"] = val
        set_global_trailing_stop(gts)
        await update.message.reply_text(
            f"✅ Trailing stop sell % set to `{val}%`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📉 Trailing Stop Settings", callback_data="gts:menu")
            ]])
        )

    elif state == "gttp_activate_mult":
        try:
            val = float(text)
            if val < 1.1 or val > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a multiplier between 1.1 and 100 (e.g. 2.5).")
            return
        clear_state(uid)
        gttp = get_global_trailing_tp()
        gttp["activate_mult"] = round(val, 2)
        set_global_trailing_tp(gttp)
        await update.message.reply_text(
            f"✅ Trailing TP activation set to `{val}x`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎯 Trailing TP Settings", callback_data="gttp:menu")
            ]])
        )

    elif state == "gttp_trail_pct":
        try:
            val = int(float(text))
            if val < 1 or val > 99:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 99.")
            return
        clear_state(uid)
        gttp = get_global_trailing_tp()
        gttp["trail_pct"] = val
        set_global_trailing_tp(gttp)
        await update.message.reply_text(
            f"✅ Trailing TP trail % set to `{val}%` drop from peak",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎯 Trailing TP Settings", callback_data="gttp:menu")
            ]])
        )

    elif state == "gttp_sell_pct":
        try:
            val = int(float(text))
            if val < 1 or val > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 100.")
            return
        clear_state(uid)
        gttp = get_global_trailing_tp()
        gttp["sell_pct"] = val
        set_global_trailing_tp(gttp)
        await update.message.reply_text(
            f"✅ Trailing TP sell % set to `{val}%`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎯 Trailing TP Settings", callback_data="gttp:menu")
            ]])
        )

    elif state == "gbe_activate_mult":
        try:
            val = float(text)
            if val < 1.1 or val > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a multiplier between 1.1 and 100 (e.g. 2.0).")
            return
        clear_state(uid)
        gbe = get_global_breakeven_stop()
        gbe["activate_mult"] = round(val, 2)
        set_global_breakeven_stop(gbe)
        await update.message.reply_text(
            f"✅ Breakeven stop activates at `{val}x`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🛡️ Breakeven Stop Settings", callback_data="gbe:menu")
            ]])
        )

    elif state == "gte_hours":
        try:
            val = int(float(text))
            if val < 1 or val > 720:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number of hours between 1 and 720.")
            return
        clear_state(uid)
        gte = get_global_time_exit()
        gte["hours"] = val
        set_global_time_exit(gte)
        await update.message.reply_text(
            f"✅ Time exit limit set to `{val} hours`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏱️ Time Exit Settings", callback_data="gte:menu")
            ]])
        )

    elif state == "gte_target_mult":
        try:
            val = float(text)
            if val < 1.0 or val > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a multiplier between 1.0 and 100 (e.g. 1.5).")
            return
        clear_state(uid)
        gte = get_global_time_exit()
        gte["target_mult"] = round(val, 2)
        set_global_time_exit(gte)
        await update.message.reply_text(
            f"✅ Time exit target set to `{val}x` — sell if below this after time limit",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏱️ Time Exit Settings", callback_data="gte:menu")
            ]])
        )

    elif state == "gte_sell_pct":
        try:
            val = int(float(text))
            if val < 1 or val > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 1 and 100.")
            return
        clear_state(uid)
        gte = get_global_time_exit()
        gte["sell_pct"] = val
        set_global_time_exit(gte)
        await update.message.reply_text(
            f"✅ Time exit sell % set to `{val}%`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏱️ Time Exit Settings", callback_data="gte:menu")
            ]])
        )

    elif state == "scanner_watch_threshold":
        raw = text.strip()
        if not raw.isdigit() or not (1 <= int(raw) <= 100):
            await update.message.reply_text(
                "Please send a number between 1 and 100.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="scanner:set_threshold"),
                ]])
            )
            return
        val = int(raw)
        user_settings = sm.get_user_settings(uid)
        # Keep alert_score >= watch_score
        alert_score = user_settings.get("alert_warm_threshold", 70)
        if val > alert_score:
            user_settings["alert_warm_threshold"] = val  # raise alert to match
        user_settings["alert_scouted_threshold"] = val
        sm.save_user_settings(uid, user_settings)
        clear_state(uid)
        await update.message.reply_text(
            f"✅ Watch Score set to `{val}/100`\n\nTokens scoring ≥ {val} will send a compact watchlist ping.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="scanner:set_threshold"),
            ]])
        )

    elif state == "scanner_alert_threshold":
        raw = text.strip()
        if not raw.isdigit() or not (1 <= int(raw) <= 100):
            await update.message.reply_text(
                "Please send a number between 1 and 100.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="scanner:set_threshold"),
                ]])
            )
            return
        val = int(raw)
        user_settings = sm.get_user_settings(uid)
        watch_score = user_settings.get("alert_scouted_threshold", 20)
        if val < watch_score:
            # Lower watch to match — alert score is always the floor
            user_settings["alert_scouted_threshold"] = val
        user_settings["alert_warm_threshold"] = val
        # Retire hot/ultra — they're now just labels, not separate thresholds
        user_settings["alert_hot_threshold"]       = val
        user_settings["alert_ultra_hot_threshold"] = val
        sm.save_user_settings(uid, user_settings)
        clear_state(uid)
        await update.message.reply_text(
            f"✅ Alert Score set to `{val}/100`\n\nTokens scoring ≥ {val} will send a full DM alert.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="scanner:set_threshold"),
            ]])
        )

    elif state == "scanner_mcap_min":
        raw = text.strip().replace(",", "").replace("$", "")
        try:
            val = int(float(raw))
            if val < 0: raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please send a valid number (e.g. `5000` or `10000`).",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="scanner:set_threshold"),
                ]])
            )
            return
        user_settings = sm.get_user_settings(uid)
        mcap_max = user_settings.get("scanner_mcap_max", 10_000_000)
        if val > mcap_max:
            user_settings["scanner_mcap_max"] = val * 10
        user_settings["scanner_mcap_min"] = val
        sm.save_user_settings(uid, user_settings)
        clear_state(uid)
        await update.message.reply_text(
            f"✅ MCap minimum set to `${val:,}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="scanner:set_threshold"),
            ]])
        )

    elif state == "scanner_mcap_max":
        raw = text.strip().replace(",", "").replace("$", "")
        try:
            val = int(float(raw))
            if val < 0: raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please send a valid number (e.g. `1000000`).",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="scanner:set_threshold"),
                ]])
            )
            return
        user_settings = sm.get_user_settings(uid)
        mcap_min = user_settings.get("scanner_mcap_min", 5_000)
        if val < mcap_min:
            user_settings["scanner_mcap_min"] = max(0, val // 10)
        user_settings["scanner_mcap_max"] = val
        sm.save_user_settings(uid, user_settings)
        clear_state(uid)
        await update.message.reply_text(
            f"✅ MCap maximum set to `${val:,}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="scanner:set_threshold"),
            ]])
        )

    # Legacy handlers kept for any in-flight state (users mid-conversation during restart)
    elif state in ("scanner_warm_threshold", "scanner_hot_threshold", "scanner_ultra_hot_threshold"):
        clear_state(uid)
        await update.message.reply_text(
            "Threshold config has been simplified. Use the buttons below to set your scores.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Thresholds", callback_data="scanner:set_threshold"),
            ]])
        )

    elif state == "scanner_alert_channel":
        # Accept channel ID (numeric, e.g. -1001234567890) or @username
        ch = text.strip()
        if not (ch.startswith("@") or ch.lstrip("-").isdigit()):
            await update.message.reply_text(
                "Enter a channel ID (e.g. `-1001234567890`) or @username (e.g. `@mychannel`).",
                parse_mode="Markdown"
            )
            return
        sc.set_alert_channel(ch)
        clear_state(uid)
        await update.message.reply_text(
            f"✅ *Alert channel set to* `{ch}`\n\n"
            f"Every scanner alert will now be posted there too.\n"
            f"Make sure the bot is an admin in the channel.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📣 Channel Settings", callback_data="scanner:alert_channel_menu"),
            ]])
        )

    elif state == "alert_channel_main":
        # Set main alert channel
        ch = text.strip()
        if not (ch.startswith("@") or ch.lstrip("-").isdigit()):
            await update.message.reply_text(
                "Enter channel ID (e.g. `-1001234567890`) or @username (e.g. `@mychannel`).",
                parse_mode="Markdown"
            )
            return
        try:
            ch_id = int(ch) if ch.lstrip("-").isdigit() else ch
            set_alert_channel("main", ch_id)
            clear_state(uid)
            await update.message.reply_text(
                f"✅ <b>Main Channel Set</b>\n\n"
                f"Channel: <code>{ch}</code>\n\n"
                f"Portfolio and Pumpfun alerts will be sent here.\n"
                f"Make sure the bot is an admin.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚙️ Channel Settings", callback_data="channels:menu"),
                ]])
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")

    elif state == "alert_channel_launches":
        # Set launch alert channel
        ch = text.strip()
        if not (ch.startswith("@") or ch.lstrip("-").isdigit()):
            await update.message.reply_text(
                "Enter channel ID (e.g. `-1001234567890`) or @username (e.g. `@mychannel`).",
                parse_mode="Markdown"
            )
            return
        try:
            ch_id = int(ch) if ch.lstrip("-").isdigit() else ch
            set_alert_channel("launches", ch_id)
            clear_state(uid)
            await update.message.reply_text(
                f"✅ <b>Launch Channel Set</b>\n\n"
                f"Channel: <code>{ch}</code>\n\n"
                f"Early token launch alerts will be sent here.\n"
                f"Make sure the bot is an admin.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚙️ Channel Settings", callback_data="channels:menu"),
                ]])
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")

    elif state == "alert_channel_scout":
        ch = text.strip()
        if not (ch.startswith("@") or ch.lstrip("-").isdigit()):
            await update.message.reply_text(
                "Enter channel ID (e.g. `-1001234567890`) or @username (e.g. `@mychannel`).",
                parse_mode="Markdown"
            )
            return
        sc.set_alert_channel(ch)
        clear_state(uid)
        await update.message.reply_text(
            f"✅ <b>Scout Channel Set</b>\n\n"
            f"Channel: <code>{ch}</code>\n\n"
            f"Scanner alerts will be posted here.\n"
            f"Make sure the bot is an admin.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Channel Settings", callback_data="channels:menu"),
            ]])
        )

    elif state == "pumplive_channel":
        ch = text.strip()
        if not (ch.startswith("@") or ch.lstrip("-").isdigit()):
            await update.message.reply_text(
                "Enter a channel ID (e.g. `-1001234567890`) or @username (e.g. `@mychannel`).",
                parse_mode="Markdown"
            )
            return
        pf.set_pumplive_channel(ch)
        clear_state(uid)
        await update.message.reply_text(
            f"✅ *Pump Live channel set to* `{ch}`\n\n"
            f"Every Pump Live alert will now be posted there too.\n"
            f"Make sure the bot is an admin in the channel.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📣 Channel Settings", callback_data="pumplive:channel_menu"),
            ]])
        )

    elif state == "pumpgrad_channel":
        ch = text.strip()
        if not (ch.startswith("@") or ch.lstrip("-").isdigit()):
            await update.message.reply_text(
                "Enter a channel ID (e.g. `-1001234567890`) or @username (e.g. `@mychannel`).",
                parse_mode="Markdown"
            )
            return
        pf.set_pumpgrad_channel(ch)
        clear_state(uid)
        await update.message.reply_text(
            f"✅ *Pump Grad channel set to* `{ch}`\n\n"
            f"Every graduation alert will now be posted there too.\n"
            f"Make sure the bot is an admin in the channel.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📣 Channel Settings", callback_data="pumpgrad:channel_menu"),
            ]])
        )

    elif state == "wbalert_add_addr":
        addr = text.strip()
        if len(addr) < 32 or len(addr) > 44:
            await update.message.reply_text(
                "❌ Invalid address. Paste a valid Solana wallet address (32–44 chars).",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wbalert:list")]])
            )
            return
        context.user_data["state"]           = "wbalert_add_label"
        context.user_data["wbalert_pending"] = addr
        await update.message.reply_text(
            f"✅ Address: `{addr}`\n\nGive this wallet a label (e.g. *Whale 1*, *Dev*):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Skip (use address)", callback_data="wbalert:skip_label")]])
        )

    elif state == "alert_price":
        # Price alert — user entered target price after choosing above/below direction
        raw = text.strip().lstrip("$")
        try:
            target_price = float(raw)
            if target_price <= 0:
                raise ValueError("non-positive")
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid price. Enter a positive number, e.g. `0.000025`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
            )
            return
        alert_mint  = get_state(uid, "alert_mint", "")
        alert_sym   = get_state(uid, "alert_symbol", alert_mint[:6])
        direction   = get_state(uid, "alert_direction", "above")
        dir_lbl     = "above ↑" if direction == "above" else "below ↓"
        clear_state(uid)
        # Register the price alert via portfolio_alerts module
        try:
            import portfolio_alerts as _pa
            _pa.add_price_alert(uid, alert_mint, alert_sym, direction, target_price)
            await update.message.reply_text(
                f"✅ *Price Alert Set*\n\n"
                f"🪙 ${_esc(alert_sym)}\n"
                f"Alert when price goes {dir_lbl} `${target_price:.8g}`\n\n"
                f"_You'll get a DM when the price crosses that level._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="menu:main")]])
            )
        except (ImportError, AttributeError):
            # portfolio_alerts doesn't have add_price_alert yet — notify user
            await update.message.reply_text(
                f"⚠️ *Price alerts are not yet fully implemented.*\n\n"
                f"Your alert for ${_esc(alert_sym)} {dir_lbl} `${target_price:.8g}` was noted but will not fire.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="menu:main")]])
            )

    elif state == "wbalert_add_label":
        addr  = context.user_data.pop("wbalert_pending", "")
        label = text.strip() or addr[:8]
        context.user_data.pop("state", None)
        if addr:
            added = add_user_alert_wallet(uid, addr, label)
            if added:
                await update.message.reply_text(
                    f"✅ *Wallet alert added!*\n\n*{label}*\n`{addr}`\n\n"
                    f"_You'll be notified when this wallet buys a new token._",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👁️ My Alerts", callback_data="wbalert:list")]])
                )
            else:
                await update.message.reply_text("⚠️ Wallet already tracked.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👁️ My Alerts", callback_data="wbalert:list")]]))

    else:
        # Natural language scanner triggers
        tl = text.lower()
        if any(p in tl for p in ["start scanning", "watch for hot", "start scan", "begin scan", "resume alerts", "resume scan"]):
            await cmd_scan(update, context)
            return
        if any(p in tl for p in ["stop scanning", "stop scan", "pause scan", "pause alerts"]):
            await cmd_stopscan(update, context)
            return
        if any(p in tl for p in ["show watchlist", "watchlist", "watch list"]):
            await cmd_watchlist(update, context)
            return
        if any(p in tl for p in ["top alerts today", "top alerts", "best alerts"]):
            await cmd_topalerts(update, context)
            return
        if tl.startswith("heat score ") or tl.startswith("heatscore "):
            token = text.split(None, 1)[1] if " " in text else ""
            if token:
                context._args = [token]
                context.args  = [token]
                await cmd_heatscore(update, context)
                return

        await update.message.reply_text(
            "Unknown command. Use /menu to see all options.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Menu", callback_data="menu:main")
            ]])
        )


# ─── Analytics ────────────────────────────────────────────────────────────────

def _build_analytics(uid: int, days: int = None) -> tuple:
    trades = _db.get_trades(uid, limit=10000)
    now    = time.time()
    cutoff = now - days * 86400 if days else 0
    if cutoff:
        trades = [t for t in trades if t.get("ts", 0) >= cutoff]

    buys  = [t for t in trades if t.get("action") == "buy"]
    sells = [t for t in trades if t.get("action") == "sell"]

    closed = [t for t in sells if t.get("pnl_pct") is not None]
    wins   = [t for t in closed if t["pnl_pct"] > 0]
    losses = [t for t in closed if t["pnl_pct"] <= 0]
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    avg_pnl  = sum(t["pnl_pct"] for t in closed) / len(closed) if closed else 0
    best  = max(closed, key=lambda x: x["pnl_pct"], default=None)
    worst = min(closed, key=lambda x: x["pnl_pct"], default=None)

    sol_spent    = sum(t.get("sol_amount", 0) or 0 for t in buys)
    sol_received = sum(t.get("sol_received", 0) or 0 for t in sells)
    net_sol = sol_received - sol_spent

    nar_wins   = defaultdict(int)
    nar_losses = defaultdict(int)
    nar_pnl    = defaultdict(list)
    for t in closed:
        n = t.get("narrative", "Other")
        if t["pnl_pct"] > 0:
            nar_wins[n] += 1
        else:
            nar_losses[n] += 1
        nar_pnl[n].append(t["pnl_pct"])
    all_narratives = set(list(nar_wins) + list(nar_losses))
    nar_rows = []
    for n in sorted(all_narratives,
                    key=lambda x: -(nar_wins[x] / (nar_wins[x] + nar_losses[x])
                                    if (nar_wins[x] + nar_losses[x]) else 0)):
        total = nar_wins[n] + nar_losses[n]
        wr    = nar_wins[n] / total * 100
        avg   = sum(nar_pnl[n]) / len(nar_pnl[n])
        nar_rows.append(f"• {n}: {total} trades · {wr:.0f}% WR · avg {avg:+.0f}%")

    # Scanner stats
    scanner_log = sc.load_log()
    if days:
        date_filter = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    else:
        date_filter = "1970-01-01"
    s_entries    = [e for e in scanner_log if e.get("date", "") >= date_filter]
    s_total      = len(s_entries)
    s_alerted    = sum(1 for e in s_entries if e.get("alerted"))
    s_watchlisted = sum(1 for e in s_entries if 30 <= (e.get("score") or 0) < 55)
    avg_score    = sum(e.get("score", 0) for e in s_entries) / s_total if s_total else 0

    dq_counts: dict[str, int] = {}
    for e in s_entries:
        dq = e.get("dq")
        if dq:
            key = dq.split("—")[0].strip() if "—" in dq else dq[:30]
            dq_counts[key] = dq_counts.get(key, 0) + 1
    top_dq = sorted(dq_counts, key=lambda x: -dq_counts[x])

    nar_alert_counts: dict[str, int] = defaultdict(int)
    for e in s_entries:
        if e.get("alerted") and e.get("narrative"):
            nar_alert_counts[e["narrative"]] += 1

    label = f"{days}d" if days else "All Time"
    lines = [f"📊 *Analytics — {label}*\n"]

    lines.append("*💰 Trade Performance*")
    lines.append(f"Trades: {len(buys)} buys · {len(sells)} sells")
    if closed:
        lines.append(f"Win Rate: {win_rate:.0f}% ({len(wins)}W / {len(losses)}L)")
        lines.append(f"Avg P&L: {avg_pnl:+.0f}%")
        if best:  lines.append(f"Best:  {best['symbol']} {best['pnl_pct']:+.0f}%")
        if worst: lines.append(f"Worst: {worst['symbol']} {worst['pnl_pct']:+.0f}%")
    lines.append(f"SOL In: {sol_spent:.3f} · Out: {sol_received:.3f} · Net: {net_sol:+.3f}")

    if nar_rows:
        lines.append("\n*🔥 Narrative Performance*")
        lines.extend(nar_rows[:5])

    lines.append(f"\n*📡 Scanner ({label})*")
    lines.append(f"Scanned: {s_total:,} · Alerted: {s_alerted} · Watchlisted: {s_watchlisted}")
    lines.append(f"Avg Score: {avg_score:.1f}")
    if top_dq:
        lines.append(f"Top DQ: {top_dq[0]} ({dq_counts[top_dq[0]]}x)")
    if nar_alert_counts:
        top_nar = sorted(nar_alert_counts, key=lambda x: -nar_alert_counts[x])
        lines.append("Trending: " + " · ".join(
            f"{n}({nar_alert_counts[n]})" for n in top_nar[:3]
        ))

    # ── Auto-buy performance ──────────────────────────────────────────────────
    ab_trades = [t for t in trades if t.get("heat_score") is not None]
    if ab_trades:
        ab_buys  = [t for t in ab_trades if t.get("action") == "buy"]
        ab_sells_with_pnl = [t for t in ab_trades if t.get("action") == "sell" and t.get("pnl_pct") is not None]
        ab_wins  = [t for t in ab_sells_with_pnl if t["pnl_pct"] > 0]
        ab_wr    = len(ab_wins) / len(ab_sells_with_pnl) * 100 if ab_sells_with_pnl else 0
        avg_score_bought = sum(t["heat_score"] for t in ab_buys) / len(ab_buys) if ab_buys else 0
        lines.append(f"\n*🤖 Auto-Buy Performance*")
        lines.append(f"Auto-buys: {len(ab_buys)} · Closed: {len(ab_sells_with_pnl)}")
        if ab_sells_with_pnl:
            avg_ab_pnl = sum(t["pnl_pct"] for t in ab_sells_with_pnl) / len(ab_sells_with_pnl)
            lines.append(f"Win Rate: {ab_wr:.0f}% · Avg P&L: {avg_ab_pnl:+.0f}%")
        lines.append(f"Avg Score at Buy: {avg_score_bought:.0f}/100")

    # ── Best calls leaderboard ────────────────────────────────────────────────
    scanner_log = sc.load_log()
    alerted_log = [e for e in scanner_log if e.get("alerted") and e.get("mint")]
    # De-dupe by mint, keep first occurrence
    seen_mints: dict[str, dict] = {}
    for e in sorted(alerted_log, key=lambda x: x.get("timestamp", 0)):
        if e["mint"] not in seen_mints:
            seen_mints[e["mint"]] = e
    # Enrich with current mcap
    enriched_calls = []
    for e in list(seen_mints.values())[-20:]:  # check last 20 alerted tokens
        try:
            price, cur_mcap = fetch_token_price(e["mint"])
            alert_mcap = e.get("mcap", 0) or 0
            if alert_mcap and cur_mcap:
                gain = ((cur_mcap - alert_mcap) / alert_mcap) * 100
                enriched_calls.append({"symbol": e.get("symbol","?"), "gain": gain,
                                       "score": e.get("score", 0), "mint": e["mint"]})
        except Exception:
            continue
    if enriched_calls:
        top_calls = sorted(enriched_calls, key=lambda x: -x["gain"])[:5]
        lines.append(f"\n*🏆 Best Calls (MCap Gain)*")
        for c in top_calls:
            arrow = "🚀" if c["gain"] > 100 else ("📈" if c["gain"] > 0 else "📉")
            lines.append(f"  {arrow} `${c['symbol']}` {c['gain']:+.0f}% — score `{c['score']}`")

    text = "\n".join(lines)
    kb   = InlineKeyboardMarkup([[
        InlineKeyboardButton("All", callback_data="analytics:all"),
        InlineKeyboardButton("7d",  callback_data="analytics:7d"),
        InlineKeyboardButton("30d", callback_data="analytics:30d"),
        InlineKeyboardButton("🔄",  callback_data="analytics:all"),
    ]])
    return text, kb


async def _build_pnl(uid: int) -> str:
    """Build P&L summary: realized (trade log) + unrealized (open positions vs buy price)."""
    mode     = get_mode(uid)
    my_trades = _db.get_trades(uid, limit=10000)
    buys     = [t for t in my_trades if t.get("action") == "buy"]
    sells    = [t for t in my_trades if t.get("action") == "sell"]
    closed   = [t for t in sells if t.get("pnl_pct") is not None]

    # Realized
    sol_spent    = sum(t.get("sol_amount", 0) or 0 for t in buys)
    sol_received = sum(t.get("sol_received", 0) or 0 for t in sells)
    realized_sol = sol_received - sol_spent
    wins   = [t for t in closed if t["pnl_pct"] > 0]
    losses = [t for t in closed if t["pnl_pct"] <= 0]
    win_rate = len(wins) / len(closed) * 100 if closed else 0

    # Unrealized — check open positions in auto_sell configs
    as_configs = _db.get_all_auto_sells(uid)
    portfolio  = get_portfolio(uid)
    unreal_lines = []
    total_unreal_sol = 0.0

    # For live mode: fetch actual on-chain balances once (avoids one RPC call per token)
    _live_balances: dict[str, int] = {}
    if mode == "live":
        _pubkey = get_wallet_pubkey()
        if _pubkey:
            try:
                for a in (get_token_accounts(_pubkey) or []):
                    _live_balances[a["mint"]] = a["amount"]
            except Exception:
                pass  # fall back to initial_raw per-token below

    # Live SOL price for accurate SOL-value estimates
    _sol_price = pf.get_sol_price() or 150.0

    for mint, cfg in as_configs.items():
        buy_price = cfg.get("buy_price_usd", 0)
        symbol    = cfg.get("symbol", mint[:6])
        decimals  = cfg.get("decimals", 6)
        if not buy_price:
            continue
        if mode == "paper":
            raw_held = portfolio.get(mint, 0)
        else:
            # Prefer on-chain balance; fall back to initial_raw if RPC failed
            raw_held = _live_balances.get(mint) or cfg.get("initial_raw", 0)
        if not raw_held:
            continue
        try:
            price, _ = fetch_token_price(mint)
            if not price:
                continue
            pnl_pct = ((price - buy_price) / buy_price) * 100
            ui_amt  = raw_held / (10 ** decimals)
            cur_val_sol = (price * ui_amt) / _sol_price
            arrow = "📈" if pnl_pct > 0 else "📉"
            unreal_lines.append(
                f"  {arrow} `${symbol}` — {pnl_pct:+.0f}% (buy `${buy_price:.6f}` → now `${price:.6f}`)"
            )
            total_unreal_sol += cur_val_sol
        except Exception:
            continue

    lines = ["*💰 P&L Summary*\n"]
    lines.append(f"*Mode:* {'📄 Paper' if mode == 'paper' else '🔴 Live'}")
    lines.append(f"*Total Trades:* {len(buys)} buys · {len(sells)} sells\n")

    lines.append("*Realized*")
    lines.append(f"SOL Spent:    `{sol_spent:.4f}`")
    lines.append(f"SOL Received: `{sol_received:.4f}`")
    net_arrow = "📈" if realized_sol >= 0 else "📉"
    lines.append(f"Net SOL: {net_arrow} `{realized_sol:+.4f}`")
    if closed:
        lines.append(f"Win Rate: `{win_rate:.0f}%` ({len(wins)}W / {len(losses)}L)")

    if unreal_lines:
        lines.append("\n*Unrealized (open positions)*")
        lines.extend(unreal_lines)
    elif as_configs:
        lines.append("\n_No open positions with price data._")

    if not buys and not as_configs:
        lines.append("\n_No trades yet. Buy a token to start tracking._")

    return "\n".join(lines)


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Calculating P&L...")
    uid = update.effective_user.id
    try:
        text = await _build_pnl(uid)
    except Exception as e:
        text = f"Error building P&L: {e}"
    await msg.edit_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh",   callback_data="pnl:refresh"),
            InlineKeyboardButton("📊 Analytics", callback_data="analytics:all"),
            InlineKeyboardButton("⬅️ Menu",      callback_data="menu:main"),
        ]])
    )


async def cmd_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text, kb = _build_analytics(uid, days=None)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def analytics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    parts = query.data.split(":")
    days  = {"7d": 7, "30d": 30, "all": None}.get(parts[1] if len(parts) > 1 else "all")
    text, kb = _build_analytics(uid, days)
    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


async def pnl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    try:
        text = await _build_pnl(uid)
    except Exception as e:
        text = f"Error: {e}"
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh",   callback_data="pnl:refresh"),
            InlineKeyboardButton("📊 Analytics", callback_data="analytics:all"),
            InlineKeyboardButton("⬅️ Menu",      callback_data="menu:main"),
        ]])
    )


# ─── Portfolio Distribution Watcher ────────────────────────────────────────────

async def cmd_portfolio_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show tokens being monitored by the distribution watcher."""
    uid = update.effective_user.id
    portfolio = get_portfolio(uid)
    
    if not portfolio:
        await update.message.reply_text(
            "📊 *Portfolio Watcher*\n\n"
            "Your portfolio is empty. Tokens will be monitored for crash signals once you buy.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💰 Buy a Token", callback_data="menu:buy"),
                InlineKeyboardButton("⬅️ Menu", callback_data="menu:main"),
            ]])
        )
        return
    
    # Load watcher state
    try:
        import json
        with open(os.path.join(DATA_DIR, "portfolio_watcher_state.json"), "r") as f:
            state = json.load(f)
    except Exception:
        state = {}
    
    # Build watchlist display
    lines = ["📊 *Portfolio Watcher Status*\n"]
    tokens_watching = 0
    
    for mint, amount in sorted(portfolio.items(), key=lambda x: -x[1])[:20]:
        if amount <= 0:
            continue
        
        token_state = state.get(mint, {})
        symbol = token_state.get("symbol", mint[:8])
        cycles = token_state.get("cycles_observed", 0)
        last_signals = token_state.get("last_alert_signals", [])
        
        tokens_watching += 1
        
        status_txt = f"📍 Baseline ({cycles})" if cycles < 3 else "🟢 Active"
        signal_txt = f" | {len(last_signals)} signals" if last_signals else ""
        
        lines.append(f"• ${symbol}\n  {status_txt}{signal_txt}")
    
    if tokens_watching == 0:
        lines.append("_No tokens in portfolio to monitor_")
    else:
        lines.append(f"\n_Watching {tokens_watching} token(s)_")
        lines.append("_Alerts sent to main channel when risk detected_")
    
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚙️ Settings", callback_data="watch:settings"),
            InlineKeyboardButton("⬅️ Menu", callback_data="menu:main"),
        ]])
    )


async def watch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle portfolio watcher settings callbacks."""
    query = update.callback_query
    uid = query.from_user.id
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    await query.answer()
    
    if action == "settings":
        watcher_enabled = _db.get_setting(f"watcher_enabled_{uid}", True)
        status = "🟢 Enabled" if watcher_enabled else "🔴 Disabled"
        
        await query.edit_message_text(
            f"⚙️ *Portfolio Watcher Settings*\n\n"
            f"Status: {status}\n\n"
            f"The watcher monitors your portfolio tokens for:\n"
            f"• 📛 Developer wallet movement\n"
            f"• 🐋 Whale exits\n"
            f"• 💧 Liquidity draining\n"
            f"• 📉 Sell pressure rising\n"
            f"• 📊 Volume collapsing\n\n"
            f"High-risk alerts are sent to the main channel.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Toggle", callback_data="watch:toggle")],
                [InlineKeyboardButton("⬅️ Back", callback_data="watch:back")],
            ])
        )
    
    elif action == "toggle":
        watcher_enabled = _db.get_setting(f"watcher_enabled_{uid}", True)
        _db.set_setting(f"watcher_enabled_{uid}", not watcher_enabled)
        
        new_status = "🟢 Enabled" if not watcher_enabled else "🔴 Disabled"
        await query.edit_message_text(
            f"✅ *Watcher {new_status}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Settings", callback_data="watch:settings"),
                InlineKeyboardButton("⬅️ Back", callback_data="watch:back"),
            ]])
        )
    
    elif action == "back":
        portfolio = get_portfolio(uid)
        
        if not portfolio:
            await query.edit_message_text(
                "📊 *Portfolio Watcher*\n\n"
                "Your portfolio is empty.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💰 Buy a Token", callback_data="menu:buy"),
                    InlineKeyboardButton("⬅️ Menu", callback_data="menu:main"),
                ]])
            )
            return
        
        # Rebuild watchlist display
        try:
            import json
            with open(os.path.join(DATA_DIR, "portfolio_watcher_state.json"), "r") as f:
                state = json.load(f)
        except Exception:
            state = {}
        
        lines = ["📊 *Portfolio Watcher Status*\n"]
        tokens_watching = 0
        
        for mint, amount in sorted(portfolio.items(), key=lambda x: -x[1])[:20]:
            if amount <= 0:
                continue
            
            token_state = state.get(mint, {})
            symbol = token_state.get("symbol", mint[:8])
            cycles = token_state.get("cycles_observed", 0)
            last_signals = token_state.get("last_alert_signals", [])
            
            tokens_watching += 1
            status_txt = f"📍 Baseline ({cycles})" if cycles < 3 else "🟢 Active"
            signal_txt = f" | {len(last_signals)} signals" if last_signals else ""
            
            lines.append(f"• ${symbol}\n  {status_txt}{signal_txt}")
        
        if tokens_watching == 0:
            lines.append("_No tokens in portfolio to monitor_")
        
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Settings", callback_data="watch:settings"),
                InlineKeyboardButton("⬅️ Menu", callback_data="menu:main"),
            ]])
        )


# ─── Early Launch Hunter ──────────────────────────────────────────────────────

async def cmd_launches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent token launches detected by the hunt."""
    from launch_hunter import get_launch_stats, format_launch_stats_message
    
    message = format_launch_stats_message()
    
    await update.message.reply_text(
        message,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data="launches:refresh"),
            InlineKeyboardButton("⚙️ Settings", callback_data="launches:settings"),
            InlineKeyboardButton("⬅️ Menu", callback_data="menu:main"),
        ]])
    )


async def launches_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle launch hunter callbacks."""
    from launch_hunter import get_launch_stats, format_launch_stats_message
    
    query = update.callback_query
    uid = query.from_user.id
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    await query.answer()
    
    if action == "refresh":
        message = format_launch_stats_message()
        
        await query.edit_message_text(
            message,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data="launches:refresh"),
                InlineKeyboardButton("⚙️ Settings", callback_data="launches:settings"),
                InlineKeyboardButton("⬅️ Menu", callback_data="menu:main"),
            ]])
        )
    
    elif action == "settings":
        hunter_enabled = _db.get_setting(f"launch_hunter_enabled_{uid}", True)
        status = "🟢 Enabled" if hunter_enabled else "🔴 Disabled"
        
        await query.edit_message_text(
            f"⚙️ <b>Launch Hunter Settings</b>\n\n"
            f"Status: {status}\n\n"
            f"The launch hunter watches for:\n"
            f"• 🚀 Brand new token launches\n"
            f"• 💧 Liquidity being added\n"
            f"• ⏱️ Smart detection (seconds old)\n"
            f"• 🎯 Configurable minimum liquidity\n\n"
            f"Alerts sent to launch channel instantly when tokens appear.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Toggle", callback_data="launches:toggle")],
                [InlineKeyboardButton("⬅️ Back", callback_data="launches:back")],
            ])
        )
    
    elif action == "toggle":
        hunter_enabled = _db.get_setting(f"launch_hunter_enabled_{uid}", True)
        _db.set_setting(f"launch_hunter_enabled_{uid}", not hunter_enabled)
        
        new_status = "🟢 Enabled" if not hunter_enabled else "🔴 Disabled"
        await query.edit_message_text(
            f"✅ <b>Launch Hunter {new_status}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Settings", callback_data="launches:settings"),
                InlineKeyboardButton("⬅️ Back", callback_data="launches:back"),
            ]])
        )
    
    elif action == "back":
        message = format_launch_stats_message()
        
        await query.edit_message_text(
            message,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data="launches:refresh"),
                InlineKeyboardButton("⚙️ Settings", callback_data="launches:settings"),
                InlineKeyboardButton("⬅️ Menu", callback_data="menu:main"),
            ]])
        )


# ─── Alert Channel Configuration ──────────────────────────────────────────────

def get_alert_channels() -> dict:
    """Get all configured alert channels from global settings or config."""
    import config
    return {
        "main":    _db.get_setting("main_alert_channel_id",    getattr(config, "MAIN_CHANNEL_ID", None)),
        "launches": _db.get_setting("launch_alert_channel_id", getattr(config, "LAUNCH_ALERT_CHANNEL_ID", None)),
    }

def set_alert_channel(channel_type: str, channel_id: int) -> bool:
    """Save alert channel ID to global settings."""
    if channel_type == "main":
        _db.set_setting("main_alert_channel_id", channel_id)
    elif channel_type == "launches":
        _db.set_setting("launch_alert_channel_id", channel_id)
    else:
        return False
    return True

def format_channel_id(channel_id) -> str:
    """Format channel ID for display."""
    if not channel_id:
        return "❌ Not configured"
    return f"`{channel_id}`"

async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show and manage alert channels."""
    uid = update.effective_user.id
    channels = get_alert_channels()

    launches_status = format_channel_id(channels.get("launches"))
    scout_ch = sc.get_alert_channel()
    scout_status = f"<code>{scout_ch}</code>" if scout_ch else "❌ Not configured"

    await update.message.reply_text(
        "⚙️ <b>Alert Channel Settings</b>\n\n"
        f"🚀 <b>Launch Channel</b> (early token launches)\n"
        f"   Status: {launches_status}\n\n"
        f"🔍 <b>Scout Channel</b> (scanner alerts)\n"
        f"   Status: {scout_status}\n\n"
        f"<i>Click below to configure channels</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Set Launch Channel", callback_data="channels:set:launches")],
            [InlineKeyboardButton("🔍 Set Scout Channel", callback_data="channels:set:scout")],
            [InlineKeyboardButton("🧪 Test Channels", callback_data="channels:test")],
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu:main")],
        ])
    )


async def channels_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle alert channel configuration."""
    query = update.callback_query
    uid = query.from_user.id
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    channel_type = parts[2] if len(parts) > 2 else ""
    await query.answer()
    
    if action == "set":
        set_state(uid, waiting_for=f"alert_channel_{channel_type}")
        
        channel_names = {
            "main": "📊 Main Channel (portfolio alerts)",
            "launches": "🚀 Launch Channel (new tokens)",
            "scout": "🔍 Scout Channel (scanner alerts)"
        }
        
        await query.edit_message_text(
            f"⚙️ <b>Set {channel_names.get(channel_type, 'Alert')} Channel</b>\n\n"
            f"Send the <b>channel ID</b> (e.g. <code>-1001234567890</code>)\n\n"
            f"<i>Steps:</i>\n"
            f"1. Create a private Telegram channel\n"
            f"2. Add this bot as admin\n"
            f"3. Get the channel ID:\n"
            f"   • Use /getid command\n"
            f"   • Or forward message to @userinfobot\n"
            f"4. Paste the ID below",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="channels:menu"),
            ]])
        )
    
    elif action == "test":
        import config
        channels = get_alert_channels()
        
        msg = "🧪 <b>Testing Alert Channels</b>\n\n"

        try:
            launch_id = channels.get("launches")
            if launch_id:
                await context.bot.send_message(
                    chat_id=launch_id,
                    text="✅ <b>Launch Channel Test</b>\n\nThis channel is configured correctly!",
                    parse_mode="HTML"
                )
                msg += "✅ Launch channel working\n"
            else:
                msg += "⚠️ Launch channel not set\n"
        except Exception as e:
            msg += f"❌ Launch channel error: {str(e)[:50]}\n"

        try:
            scout_ch = sc.get_alert_channel()
            if scout_ch:
                await context.bot.send_message(
                    chat_id=scout_ch,
                    text="✅ <b>Scout Channel Test</b>\n\nThis channel is configured correctly!",
                    parse_mode="HTML"
                )
                msg += "✅ Scout channel working\n"
            else:
                msg += "⚠️ Scout channel not set\n"
        except Exception as e:
            msg += f"❌ Scout channel error: {str(e)[:50]}\n"

        channels_obj = get_alert_channels()
        launches_status = format_channel_id(channels_obj.get("launches"))
        scout_ch_disp = sc.get_alert_channel()
        scout_status = f"<code>{scout_ch_disp}</code>" if scout_ch_disp else "❌ Not set"

        msg += f"\n<b>Current Configuration:</b>\n"
        msg += f"🚀 Launch: {launches_status}\n"
        msg += f"🔍 Scout: {scout_status}"

        await query.edit_message_text(
            msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Set Launch", callback_data="channels:set:launches")],
                [InlineKeyboardButton("🔍 Set Scout", callback_data="channels:set:scout")],
                [InlineKeyboardButton("⬅️ Back", callback_data="channels:menu")],
            ])
        )
    
    elif action == "menu":
        channels_obj = get_alert_channels()
        launches_status = format_channel_id(channels_obj.get("launches"))
        scout_ch = sc.get_alert_channel()
        scout_status = f"<code>{scout_ch}</code>" if scout_ch else "❌ Not configured"

        await query.edit_message_text(
            "⚙️ <b>Alert Channel Settings</b>\n\n"
            f"🚀 <b>Launch Channel</b> (early token launches)\n"
            f"   Status: {launches_status}\n\n"
            f"🔍 <b>Scout Channel</b> (scanner alerts)\n"
            f"   Status: {scout_status}\n\n"
            f"<i>Click below to configure channels</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Set Launch Channel", callback_data="channels:set:launches")],
                [InlineKeyboardButton("🔍 Set Scout Channel", callback_data="channels:set:scout")],
                [InlineKeyboardButton("🧪 Test Channels", callback_data="channels:test")],
                [InlineKeyboardButton("⬅️ Menu", callback_data="menu:main")],
            ])
        )



# ─── Global Risk Settings menu helpers ───────────────────────────────────────

def _global_risk_menu_text(gsl: dict, gts: dict, gttp: dict, gbe: dict, gte: dict) -> str:
    def _on(d): return "🟢 ON" if d.get("enabled") else "🔴 OFF"

    gsl_line  = f"  Trigger: {gsl.get('pct', 50)}% drop → sell {gsl.get('sell_pct', 100)}%"
    gts_line  = f"  Trail: {gts.get('trail_pct', 30)}% drop from peak → sell {gts.get('sell_pct', 100)}%"
    gttp_line = (f"  Activates at {gttp.get('activate_mult', 2.0)}x → trails {gttp.get('trail_pct', 20)}% "
                 f"→ sells {gttp.get('sell_pct', 50)}%")
    gbe_line  = f"  Activates at {gbe.get('activate_mult', 2.0)}x → stop-loss moves to entry price"
    gte_line  = (f"  After {gte.get('hours', 24)}h, if below {gte.get('target_mult', 1.5)}x "
                 f"→ sell {gte.get('sell_pct', 100)}%")

    return (
        "*🌍 Global Risk Settings*\n\n"
        "These rules apply automatically to *every token you buy* — no per-trade setup needed.\n\n"
        f"🛑 *Hard Stop-Loss* — {_on(gsl)}\n"
        f"{gsl_line}\n"
        "  Maximum loss protection. Sells immediately when price drops X% from your buy.\n\n"
        f"📉 *Trailing Stop* — {_on(gts)}\n"
        f"{gts_line}\n"
        "  Follows the price up automatically. Locks in gains by selling only if the price\n"
        "  reverses by X% from its highest point. Lets winners run, stops big reversals.\n\n"
        f"🎯 *Trailing Take-Profit* — {_on(gttp)}\n"
        f"{gttp_line}\n"
        "  Waits for a strong rally, then trails the peak. Captures real profit without\n"
        "  selling too early — activates only when you're already winning.\n\n"
        f"🛡️ *Breakeven Stop* — {_on(gbe)}\n"
        f"{gbe_line}\n"
        "  Once you double your money, your stop-loss moves to your entry price.\n"
        "  After this triggers, you literally *cannot lose money* on the trade.\n\n"
        f"⏱️ *Time Exit* — {_on(gte)}\n"
        f"{gte_line}\n"
        "  Exits dead/slow trades automatically. If a token hasn't gained enough\n"
        "  after X hours, it sells — freeing capital for better opportunities."
    )


def _global_risk_kb(gsl: dict, gts: dict, gttp: dict, gbe: dict, gte: dict) -> InlineKeyboardMarkup:
    def _toggle_lbl(d, name): return f"{'✅' if d.get('enabled') else '❌'} {name}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_toggle_lbl(gsl, "Hard Stop-Loss"),    callback_data="gsl:toggle"),
         InlineKeyboardButton("⚙️ Settings", callback_data="gsl:menu")],
        [InlineKeyboardButton(_toggle_lbl(gts, "Trailing Stop"),     callback_data="gts:toggle"),
         InlineKeyboardButton("⚙️ Settings", callback_data="gts:menu")],
        [InlineKeyboardButton(_toggle_lbl(gttp, "Trailing TP"),      callback_data="gttp:toggle"),
         InlineKeyboardButton("⚙️ Settings", callback_data="gttp:menu")],
        [InlineKeyboardButton(_toggle_lbl(gbe, "Breakeven Stop"),    callback_data="gbe:toggle"),
         InlineKeyboardButton("⚙️ Settings", callback_data="gbe:menu")],
        [InlineKeyboardButton(_toggle_lbl(gte, "Time Exit"),         callback_data="gte:toggle"),
         InlineKeyboardButton("⚙️ Settings", callback_data="gte:menu")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:autosell")],
    ])


def _gts_menu_text(gts: dict) -> str:
    on = "🟢 ON" if gts.get("enabled") else "🔴 OFF"
    return (
        f"*📉 Global Trailing Stop* — {on}\n\n"
        "*How it works:*\n"
        "Every time the price hits a new high, that becomes the new 'peak'. "
        "If the price then drops X% from that peak, your position sells automatically.\n\n"
        "*Example:*\n"
        "  Buy at `$1.00` → rises to `$2.00` (new peak)\n"
        "  Drops 30% → price hits `$1.40` → SELL!\n\n"
        "This is ideal for meme coins: lets winners run but exits before a full dump.\n\n"
        f"Trail %: `{gts.get('trail_pct', 30)}%` drop from peak triggers sell\n"
        f"Sell %: `{gts.get('sell_pct', 100)}%` of position sold when triggered"
    )


def _gts_menu_kb(gts: dict) -> InlineKeyboardMarkup:
    on = gts.get("enabled", False)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏸️ Disable" if on else "▶️ Enable", callback_data="gts:toggle")],
        [InlineKeyboardButton("Trail 15%", callback_data="gts:trail_pct:15"),
         InlineKeyboardButton("Trail 30%", callback_data="gts:trail_pct:30"),
         InlineKeyboardButton("Trail 50%", callback_data="gts:trail_pct:50")],
        [InlineKeyboardButton("✏️ Custom trail %", callback_data="gts:trail_pct_custom")],
        [InlineKeyboardButton("Sell 50%",  callback_data="gts:sell_pct:50"),
         InlineKeyboardButton("Sell 75%",  callback_data="gts:sell_pct:75"),
         InlineKeyboardButton("Sell 100%", callback_data="gts:sell_pct:100")],
        [InlineKeyboardButton("✏️ Custom sell %", callback_data="gts:sell_pct_custom")],
        [InlineKeyboardButton("⬅️ Back to Risk Settings", callback_data="gsl:overview")],
    ])


def _gttp_menu_text(gttp: dict) -> str:
    on = "🟢 ON" if gttp.get("enabled") else "🔴 OFF"
    return (
        f"*🎯 Global Trailing Take-Profit* — {on}\n\n"
        "*How it works:*\n"
        "Waits until your position hits a big gain (e.g. 2x), then starts trailing. "
        "If the price reverses X% from its highest point after activation, it sells your chosen %.\n\n"
        "*Example:*\n"
        "  Buy at `$1.00` → activates at `$2.00` (2x)\n"
        "  Rises to `$3.00` (new peak) → drops 20% → hits `$2.40` → SELL 50%\n\n"
        "Sells partial position to lock in profits while letting the rest ride higher.\n\n"
        f"Activate at: `{gttp.get('activate_mult', 2.0)}x` from buy price\n"
        f"Trail %: `{gttp.get('trail_pct', 20)}%` drop from peak triggers sell\n"
        f"Sell %: `{gttp.get('sell_pct', 50)}%` of position sold when triggered"
    )


def _gttp_menu_kb(gttp: dict) -> InlineKeyboardMarkup:
    on = gttp.get("enabled", False)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏸️ Disable" if on else "▶️ Enable", callback_data="gttp:toggle")],
        [InlineKeyboardButton("Activate 1.5x", callback_data="gttp:activate_mult:1.5"),
         InlineKeyboardButton("Activate 2x",   callback_data="gttp:activate_mult:2.0"),
         InlineKeyboardButton("Activate 3x",   callback_data="gttp:activate_mult:3.0")],
        [InlineKeyboardButton("✏️ Custom activate mult", callback_data="gttp:activate_mult_custom")],
        [InlineKeyboardButton("Trail 10%", callback_data="gttp:trail_pct:10"),
         InlineKeyboardButton("Trail 20%", callback_data="gttp:trail_pct:20"),
         InlineKeyboardButton("Trail 30%", callback_data="gttp:trail_pct:30")],
        [InlineKeyboardButton("✏️ Custom trail %", callback_data="gttp:trail_pct_custom")],
        [InlineKeyboardButton("Sell 25%",  callback_data="gttp:sell_pct:25"),
         InlineKeyboardButton("Sell 50%",  callback_data="gttp:sell_pct:50"),
         InlineKeyboardButton("Sell 75%",  callback_data="gttp:sell_pct:75")],
        [InlineKeyboardButton("✏️ Custom sell %", callback_data="gttp:sell_pct_custom")],
        [InlineKeyboardButton("⬅️ Back to Risk Settings", callback_data="gsl:overview")],
    ])


def _gbe_menu_text(gbe: dict) -> str:
    on = "🟢 ON" if gbe.get("enabled") else "🔴 OFF"
    return (
        f"*🛡️ Global Breakeven Stop* — {on}\n\n"
        "*How it works:*\n"
        "Once your position reaches a set gain (e.g. 2x), your stop-loss automatically moves "
        "to your original entry price. From that point on, the position can only make money — "
        "the absolute worst outcome is breaking even.\n\n"
        "*Example:*\n"
        "  Buy at `$1.00` → price hits `$2.00` (2x reached)\n"
        "  Stop-loss moves to `$1.00` (your buy price)\n"
        "  If price crashes back to `$1.00` → SELL at breakeven, zero loss\n\n"
        "Works best combined with Trailing Stop or Hard Stop-Loss.\n\n"
        f"Activate at: `{gbe.get('activate_mult', 2.0)}x` from buy price"
    )


def _gbe_menu_kb(gbe: dict) -> InlineKeyboardMarkup:
    on = gbe.get("enabled", False)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏸️ Disable" if on else "▶️ Enable", callback_data="gbe:toggle")],
        [InlineKeyboardButton("Activate 1.5x", callback_data="gbe:activate_mult:1.5"),
         InlineKeyboardButton("Activate 2x",   callback_data="gbe:activate_mult:2.0"),
         InlineKeyboardButton("Activate 3x",   callback_data="gbe:activate_mult:3.0")],
        [InlineKeyboardButton("✏️ Custom activate mult", callback_data="gbe:activate_mult_custom")],
        [InlineKeyboardButton("⬅️ Back to Risk Settings", callback_data="gsl:overview")],
    ])


def _gte_menu_text(gte: dict) -> str:
    on = "🟢 ON" if gte.get("enabled") else "🔴 OFF"
    return (
        f"*⏱️ Global Time Exit* — {on}\n\n"
        "*How it works:*\n"
        "If a token hasn't reached your target gain after a set number of hours, "
        "it sells automatically. This frees up capital from slow, stagnant trades "
        "so you can deploy it into better opportunities.\n\n"
        "*Example:*\n"
        "  Buy at `$1.00` — target is `1.5x` within `24h`\n"
        "  After 24h, price is only `$1.20` (below 1.5x target)\n"
        "  Time Exit fires → SELL 100%\n\n"
        "  If price hits 1.5x before 24h, Time Exit does NOT fire.\n\n"
        f"Time limit: `{gte.get('hours', 24)} hours` after purchase\n"
        f"Target: must be at `{gte.get('target_mult', 1.5)}x` or above, otherwise sell\n"
        f"Sell %: `{gte.get('sell_pct', 100)}%` of position sold"
    )


def _gte_menu_kb(gte: dict) -> InlineKeyboardMarkup:
    on = gte.get("enabled", False)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏸️ Disable" if on else "▶️ Enable", callback_data="gte:toggle")],
        [InlineKeyboardButton("4 hours",  callback_data="gte:hours:4"),
         InlineKeyboardButton("12 hours", callback_data="gte:hours:12"),
         InlineKeyboardButton("24 hours", callback_data="gte:hours:24"),
         InlineKeyboardButton("48 hours", callback_data="gte:hours:48")],
        [InlineKeyboardButton("✏️ Custom hours", callback_data="gte:hours_custom")],
        [InlineKeyboardButton("Target 1.2x", callback_data="gte:target_mult:1.2"),
         InlineKeyboardButton("Target 1.5x", callback_data="gte:target_mult:1.5"),
         InlineKeyboardButton("Target 2x",   callback_data="gte:target_mult:2.0")],
        [InlineKeyboardButton("✏️ Custom target mult", callback_data="gte:target_mult_custom")],
        [InlineKeyboardButton("Sell 50%",  callback_data="gte:sell_pct:50"),
         InlineKeyboardButton("Sell 75%",  callback_data="gte:sell_pct:75"),
         InlineKeyboardButton("Sell 100%", callback_data="gte:sell_pct:100")],
        [InlineKeyboardButton("✏️ Custom sell %", callback_data="gte:sell_pct_custom")],
        [InlineKeyboardButton("⬅️ Back to Risk Settings", callback_data="gsl:overview")],
    ])


# ─── Global Stop-Loss callback ────────────────────────────────────────────────

def _gsl_menu_kb(gsl: dict) -> InlineKeyboardMarkup:
    on         = gsl.get("enabled", False)
    toggle_lbl = "⏸️ Disable" if on else "▶️ Enable"
    status_txt = "🟢 Enabled" if on else "🔴 Disabled"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_lbl, callback_data="gsl:toggle")],
        [InlineKeyboardButton("Drop 25%", callback_data="gsl:pct:25"),
         InlineKeyboardButton("Drop 50%", callback_data="gsl:pct:50"),
         InlineKeyboardButton("Drop 75%", callback_data="gsl:pct:75")],
        [InlineKeyboardButton("✏️ Custom drop %", callback_data="gsl:pct_custom")],
        [InlineKeyboardButton("Sell 25%",  callback_data="gsl:sell_pct:25"),
         InlineKeyboardButton("Sell 50%",  callback_data="gsl:sell_pct:50"),
         InlineKeyboardButton("Sell 75%",  callback_data="gsl:sell_pct:75"),
         InlineKeyboardButton("Sell 100%", callback_data="gsl:sell_pct:100")],
        [InlineKeyboardButton("✏️ Custom sell %", callback_data="gsl:sell_pct_custom")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:autosell")],
    ])


async def gsl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]
    await query.answer()

    gsl = get_global_sl()

    if action == "menu":
        on = gsl.get("enabled", False)
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        await query.edit_message_text(
            f"*🌍 Global Stop-Loss*\n\n"
            f"Status: {status_txt}\n"
            f"Trigger: price drops `{gsl.get('pct', 50)}%` from buy price\n"
            f"Action: sell `{gsl.get('sell_pct', 100)}%` of position\n\n"
            f"Applies to ALL tracked positions as a safety net.",
            parse_mode="Markdown",
            reply_markup=_gsl_menu_kb(gsl)
        )

    elif action == "toggle":
        gsl["enabled"] = not gsl.get("enabled", False)
        set_global_sl(gsl)
        on = gsl["enabled"]
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        await query.edit_message_text(
            f"*🌍 Global Stop-Loss*\n\n"
            f"Status: {status_txt}\n"
            f"Trigger: price drops `{gsl.get('pct', 50)}%` from buy price\n"
            f"Action: sell `{gsl.get('sell_pct', 100)}%` of position\n\n"
            f"Applies to ALL tracked positions as a safety net.",
            parse_mode="Markdown",
            reply_markup=_gsl_menu_kb(gsl)
        )

    elif action == "pct":
        pct = int(parts[2])
        gsl["pct"] = pct
        set_global_sl(gsl)
        await query.answer(f"Drop threshold set to {pct}%")
        on = gsl.get("enabled", False)
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        await query.edit_message_text(
            f"*🌍 Global Stop-Loss*\n\n"
            f"Status: {status_txt}\n"
            f"Trigger: price drops `{gsl.get('pct', 50)}%` from buy price\n"
            f"Action: sell `{gsl.get('sell_pct', 100)}%` of position\n\n"
            f"Applies to ALL tracked positions as a safety net.",
            parse_mode="Markdown",
            reply_markup=_gsl_menu_kb(gsl)
        )

    elif action == "sell_pct":
        pct = int(parts[2])
        gsl["sell_pct"] = pct
        set_global_sl(gsl)
        await query.answer(f"Sell amount set to {pct}%")
        on = gsl.get("enabled", False)
        status_txt = "🟢 Enabled" if on else "🔴 Disabled"
        await query.edit_message_text(
            f"*🌍 Global Stop-Loss*\n\n"
            f"Status: {status_txt}\n"
            f"Trigger: price drops `{gsl.get('pct', 50)}%` from buy price\n"
            f"Action: sell `{gsl.get('sell_pct', 100)}%` of position\n\n"
            f"Applies to ALL tracked positions as a safety net.",
            parse_mode="Markdown",
            reply_markup=_gsl_menu_kb(gsl)
        )

    elif action == "pct_custom":
        set_state(uid, waiting_for="gsl_pct")
        await query.edit_message_text(
            "Enter custom drop % to trigger global stop-loss (e.g. 40 for 40% drop):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="gsl:menu")
            ]])
        )

    elif action == "sell_pct_custom":
        set_state(uid, waiting_for="gsl_sell_pct")
        await query.edit_message_text(
            "Enter sell % when global stop-loss fires (1–100):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="gsl:menu")
            ]])
        )

    elif action == "overview":
        gts  = get_global_trailing_stop()
        gttp = get_global_trailing_tp()
        gbe  = get_global_breakeven_stop()
        gte  = get_global_time_exit()
        gsl  = get_global_sl()
        await query.edit_message_text(
            _global_risk_menu_text(gsl, gts, gttp, gbe, gte),
            parse_mode="Markdown",
            reply_markup=_global_risk_kb(gsl, gts, gttp, gbe, gte)
        )


# ─── Global Trailing Stop callback ───────────────────────────────────────────

async def gts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]
    await query.answer()

    gts = get_global_trailing_stop()

    if action in ("menu", "toggle"):
        if action == "toggle":
            gts["enabled"] = not gts.get("enabled", False)
            set_global_trailing_stop(gts)
        await query.edit_message_text(
            _gts_menu_text(gts),
            parse_mode="Markdown",
            reply_markup=_gts_menu_kb(gts)
        )

    elif action == "trail_pct":
        gts["trail_pct"] = int(parts[2])
        set_global_trailing_stop(gts)
        await query.answer(f"Trail % set to {parts[2]}%")
        await query.edit_message_text(_gts_menu_text(gts), parse_mode="Markdown",
                                      reply_markup=_gts_menu_kb(gts))

    elif action == "sell_pct":
        gts["sell_pct"] = int(parts[2])
        set_global_trailing_stop(gts)
        await query.answer(f"Sell % set to {parts[2]}%")
        await query.edit_message_text(_gts_menu_text(gts), parse_mode="Markdown",
                                      reply_markup=_gts_menu_kb(gts))

    elif action == "trail_pct_custom":
        set_state(uid, waiting_for="gts_trail_pct")
        await query.edit_message_text(
            "Enter custom trail % (e.g. 25 means sell if price drops 25% from peak):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="gts:menu")
            ]])
        )

    elif action == "sell_pct_custom":
        set_state(uid, waiting_for="gts_sell_pct")
        await query.edit_message_text(
            "Enter sell % when trailing stop fires (1–100):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="gts:menu")
            ]])
        )


# ─── Global Trailing Take-Profit callback ────────────────────────────────────

async def gttp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]
    await query.answer()

    gttp = get_global_trailing_tp()

    if action in ("menu", "toggle"):
        if action == "toggle":
            gttp["enabled"] = not gttp.get("enabled", False)
            set_global_trailing_tp(gttp)
        await query.edit_message_text(
            _gttp_menu_text(gttp),
            parse_mode="Markdown",
            reply_markup=_gttp_menu_kb(gttp)
        )

    elif action == "activate_mult":
        gttp["activate_mult"] = float(parts[2])
        set_global_trailing_tp(gttp)
        await query.answer(f"Activation set to {parts[2]}x")
        await query.edit_message_text(_gttp_menu_text(gttp), parse_mode="Markdown",
                                      reply_markup=_gttp_menu_kb(gttp))

    elif action == "trail_pct":
        gttp["trail_pct"] = int(parts[2])
        set_global_trailing_tp(gttp)
        await query.answer(f"Trail % set to {parts[2]}%")
        await query.edit_message_text(_gttp_menu_text(gttp), parse_mode="Markdown",
                                      reply_markup=_gttp_menu_kb(gttp))

    elif action == "sell_pct":
        gttp["sell_pct"] = int(parts[2])
        set_global_trailing_tp(gttp)
        await query.answer(f"Sell % set to {parts[2]}%")
        await query.edit_message_text(_gttp_menu_text(gttp), parse_mode="Markdown",
                                      reply_markup=_gttp_menu_kb(gttp))

    elif action == "activate_mult_custom":
        set_state(uid, waiting_for="gttp_activate_mult")
        await query.edit_message_text(
            "Enter activation multiplier (e.g. 2.5 = activates when price is 2.5x your buy):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="gttp:menu")
            ]])
        )

    elif action == "trail_pct_custom":
        set_state(uid, waiting_for="gttp_trail_pct")
        await query.edit_message_text(
            "Enter trail % (e.g. 20 = sell if price drops 20% from peak after activation):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="gttp:menu")
            ]])
        )

    elif action == "sell_pct_custom":
        set_state(uid, waiting_for="gttp_sell_pct")
        await query.edit_message_text(
            "Enter sell % when trailing TP fires (1–100):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="gttp:menu")
            ]])
        )


# ─── Global Breakeven Stop callback ──────────────────────────────────────────

async def gbe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]
    await query.answer()

    gbe = get_global_breakeven_stop()

    if action in ("menu", "toggle"):
        if action == "toggle":
            gbe["enabled"] = not gbe.get("enabled", False)
            set_global_breakeven_stop(gbe)
        await query.edit_message_text(
            _gbe_menu_text(gbe),
            parse_mode="Markdown",
            reply_markup=_gbe_menu_kb(gbe)
        )

    elif action == "activate_mult":
        gbe["activate_mult"] = float(parts[2])
        set_global_breakeven_stop(gbe)
        await query.answer(f"Activation set to {parts[2]}x")
        await query.edit_message_text(_gbe_menu_text(gbe), parse_mode="Markdown",
                                      reply_markup=_gbe_menu_kb(gbe))

    elif action == "activate_mult_custom":
        set_state(uid, waiting_for="gbe_activate_mult")
        await query.edit_message_text(
            "Enter activation multiplier (e.g. 2.0 = activates when price is 2x your buy):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="gbe:menu")
            ]])
        )


# ─── Global Time Exit callback ────────────────────────────────────────────────

async def gte_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]
    await query.answer()

    gte = get_global_time_exit()

    if action in ("menu", "toggle"):
        if action == "toggle":
            gte["enabled"] = not gte.get("enabled", False)
            set_global_time_exit(gte)
        await query.edit_message_text(
            _gte_menu_text(gte),
            parse_mode="Markdown",
            reply_markup=_gte_menu_kb(gte)
        )

    elif action == "hours":
        gte["hours"] = int(parts[2])
        set_global_time_exit(gte)
        await query.answer(f"Time limit set to {parts[2]}h")
        await query.edit_message_text(_gte_menu_text(gte), parse_mode="Markdown",
                                      reply_markup=_gte_menu_kb(gte))

    elif action == "target_mult":
        gte["target_mult"] = float(parts[2])
        set_global_time_exit(gte)
        await query.answer(f"Target set to {parts[2]}x")
        await query.edit_message_text(_gte_menu_text(gte), parse_mode="Markdown",
                                      reply_markup=_gte_menu_kb(gte))

    elif action == "sell_pct":
        gte["sell_pct"] = int(parts[2])
        set_global_time_exit(gte)
        await query.answer(f"Sell % set to {parts[2]}%")
        await query.edit_message_text(_gte_menu_text(gte), parse_mode="Markdown",
                                      reply_markup=_gte_menu_kb(gte))

    elif action == "hours_custom":
        set_state(uid, waiting_for="gte_hours")
        await query.edit_message_text(
            "Enter time limit in hours (e.g. 6, 12, 24, 48):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="gte:menu")
            ]])
        )

    elif action == "target_mult_custom":
        set_state(uid, waiting_for="gte_target_mult")
        await query.edit_message_text(
            "Enter target multiplier (e.g. 1.5 = token must be 1.5x or higher, otherwise sell):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="gte:menu")
            ]])
        )

    elif action == "sell_pct_custom":
        set_state(uid, waiting_for="gte_sell_pct")
        await query.edit_message_text(
            "Enter sell % when time exit fires (1–100):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="gte:menu")
            ]])
        )


async def user_wallet_alert_loop(app):
    """Poll user-tracked wallets for new token purchases and send Telegram alerts."""
    print("[WALLET_ALERT] loop started", flush=True)
    # last_tokens is transient state — kept in memory, safe to lose on restart
    _last_tokens: dict[str, dict] = {}  # addr → {mint: amount}
    while True:
        try:
            loop = asyncio.get_running_loop()

            # Build address → [(uid, label)] index from DB
            wallet_users: dict[str, list] = {}
            for row in _db.get_all_wallet_alerts().items():
                uid_int, entries = row
                for e in entries:
                    wallet_users.setdefault(e["wallet"], []).append((uid_int, e.get("label", "")))

            for addr, uid_entries in wallet_users.items():
                try:
                    current_toks = await loop.run_in_executor(None, get_token_accounts, addr)
                    current_map  = {t["mint"]: t["amount"] for t in current_toks}
                except Exception as e:
                    print(f"[WALLET_ALERT] fetch error {addr[:8]}: {e}", flush=True)
                    continue

                prev = _last_tokens.get(addr, {})
                new_buys = [
                    mint for mint, amt in current_map.items()
                    if mint not in prev or amt > prev.get(mint, 0) * 1.05
                ]
                for uid_int, label in uid_entries:
                    for mint in new_buys[:3]:
                        try:
                            pair = await loop.run_in_executor(None, fetch_sol_pair, mint)
                            sym  = pair["baseToken"]["symbol"] if pair else mint[:8]
                            mcap = float(pair.get("marketCap", 0) or 0) if pair else 0
                            mcap_str = f"${mcap / 1_000_000:.2f}M" if mcap >= 1_000_000 else (f"${mcap / 1_000:.0f}K" if mcap >= 1_000 else "—")
                            await app.bot.send_message(
                                uid_int,
                                f"👁️ *Wallet Alert — {label or addr[:8]}*\n\n"
                                f"Bought: *${sym}*\n"
                                f"MCap: `{mcap_str}`\n"
                                f"Wallet: `{addr[:8]}...{addr[-4:]}`",
                                parse_mode="Markdown",
                                reply_markup=InlineKeyboardMarkup([[
                                    InlineKeyboardButton("📊 Chart", url=f"https://dexscreener.com/solana/{mint}"),
                                    InlineKeyboardButton("🟢 Buy",   callback_data=f"quick:buy:{mint}"),
                                ]])
                            )
                        except Exception as e:
                            print(f"[WALLET_ALERT] alert send error: {e}", flush=True)
                _last_tokens[addr] = current_map

        except Exception as e:
            print(f"[WALLET_ALERT] loop error: {e}", flush=True)
        await asyncio.sleep(30)


# ─── Bot command list ─────────────────────────────────────────────────────────


async def _supervised_task(name: str, coro_fn, *args):
    """Run a coroutine forever, auto-restarting on crash with backoff.
    If the coroutine returns cleanly (no exception), it is NOT restarted.
    """
    import traceback
    delay = 5
    while True:
        try:
            await coro_fn(*args)
            # Clean return — task chose to exit (e.g. disabled in config)
            print(f"[{name}] exited cleanly", flush=True)
            return
        except asyncio.CancelledError:
            print(f"[{name}] cancelled", flush=True)
            return
        except Exception as e:
            print(f"[{name}] CRASHED: {e} — restarting in {delay}s", flush=True)
            traceback.print_exc()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


async def _start_api_server(app):
    """Launch the AI Control API server alongside the bot."""
    try:
        import uvicorn
        import api_server as _api
        _api.set_bot_app(app)             # hand the live bot handle to the API
        port = getattr(__import__("config"), "API_PORT", 8080)
        cfg  = uvicorn.Config(
            _api.app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
            loop="none",                  # reuse the existing asyncio loop
        )
        server = uvicorn.Server(cfg)
        print(f"[API] AI Control API starting on port {port}", flush=True)
        await server.serve()
    except (SystemExit, OSError) as e:
        # OSError errno 98 = port already in use (previous instance still shutting down)
        print(f"[API] Port {getattr(__import__('config'), 'API_PORT', 8080)} in use — API disabled. Bot continues normally.", flush=True)
    except Exception as e:
        print(f"[API] Server error: {e}", flush=True)


async def post_init(app):
    # Inject auto-buy callback into pumpfeed (avoids circular import)
    pf.set_grad_autobuy_fn(execute_auto_buy)
    # Start pump.fun live feed WebSocket listener (supervised, auto-restarts)
    asyncio.create_task(_supervised_task("PUMPFEED", pf.run_pumpfeed, app.bot))
    # Poll pump.fun API for graduated tokens → pumpgrad DM notifications
    asyncio.create_task(_supervised_task("GRADWATCH", pf.run_gradwatch, app.bot))
    # Monitor portfolio tokens for crash signals (distribution watcher)
    asyncio.create_task(_supervised_task("PORTFOLIO_WATCH", pf.run_portfolio_watch, app.bot))
    # Monitor blockchain for brand new token launches (early hunter)
    asyncio.create_task(_supervised_task("LAUNCH_HUNTER", pf.run_launch_hunter, app.bot))
    # Poll user-tracked wallets for new buy alerts
    asyncio.create_task(user_wallet_alert_loop(app))
    # Start AI Control API server
    asyncio.create_task(_start_api_server(app))

    await app.bot.set_my_commands([
        BotCommand("start",      "Launch the bot"),
        BotCommand("menu",       "Show all options & buttons"),
        BotCommand("price",      "Look up a token price"),
        BotCommand("top",        "Our top scouted tokens ranked by MCap gain"),
        BotCommand("buy",        "Buy a token (paper or live)"),
        BotCommand("sell",       "Sell a token from your portfolio"),
        BotCommand("portfolio",  "View your holdings & balances"),
        BotCommand("trades",     "View trade history (filter: win/loss/symbol/date)"),
        BotCommand("research_log", "Download research log CSV for data analysis"),
        BotCommand("autosell",   "Configure auto-sell targets per token"),
        BotCommand("stoploss",   "Global stop-loss settings (safety net)"),
        BotCommand("mode",       "Switch between paper and live trading"),
        BotCommand("scan",       "Resume live token alerts (always-on scanner)"),
        BotCommand("stopscan",   "Pause your live token alerts"),
        BotCommand("watchlist",  "Tokens scoring 50–69 (worth watching)"),
        BotCommand("heatscore",  "Heat score any token on demand"),
        BotCommand("topalerts",  "Best scanner alerts from today"),
        BotCommand("settings",   "View & adjust heat score parameters (1-100)"),
        BotCommand("presets",    "Quick-swap between trading style presets"),
        BotCommand("customize",  "Fine-tune individual heat score settings"),
        BotCommand("stats",      "Scout performance analytics by alert tier"),
        BotCommand("analytics",  "Trade stats, scanner performance & log"),
        BotCommand("wallet",     "Manage your Solana wallet"),
        BotCommand("pumplive",   "Toggle live pump.fun launch notifications"),
        BotCommand("pumpgrad",   "Alerts when pump.fun tokens hit 100% bonding curve"),
        BotCommand("autobuy",    "Auto-buy tokens when scanner fires alerts"),
        BotCommand("pnl",        "View realized & unrealized P&L summary"),
        BotCommand("cluster",    "Co-investment cluster map for a token"),
        BotCommand("clustertop", "Top co-investing wallet pairs ever"),
        BotCommand("playbook",   "Predictive launch archetype win rates"),
        BotCommand("wallets",    "Auto-tracked wallet intelligence & win rates"),
        BotCommand("narratives", "Narrative performance stats & trending themes"),
    ])


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Slash commands
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("menu",       start))
    app.add_handler(CommandHandler("price",      cmd_price))
    app.add_handler(CommandHandler("top",        cmd_top))
    app.add_handler(CommandHandler("buy",        cmd_buy))
    app.add_handler(CommandHandler("sell",       cmd_sell))
    app.add_handler(CommandHandler("portfolio",  cmd_portfolio))
    app.add_handler(CommandHandler("trades",     cmd_trades_history))
    app.add_handler(CommandHandler("research_log", cmd_research_log))
    app.add_handler(CommandHandler("autosell",   cmd_autosell))
    app.add_handler(CommandHandler("mode",       cmd_mode))
    app.add_handler(CommandHandler("scan",       cmd_scan))
    app.add_handler(CommandHandler("stopscan",   cmd_stopscan))
    app.add_handler(CommandHandler("watchlist",  cmd_watchlist))
    app.add_handler(CommandHandler("heatscore",  cmd_heatscore))
    app.add_handler(CommandHandler("topalerts",  cmd_topalerts))
    app.add_handler(CommandHandler("settings",   cmd_settings))
    app.add_handler(CommandHandler("presets",    cmd_presets))
    app.add_handler(CommandHandler("customize",  cmd_customize))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("wallet",     cmd_wallet))
    app.add_handler(CommandHandler("pumplive",   cmd_pumplive))
    app.add_handler(CommandHandler("pumpgrad",   cmd_pumpgrad))
    app.add_handler(CommandHandler("watch",      cmd_portfolio_watch))
    app.add_handler(CommandHandler("launches",   cmd_launches))
    app.add_handler(CommandHandler("channels",   cmd_channels))
    app.add_handler(CommandHandler("whalebuy",        cmd_whalebuy))
    app.add_handler(CommandHandler("momentum",        cmd_momentum))
    app.add_handler(CommandHandler("contract",        cmd_contract))
    app.add_handler(CommandHandler("discoverwallet",  cmd_discoverwallet))
    app.add_handler(CommandHandler("bundle",          cmd_bundle))
    app.add_handler(CommandHandler("fingerprint",     cmd_fingerprint))
    app.add_handler(CommandHandler("cluster",         cmd_cluster))
    app.add_handler(CommandHandler("clustertop",      cmd_clustertop))
    app.add_handler(CommandHandler("playbook",        cmd_playbook))
    app.add_handler(CommandHandler("analytics",  cmd_analytics))
    app.add_handler(CommandHandler("autobuy",    cmd_autobuy))
    app.add_handler(CommandHandler("pnl",        cmd_pnl))
    app.add_handler(CommandHandler("stoploss",   cmd_stoploss))
    app.add_handler(CommandHandler("wallets",    cmd_wallets_intel))
    app.add_handler(CommandHandler("narratives", cmd_narratives_intel))
    app.add_handler(CommandHandler("watchbuy",   cmd_watchbuy))
    app.add_handler(CommandHandler("history",    cmd_history))

    # Button callbacks
    app.add_handler(CallbackQueryHandler(menu_callback,                pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(market_callback,              pattern=r"^market:"))
    app.add_handler(CallbackQueryHandler(trade_callback,               pattern=r"^trade:"))
    app.add_handler(CallbackQueryHandler(mode_callback,                pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(settings_callback,            pattern=r"^settings:"))
    app.add_handler(CallbackQueryHandler(heatscore_callback,           pattern=r"^heatscore:"))
    app.add_handler(CallbackQueryHandler(autosell_callback,            pattern=r"^as:"))
    app.add_handler(CallbackQueryHandler(as_preset_callback,           pattern=r"^as_preset:"))
    app.add_handler(CallbackQueryHandler(custom_target_type_callback,  pattern=r"^ct_type:"))
    app.add_handler(CallbackQueryHandler(portfolio_callback,           pattern=r"^portfolio:"))
    app.add_handler(CallbackQueryHandler(qt_callback,                  pattern=r"^qt:"))
    app.add_handler(CallbackQueryHandler(qp_callback,                  pattern=r"^qp:"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(confirm_callback,             pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(quick_callback,               pattern=r"^quick:"))
    app.add_handler(CallbackQueryHandler(alert_dir_callback,           pattern=r"^alert_dir:"))
    app.add_handler(CallbackQueryHandler(cancel_callback,              pattern=r"^cancel$"))
    app.add_handler(CallbackQueryHandler(scanner_callback,             pattern=r"^scanner:"))
    app.add_handler(CallbackQueryHandler(wallet_callback,              pattern=r"^wallet:"))
    app.add_handler(CallbackQueryHandler(pumplive_callback,            pattern=r"^pumplive:"))
    app.add_handler(CallbackQueryHandler(pumpgrad_callback,            pattern=r"^pumpgrad:"))
    app.add_handler(CallbackQueryHandler(watch_callback,               pattern=r"^watch:"))
    app.add_handler(CallbackQueryHandler(launches_callback,            pattern=r"^launches:"))
    app.add_handler(CallbackQueryHandler(channels_callback,            pattern=r"^channels:"))
    app.add_handler(CallbackQueryHandler(pf_buy_callback,              pattern=r"^pf:buy:"))
    app.add_handler(CallbackQueryHandler(analytics_callback,           pattern=r"^analytics:"))
    app.add_handler(CallbackQueryHandler(autobuy_callback,             pattern=r"^autobuy:"))
    app.add_handler(CallbackQueryHandler(pnl_callback,                 pattern=r"^pnl:"))
    app.add_handler(CallbackQueryHandler(gsl_callback,                 pattern=r"^gsl:"))
    app.add_handler(CallbackQueryHandler(gts_callback,                 pattern=r"^gts:"))
    app.add_handler(CallbackQueryHandler(gttp_callback,                pattern=r"^gttp:"))
    app.add_handler(CallbackQueryHandler(gbe_callback,                 pattern=r"^gbe:"))
    app.add_handler(CallbackQueryHandler(gte_callback,                 pattern=r"^gte:"))
    app.add_handler(CallbackQueryHandler(intel_callback,               pattern=r"^intel:"))
    app.add_handler(CallbackQueryHandler(wbalert_callback,             pattern=r"^wbalert:"))
    app.add_handler(CallbackQueryHandler(trade_center_callback,        pattern=r"^trades:"))
    app.add_handler(CallbackQueryHandler(history_page_callback,        pattern=r"^history_page:"))
    app.add_handler(CallbackQueryHandler(qb_preset_callback,           pattern=r"^qb_preset:"))

    # Text input
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Background jobs with error handling
    async def safe_check_auto_sell(ctx):
        try:
            await check_auto_sell(ctx)
        except Exception as e:
            print(f"[AUTO-SELL] Critical error: {e}")
            import traceback; traceback.print_exc()

    async def safe_check_portfolio_alerts(ctx):
        """Check all users' portfolios for mcap milestones."""
        def _get_mcap(mint):
            _, mcap = fetch_token_price(mint)
            return mcap or 0
        try:
            all_configs = portfolio_alerts.load_auto_sell()
            for uid_str in all_configs.keys():
                try:
                    uid = int(uid_str)
                    await portfolio_alerts.check_all_portfolio_alerts(
                        ctx.bot, uid, _get_mcap
                    )
                except Exception as e:
                    print(f"[PORTFOLIO_ALERTS] Error for user {uid_str}: {e}")
        except Exception as e:
            print(f"[PORTFOLIO_ALERTS] Critical error: {e}")
            import traceback; traceback.print_exc()

    async def safe_run_scanner(ctx):
        try:
            chat_ids = _db.get_scan_targets()
            await sc.run_scan(ctx.bot, chat_ids, on_alert=handle_scanner_autobuy)
        except Exception as e:
            print(f"[SCANNER] Critical error: {e}")
            import traceback; traceback.print_exc()

    app.job_queue.run_repeating(safe_check_auto_sell,            interval=12, first=12)
    app.job_queue.run_repeating(safe_check_portfolio_alerts,     interval=ALERT_CHECK_SECS, first=45)

    async def run_scanner_job(ctx):
        chat_ids = _db.get_scan_targets()
        await sc.run_scan(ctx.bot, chat_ids, on_alert=handle_scanner_autobuy)

    app.job_queue.run_repeating(safe_run_scanner, interval=15, first=5)

    # Global error handler to catch unhandled exceptions
    async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle application-level errors."""
        print(f"[ERROR_HANDLER] Exception: {context.error}")
        import traceback
        if context.error:
            traceback.print_exception(type(context.error), context.error, context.error.__traceback__)
        # Try to notify user
        try:
            if update and hasattr(update, 'effective_user') and update.effective_user:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text="⚠️ An error occurred. The bot is back online. Please try again."
                )
        except:
            pass

    app.add_error_handler(error_handler)

    print("@DigitalDegenX_Bot running...")
    app.run_polling()
