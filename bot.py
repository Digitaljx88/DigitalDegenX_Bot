"""
@DigitalDegenX_Bot — Solana Meme Coin AI Trading Bot
Features: menu UI + slash commands, paper/live trading, AI analysis,
          price alerts, live wallet portfolio, persistent storage,
          auto-sell (2x/4x/custom), mcap milestone alerts.
"""

import asyncio
import importlib
import json
import os
import re
import subprocess
import time
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import scanner as sc
import pumpfun
import pumpfeed as pf
import intelligence_tracker as intel

import config as _cfg
from config import (
    TELEGRAM_TOKEN, SOLANA_RPC, WALLET_PRIVATE_KEY,
    ADMIN_IDS, PAPER_START_SOL, ALERT_CHECK_SECS,
    HELIUS_API_KEY,
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
TOKEN_PROGRAM      = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

DATA_DIR           = os.path.join(os.path.dirname(__file__), "data")
PORTFOLIO_FILE     = os.path.join(DATA_DIR, "portfolios.json")
ALERTS_FILE        = os.path.join(DATA_DIR, "alerts.json")
AUTO_SELL_FILE     = os.path.join(DATA_DIR, "auto_sell.json")
AUTO_BUY_FILE      = os.path.join(DATA_DIR, "auto_buy.json")
TRADE_LOG_FILE     = os.path.join(DATA_DIR, "trade_log.json")
GLOBAL_SETTINGS_FILE = os.path.join(DATA_DIR, "global_settings.json")

os.makedirs(DATA_DIR, exist_ok=True)

DEFAULT_MCAP_MILESTONES = [100_000, 500_000, 1_000_000]

# ─── Storage helpers ──────────────────────────────────────────────────────────

def _load(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(path: str, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Portfolios ────────────────────────────────────────────────────────────────

def load_portfolios() -> dict:  return _load(PORTFOLIO_FILE)
def save_portfolios(d: dict):   _save(PORTFOLIO_FILE, d)

def get_portfolio(uid: int) -> dict:
    p = load_portfolios()
    key = str(uid)
    if key not in p:
        p[key] = {"SOL": PAPER_START_SOL}
        save_portfolios(p)
    return p[key]

def update_portfolio(uid: int, portfolio: dict):
    p = load_portfolios()
    p[str(uid)] = portfolio
    save_portfolios(p)

def reset_portfolio(uid: int):
    """Reset paper portfolio to starting balance and clear all auto-sell configs."""
    # Reset portfolio
    p = load_portfolios()
    p[str(uid)] = {"SOL": PAPER_START_SOL}
    save_portfolios(p)
    # Clear all auto-sell entries for this user
    a = load_auto_sell()
    if str(uid) in a:
        del a[str(uid)]
        save_auto_sell(a)


# ── Wallet key persistence ─────────────────────────────────────────────────────

def save_wallet_key(new_key: str):
    """Write WALLET_PRIVATE_KEY to config.py and reload the module."""
    cfg_path = os.path.join(os.path.dirname(__file__), "config.py")
    with open(cfg_path) as f:
        src = f.read()
    src = re.sub(
        r'^WALLET_PRIVATE_KEY\s*=\s*.*$',
        f'WALLET_PRIVATE_KEY = "{new_key}"',
        src, flags=re.MULTILINE
    )
    with open(cfg_path, "w") as f:
        f.write(src)
    importlib.reload(_cfg)
    global WALLET_PRIVATE_KEY
    WALLET_PRIVATE_KEY = _cfg.WALLET_PRIVATE_KEY


# ── Price alerts ──────────────────────────────────────────────────────────────

def load_alerts() -> dict:   return _load(ALERTS_FILE)
def save_alerts(d: dict):    _save(ALERTS_FILE, d)

def get_alerts(uid: int) -> list:
    return load_alerts().get(str(uid), [])

def add_alert(uid: int, mint: str, symbol: str, target: float, direction: str):
    a = load_alerts()
    a.setdefault(str(uid), []).append(
        {"mint": mint, "symbol": symbol, "target": target, "direction": direction}
    )
    save_alerts(a)

def remove_alert(uid: int, index: int):
    a = load_alerts()
    key = str(uid)
    if key in a and 0 <= index < len(a[key]):
        a[key].pop(index)
        save_alerts(a)

# ── Auto-sell configs ─────────────────────────────────────────────────────────

def load_auto_sell() -> dict:   return _load(AUTO_SELL_FILE)
def save_auto_sell(d: dict):    _save(AUTO_SELL_FILE, d)


def get_auto_sell(uid: int, mint: str) -> dict | None:
    return load_auto_sell().get(str(uid), {}).get(mint)


def set_auto_sell(uid: int, mint: str, config: dict):
    a = load_auto_sell()
    a.setdefault(str(uid), {})[mint] = config
    save_auto_sell(a)


def remove_auto_sell(uid: int, mint: str):
    a = load_auto_sell()
    if str(uid) in a and mint in a[str(uid)]:
        del a[str(uid)][mint]
        save_auto_sell(a)


def _apply_presets_to_open_positions(uid: int, presets: list) -> int:
    """Apply updated preset targets to all existing open auto-sell positions.
    Replaces mult_targets in every open position with the new presets (all triggered=False).
    Returns the count of positions updated.
    """
    a = load_auto_sell()
    user_configs = a.get(str(uid), {})
    if not user_configs:
        return 0
    new_targets = [
        {"mult": p["mult"], "sell_pct": p["sell_pct"], "triggered": False, "label": f"{p['mult']:.1f}x"}
        for p in presets
    ]
    for cfg in user_configs.values():
        cfg["mult_targets"] = [dict(t) for t in new_targets]  # fresh copy per position
    a[str(uid)] = user_configs
    save_auto_sell(a)
    return len(user_configs)


# ── Global settings ───────────────────────────────────────────────────────────

def load_global_settings() -> dict:   return _load(GLOBAL_SETTINGS_FILE)
def save_global_settings(d: dict):    _save(GLOBAL_SETTINGS_FILE, d)

def get_global_sl() -> dict:
    return load_global_settings().get("stop_loss", {
        "enabled": False, "pct": 50, "sell_pct": 100
    })

def set_global_sl(data: dict):
    gs = load_global_settings()
    gs["stop_loss"] = data
    save_global_settings(gs)

def get_user_as_presets(uid: int) -> list:
    """Get user's custom auto-sell multiplier presets.
    Returns list of dicts: [{"mult": 2.0, "sell_pct": 50}, {"mult": 4.0, "sell_pct": 50}]
    """
    gs = load_global_settings()
    defaults = [
        {"mult": 2.0, "sell_pct": 50},
        {"mult": 4.0, "sell_pct": 50},
    ]
    return gs.get(f"as_presets_{uid}", defaults)

def set_user_as_presets(uid: int, presets: list):
    """Set user's custom auto-sell multiplier presets.
    presets: list of dicts [{"mult": X, "sell_pct": Y}, ...]
    Also immediately applies the new presets to all existing open positions.
    """
    gs = load_global_settings()
    gs[f"as_presets_{uid}"] = presets
    save_global_settings(gs)
    _apply_presets_to_open_positions(uid, presets)

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
                    buy_price_usd: float, raw_amount: int, decimals: int):
    """Called after every buy to create default auto-sell config using user presets."""
    existing = get_auto_sell(uid, mint)
    # Get user's preset multipliers (defaults to 2x→50%, 4x→50%)
    user_presets = get_user_as_presets(uid)
    # Build mult_targets with triggered=False and labels
    mult_targets = []
    for preset in user_presets:
        mult_targets.append({
            "mult": preset["mult"],
            "sell_pct": preset["sell_pct"],
            "triggered": False,
            "label": f"{preset['mult']:.1f}x"
        })
    
    config = {
        "symbol":        symbol,
        "buy_price_usd": buy_price_usd,
        "initial_raw":   raw_amount,
        "decimals":      decimals,
        "enabled":       True,
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

def load_trade_log() -> list:
    d = _load(TRADE_LOG_FILE)
    return d.get("trades", []) if isinstance(d, dict) else []

def save_trade_log(trades: list):
    _save(TRADE_LOG_FILE, {"trades": trades})

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
    return load_auto_sell().get(str(uid), {}).get(mint, {}).get("buy_price_usd")

# ── Auto-buy configs ──────────────────────────────────────────────────────────

AUTO_BUY_DEFAULTS = {
    "enabled":         False,
    "sol_amount":      0.1,
    "min_score":       70,
    "max_mcap":        500_000,
    "daily_limit_sol": 1.0,
    "spent_today":     0.0,
    "spent_date":      "",
    "bought":          [],   # mints purchased this session (reset each day)
}

def load_auto_buy() -> dict:  return _load(AUTO_BUY_FILE)
def save_auto_buy(d: dict):   _save(AUTO_BUY_FILE, d)

def get_auto_buy(uid: int) -> dict:
    d = load_auto_buy()
    return d.get(str(uid), dict(AUTO_BUY_DEFAULTS))

def set_auto_buy(uid: int, cfg: dict):
    d = load_auto_buy()
    d[str(uid)] = cfg
    save_auto_buy(d)

def _ab_reset_day_if_needed(cfg: dict) -> dict:
    """Reset daily spend counter if date has changed."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if cfg.get("spent_date") != today:
        cfg["spent_today"] = 0.0
        cfg["spent_date"]  = today
        cfg["bought"]      = []
    return cfg

def log_trade(uid: int, mode: str, action: str, mint: str, symbol: str,
              name: str = "", narrative: str = None, heat_score: int = None,
              sol_amount: float = None, sol_received: float = None,
              token_amount: int = 0, price_usd: float = 0.0,
              buy_price_usd: float = None, mcap: float = 0.0,
              pnl_pct: float = None, tx_sig: str = None):
    if narrative is None:
        narrative = _detect_narrative(name, symbol)
    if action == "sell" and buy_price_usd and price_usd and pnl_pct is None:
        pnl_pct = (price_usd - buy_price_usd) / buy_price_usd * 100
    record = {
        "ts":           time.time(),
        "date":         datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "uid":          uid,
        "mode":         mode,
        "action":       action,
        "mint":         mint,
        "symbol":       symbol,
        "name":         name,
        "narrative":    narrative,
        "heat_score":   heat_score,
        "sol_amount":   sol_amount,
        "sol_received": sol_received,
        "token_amount": token_amount,
        "price_usd":    price_usd,
        "buy_price_usd": buy_price_usd,
        "mcap":         mcap,
        "pnl_pct":      pnl_pct,
        "tx_sig":       tx_sig,
    }
    trades = load_trade_log()
    trades.append(record)
    if len(trades) > 10_000:
        trades = trades[-10_000:]
    save_trade_log(trades)


# ─── In-memory state ──────────────────────────────────────────────────────────

user_modes: dict[int, str]  = {}
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

def fetch_sol_pair(query: str) -> dict | None:
    url = (DEXSCREENER_TOKEN + query) if len(query) > 30 else (DEXSCREENER_SEARCH + query)
    try:
        pairs = requests.get(url, timeout=10).json().get("pairs") or []
        sol   = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol:
            return None
        return sorted(sol, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]
    except Exception:
        return None


def fetch_token_price(mint: str) -> tuple[float | None, float | None]:
    """Returns (price_usd, mcap_usd)."""
    try:
        pairs = requests.get(DEXSCREENER_TOKEN + mint, timeout=10).json().get("pairs") or []
        sol   = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol:
            return None, None
        pair  = sorted(sol, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]
        price = float(pair.get("priceUsd", 0) or 0)
        mcap  = float(pair.get("marketCap", 0) or 0)
        return price, mcap
    except Exception:
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
    targets   = sc.load_state().get("scan_targets", [])
    scan_lbl  = "🔕 Pause Alerts" if uid in targets else "🔔 Resume Alerts"
    pf_lbl    = "🟢 Pump Live: ON" if pf.is_subscribed(uid) else "🔴 Pump Live: OFF"
    pg_lbl    = "🟢 Pump Grad: ON" if pf.is_grad_subscribed(uid) else "🔴 Pump Grad: OFF"
    ab_cfg    = get_auto_buy(uid)
    ab_lbl    = "🟢 Auto-Buy: ON" if ab_cfg.get("enabled") else "🔴 Auto-Buy: OFF"
    gsl       = get_global_sl()
    gsl_lbl   = "🟢 SL: ON" if gsl.get("enabled") else "🔴 SL: OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Market",       callback_data="menu:market"),
         InlineKeyboardButton("💰 Trade",        callback_data="menu:trade"),
         InlineKeyboardButton("👜 Portfolio",    callback_data="menu:portfolio")],
        [InlineKeyboardButton("🔔 Alerts",        callback_data="menu:alerts"),
         InlineKeyboardButton("🤖 Auto-Sell",     callback_data="menu:autosell"),
         InlineKeyboardButton(ab_lbl,             callback_data="menu:autobuy")],
        [InlineKeyboardButton(scan_lbl,           callback_data="scanner:toggle"),
         InlineKeyboardButton("📋 Watchlist",     callback_data="scanner:watchlist"),
         InlineKeyboardButton("🏆 Top Alerts",    callback_data="scanner:topalerts")],
        [InlineKeyboardButton("🌡️ Threshold",     callback_data="scanner:set_threshold"),
         InlineKeyboardButton("📣 Alert Channel", callback_data="scanner:alert_channel_menu")],
        [InlineKeyboardButton(pf_lbl,             callback_data="pumplive:toggle"),
         InlineKeyboardButton("⚙️ Live Settings", callback_data="pumplive:menu")],
        [InlineKeyboardButton(pg_lbl,             callback_data="pumpgrad:toggle"),
         InlineKeyboardButton("⚙️ Grad Settings", callback_data="pumpgrad:menu")],
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


def alerts_kb(uid: int) -> InlineKeyboardMarkup:
    rows = []
    for i, a in enumerate(get_alerts(uid)):
        arrow = "↑" if a["direction"] == "above" else "↓"
        rows.append([InlineKeyboardButton(
            f"❌ ${a['symbol']} {arrow} ${a['target']}", callback_data=f"alert:del:{i}"
        )])
    rows.append([InlineKeyboardButton("➕ New Alert", callback_data="alert:new")])
    rows.append([InlineKeyboardButton("⬅️ Back",      callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def autosell_list_kb(uid: int) -> InlineKeyboardMarkup:
    """List all tokens with auto-sell configs."""
    configs = load_auto_sell().get(str(uid), {})
    gsl     = get_global_sl()
    gsl_lbl = "🟢 ON" if gsl.get("enabled") else "🔴 OFF"
    rows = []
    for mint, cfg in configs.items():
        sym     = cfg.get("symbol", mint[:6])
        enabled = "✅" if cfg.get("enabled") else "⏸️"
        rows.append([InlineKeyboardButton(
            f"{enabled} ${sym}", callback_data=f"as:view:{mint}"
        )])
    rows.append([InlineKeyboardButton("🔧 Default Presets", callback_data="as_preset:menu"),
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


def settings_kb(uid: int) -> InlineKeyboardMarkup:
    mode = get_mode(uid)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            ("✅ " if mode == "paper" else "") + "📄 Paper Trading",
            callback_data="mode:paper"
        )],
        [InlineKeyboardButton(
            ("✅ " if mode == "live" else "") + "🔴 Live Trading",
            callback_data="mode:live"
        )],
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

def jupiter_quote(in_mint: str, out_mint: str, amount: int) -> dict | None:
    try:
        return requests.get(JUPITER_QUOTE_URL, params={
            "inputMint": in_mint, "outputMint": out_mint,
            "amount": amount, "slippageBps": 150,
        }, timeout=10).json()
    except Exception:
        return None


def execute_swap_live(quote: dict) -> str:
    if not WALLET_PRIVATE_KEY:
        return "ERROR: No WALLET_PRIVATE_KEY in config.py"
    try:
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        import base64

        keypair = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
        swap = requests.post(JUPITER_SWAP_URL, json={
            "quoteResponse": quote,
            "userPublicKey": str(keypair.pubkey()),
            "wrapAndUnwrapSol": True,
        }, timeout=15).json()

        if "swapTransaction" not in swap:
            return f"Swap build failed: {swap.get('error', swap)}"

        tx = VersionedTransaction.from_bytes(base64.b64decode(swap["swapTransaction"]))
        tx.sign([keypair])

        resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [
                base64.b64encode(bytes(tx)).decode(),
                {"encoding": "base64", "preflightCommitment": "confirmed"},
            ],
        }, timeout=30).json()

        return resp.get("result") or f"RPC error: {resp.get('error', resp)}"
    except Exception as e:
        return f"Transaction error: {e}"


def get_wallet_pubkey() -> str | None:
    if not WALLET_PRIVATE_KEY:
        return None
    try:
        from solders.keypair import Keypair
        return str(Keypair.from_base58_string(WALLET_PRIVATE_KEY).pubkey())
    except Exception:
        return None


def get_sol_balance(pubkey: str) -> float:
    try:
        resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance",
            "params": [pubkey],
        }, timeout=10).json()
        return resp["result"]["value"] / 1e9
    except Exception:
        return 0.0


def get_token_accounts(pubkey: str) -> list[dict]:
    try:
        resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [pubkey, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed"}],
        }, timeout=15).json()
        out = []
        for item in resp.get("result", {}).get("value", []):
            info = item["account"]["data"]["parsed"]["info"]
            amt  = int(info["tokenAmount"]["amount"])
            if amt > 0:
                out.append({
                    "mint":      info["mint"],
                    "amount":    amt,
                    "decimals":  info["tokenAmount"]["decimals"],
                    "ui_amount": info["tokenAmount"]["uiAmount"],
                })
        return out
    except Exception:
        return []


# ─── Auto-sell execution ──────────────────────────────────────────────────────

async def execute_auto_sell(bot, uid: int, mint: str, symbol: str,
                             sell_pct: int, reason: str, mode: str,
                             price_usd: float = 0.0, mcap: float = 0.0):
    """Sell `sell_pct`% of the current position for this user/token."""
    portfolio = get_portfolio(uid)
    raw_held  = portfolio.get(mint, 0)
    if raw_held <= 0:
        return

    sell_amount = max(1, int(raw_held * sell_pct / 100))

    if mode == "paper":
        quote = jupiter_quote(mint, SOL_MINT, sell_amount)
        if not quote or "outAmount" not in quote:
            return
        sol_received = int(quote["outAmount"]) / 1e9
        portfolio[mint] = raw_held - sell_amount
        portfolio["SOL"] = portfolio.get("SOL", 0) + sol_received
        if portfolio[mint] <= 0:
            portfolio.pop(mint, None)
        update_portfolio(uid, portfolio)
        log_trade(uid, mode, "sell", mint, symbol,
                  sol_received=sol_received, token_amount=sell_amount,
                  price_usd=price_usd, buy_price_usd=_get_buy_price(uid, mint),
                  mcap=mcap)
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
    else:
        # Live mode
        quote = jupiter_quote(mint, SOL_MINT, sell_amount)
        if not quote or "outAmount" not in quote:
            return
        sig = execute_swap_live(quote)
        sol_received = int(quote.get("outAmount", 0)) / 1e9
        if "ERROR" in sig or "error" in sig.lower():
            await bot.send_message(
                chat_id=uid,
                text=f"⚠️ *Auto-Sell Failed — {reason}*\n`${symbol}`\nError: `{sig}`",
                parse_mode="Markdown",
            )
        else:
            log_trade(uid, mode, "sell", mint, symbol,
                      sol_received=sol_received, token_amount=sell_amount,
                      price_usd=price_usd, buy_price_usd=_get_buy_price(uid, mint),
                      mcap=mcap, tx_sig=sig)
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


# ─── Background monitoring ────────────────────────────────────────────────────

async def check_price_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Check regular price alerts (above/below)."""
    alerts_data = load_alerts()
    if not alerts_data:
        return
    triggered: dict[str, list[int]] = {}
    for uid_str, user_alerts in alerts_data.items():
        to_remove = []
        for i, alert in enumerate(user_alerts):
            price, _ = fetch_token_price(alert["mint"])
            if price is None:
                continue
            hit = (
                (alert["direction"] == "above" and price >= alert["target"]) or
                (alert["direction"] == "below" and price <= alert["target"])
            )
            if hit:
                arrow = "↑" if alert["direction"] == "above" else "↓"
                try:
                    await context.bot.send_message(
                        chat_id=int(uid_str),
                        text=(
                            f"🔔 *Price Alert!*\n\n"
                            f"`${alert['symbol']}` hit `${price:.8f}`\n"
                            f"Target: {arrow} `${alert['target']}`"
                        ),
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("📊 View", callback_data=f"quick:buy:{alert['mint']}")
                        ]])
                    )
                except Exception:
                    pass
                to_remove.append(i)
        if to_remove:
            triggered[uid_str] = to_remove
    if triggered:
        alerts_data = load_alerts()
        for uid_str, indices in triggered.items():
            for i in sorted(indices, reverse=True):
                if uid_str in alerts_data and i < len(alerts_data[uid_str]):
                    alerts_data[uid_str].pop(i)
        save_alerts(alerts_data)


async def check_auto_sell(context: ContextTypes.DEFAULT_TYPE):
    """Monitor positions for auto-sell triggers and mcap milestones."""
    all_configs = load_auto_sell()
    if not all_configs:
        return

    for uid_str, tokens in all_configs.items():
        uid  = int(uid_str)
        mode = get_mode(uid)

        for mint, cfg in list(tokens.items()):
            if not cfg.get("enabled", True):
                continue

            buy_price = cfg.get("buy_price_usd", 0)
            if not buy_price:
                continue

            # Safeguard: Check if user still holds this token (prevent stale auto-sell entries)
            portfolio = get_portfolio(uid)
            if mint not in portfolio or portfolio[mint] <= 0:
                # Token sold or removed from portfolio — clean up auto-sell config
                remove_auto_sell(uid, mint)
                # Log for debugging
                print(f"[AUTO-SELL] Cleaned up stale entry: uid={uid}, mint={mint[:8]}")
                continue

            price, mcap = fetch_token_price(mint)
            if price is None:
                continue

            symbol  = cfg.get("symbol", mint[:6])
            changed = False

            # ── Hard stop-loss ────────────────────────────────────────────────
            sl = cfg.get("stop_loss", {})
            if sl.get("enabled") and not sl.get("triggered"):
                drop_pct = ((buy_price - price) / buy_price) * 100
                if drop_pct >= sl.get("pct", 50):
                    sl["triggered"] = True
                    changed = True
                    await execute_auto_sell(
                        context.bot, uid, mint, symbol,
                        sl.get("sell_pct", 100),
                        f"Stop-Loss -{sl['pct']}%", mode,
                        price_usd=price, mcap=mcap or 0
                    )

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
                        ts["triggered"] = True
                        changed = True
                        await execute_auto_sell(
                            context.bot, uid, mint, symbol,
                            ts.get("sell_pct", 100),
                            f"Trailing Stop -{ts['trail_pct']}% from peak", mode,
                            price_usd=price, mcap=mcap or 0
                        )

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
                            ttp["triggered"] = True
                            changed = True
                            await execute_auto_sell(
                                context.bot, uid, mint, symbol,
                                ttp.get("sell_pct", 50),
                                f"Trailing TP -{ttp['trail_pct']}% from peak", mode,
                                price_usd=price, mcap=mcap or 0
                            )

            # ── Time-based exit ───────────────────────────────────────────────
            te = cfg.get("time_exit", {})
            if te.get("enabled") and not te.get("triggered"):
                buy_time = te.get("buy_time", 0)
                hours_elapsed = (time.time() - buy_time) / 3600
                if hours_elapsed >= te.get("hours", 24):
                    if price < buy_price * te.get("target_mult", 2.0):
                        te["triggered"] = True
                        changed = True
                        await execute_auto_sell(
                            context.bot, uid, mint, symbol,
                            te.get("sell_pct", 100),
                            f"Time Exit ({te['hours']}h — target not reached)", mode,
                            price_usd=price, mcap=mcap or 0
                        )

            # ── Breakeven stop ────────────────────────────────────────────────
            be = cfg.get("breakeven_stop", {})
            if be.get("enabled") and not be.get("triggered"):
                if price >= buy_price * be.get("activate_mult", 2.0):
                    sl = cfg.setdefault("stop_loss", {"enabled": True, "pct": 0, "sell_pct": 100, "triggered": False})
                    sl["enabled"] = True
                    sl["pct"] = 0   # trigger at or below buy price (breakeven)
                    # NOTE: Do NOT reset triggered flag - if already triggered, don't retrigger
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
                    cfg["_gsl_triggered"] = True
                    changed = True
                    await execute_auto_sell(
                        context.bot, uid, mint, symbol,
                        gsl.get("sell_pct", 100),
                        f"Global Stop-Loss -{gsl['pct']}%", mode,
                        price_usd=price, mcap=mcap or 0
                    )

            # ── Multiplier targets (auto-sell) ────────────────────────────────
            for target in cfg.get("mult_targets", []):
                if target["triggered"]:
                    continue
                if price >= buy_price * target["mult"]:
                    target["triggered"] = True
                    changed = True
                    await execute_auto_sell(
                        context.bot, uid, mint, symbol,
                        target["sell_pct"], target["label"], mode,
                        price_usd=price, mcap=mcap or 0
                    )

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
        return

    score     = result.get("total", 0)
    mint      = result.get("mint", "")
    symbol    = result.get("symbol", mint[:6])
    name      = result.get("name", symbol)
    mcap      = result.get("mcap", 0)

    if score < cfg.get("min_score", 70):
        return
    if mcap and mcap > cfg.get("max_mcap", 500_000):
        return

    cfg = _ab_reset_day_if_needed(cfg)

    if mint in cfg.get("bought", []):
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

    mode = get_mode(uid)

    # ── Paper auto-buy ─────────────────────────────────────────────────────────
    if mode == "paper":
        portfolio = get_portfolio(uid)
        sol_bal   = portfolio.get("SOL", 0)
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

        lamports = int(sol_amount * 1_000_000_000)
        quote    = jupiter_quote(SOL_MINT, mint, lamports)
        if not quote or "error" in quote:
            return

        out_amount = int(quote.get("outAmount", 0))
        price_usd  = result.get("price_usd", 0)
        decimals   = 6  # default; DexScreener not re-fetched here

        portfolio["SOL"]  = sol_bal - sol_amount
        portfolio[mint]   = portfolio.get(mint, 0) + out_amount
        update_portfolio(uid, portfolio)
        setup_auto_sell(uid, mint, symbol, price_usd, out_amount, decimals)
        log_trade(uid, "paper", "buy", mint, symbol, name=name,
                  sol_amount=sol_amount, token_amount=out_amount,
                  price_usd=price_usd, mcap=mcap, heat_score=score)

        cfg["bought"].append(mint)
        cfg["spent_today"] = spent_today + sol_amount
        set_auto_buy(uid, cfg)

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

    lamports = int(sol_amount * 1_000_000_000)

    # Try pump.fun bonding curve first
    bc     = pumpfun.fetch_bonding_curve_data(mint, SOLANA_RPC)
    tx_sig = None
    price_usd = result.get("price_usd", 0)

    if bc and not bc.get("complete"):
        tok_est = pumpfun.calculate_buy_tokens(lamports, bc)
        tx_sig  = pumpfun.buy_token(mint, sol_amount, SOLANA_RPC, WALLET_PRIVATE_KEY)
        out_raw = tok_est
        route   = "pump.fun"
        decimals = 6
    else:
        quote = jupiter_quote(SOL_MINT, mint, lamports)
        if not quote or "error" in quote:
            return
        tx_sig   = execute_swap_live(quote)
        out_raw  = int(quote.get("outAmount", 0))
        route    = "jupiter"
        decimals = 6

    success = tx_sig and not tx_sig.startswith("ERROR")

    if success:
        # Update portfolio tracking
        pubkey = get_wallet_pubkey()
        if pubkey:
            sol_bal = get_sol_balance(pubkey)
        setup_auto_sell(uid, mint, symbol, price_usd, out_raw, decimals)
        log_trade(uid, "live", "buy", mint, symbol, name=name,
                  sol_amount=sol_amount, token_amount=out_raw,
                  price_usd=price_usd, mcap=mcap, heat_score=score, tx_sig=tx_sig)

        cfg["bought"].append(mint)
        cfg["spent_today"] = spent_today + sol_amount
        set_auto_buy(uid, cfg)

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


async def handle_scanner_autobuy(bot, result: dict):
    """Called by run_scan when a token hits the alert threshold."""
    s        = sc.load_state()
    chat_ids = s.get("scan_targets", [])
    for uid in chat_ids:
        try:
            await execute_auto_buy(bot, uid, result)
        except Exception:
            pass


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
    if not pair:
        await msg.edit_text("Token not found on Solana.", reply_markup=back_kb())
        return

    token_mint  = pair["baseToken"]["address"]
    symbol      = pair["baseToken"]["symbol"]
    name        = pair["baseToken"].get("name", "")
    price_usd   = float(pair.get("priceUsd", 0) or 0)
    mcap        = float(pair.get("marketCap", 0) or 0)
    decimals    = int(pair.get("baseToken", {}).get("decimals", 6) or 6)

    if action == "buy":
        lamports = int(amount * 1_000_000_000)
        quote    = jupiter_quote(SOL_MINT, token_mint, lamports)
    else:
        quote = jupiter_quote(token_mint, SOL_MINT, int(amount))

    if not quote or "error" in quote:
        await msg.edit_text(f"Quote failed: {quote}", reply_markup=back_kb())
        return

    out_amount   = int(quote.get("outAmount", 0))
    price_impact = quote.get("priceImpactPct", "N/A")

    # ── Paper ─────────────────────────────────────────────────────────────────
    if mode == "paper":
        portfolio = get_portfolio(uid)
        if action == "buy":
            if portfolio.get("SOL", 0) < amount:
                await msg.edit_text(
                    f"Insufficient paper SOL. Balance: `{portfolio.get('SOL',0):.4f}`",
                    parse_mode="Markdown", reply_markup=back_kb()
                )
                return
            portfolio["SOL"]     = portfolio.get("SOL", 0) - amount
            portfolio[token_mint] = portfolio.get(token_mint, 0) + out_amount
            update_portfolio(uid, portfolio)
            log_trade(uid, "paper", "buy", token_mint, symbol, name=name,
                      sol_amount=amount, token_amount=out_amount,
                      price_usd=price_usd, mcap=mcap)
            # Set up auto-sell monitoring
            setup_auto_sell(uid, token_mint, symbol, price_usd, out_amount, decimals)
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
            held = portfolio.get(token_mint, 0)
            if held < int(amount):
                await msg.edit_text(
                    f"Insufficient balance. Hold: `{held:,}` raw",
                    parse_mode="Markdown", reply_markup=back_kb()
                )
                return
            sol_received            = out_amount / 1e9
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

    context.user_data[f"pending_{action}"] = {
        "via": "jupiter", "quote": quote, "symbol": symbol,
        "mint": token_mint, "price_usd": price_usd,
        "raw_out": out_amount, "decimals": decimals,
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


async def _show_portfolio(send_fn, uid: int):
    mode = get_mode(uid)
    as_configs = load_auto_sell().get(str(uid), {})

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
        lines    = [
            f"🔴 *Live Wallet*\n`{pubkey[:8]}...{pubkey[-6:]}`\n\n"
            f"SOL: `{sol_bal:.4f}`\n"
        ]
        token_rows = []
        if accounts:
            lines.append("*Positions — tap ⚡ to trade:*")
            for acc in accounts[:10]:
                pair   = fetch_sol_pair(acc["mint"])
                sym    = pair.get("baseToken", {}).get("symbol", acc["mint"][:8]) if pair else acc["mint"][:8]
                price  = float(pair.get("priceUsd", 0) or 0) if pair else 0
                val    = price * acc["ui_amount"]
                as_cfg = as_configs.get(acc["mint"], {})
                as_tag = " 🤖" if as_cfg.get("enabled") else ""
                lines.append(f"`{sym}`{as_tag}: {acc['ui_amount']:,.4f} ≈ `${val:,.4f}`")
                token_rows.append([
                    InlineKeyboardButton(f"⚡ {sym}",  callback_data=f"qt:{acc['mint']}"),
                    InlineKeyboardButton("📊", url=f"https://dexscreener.com/solana/{acc['mint']}"),
                    InlineKeyboardButton("🪙", url=f"https://pump.fun/{acc['mint']}"),
                ])
                if as_cfg.get("enabled"):
                    token_rows.append([
                        InlineKeyboardButton(f"🤖 {sym} Auto-Sell Config", callback_data=f"as:view:{acc['mint']}")
                    ])
        else:
            lines.append("No token positions found.")

        kb = token_rows[:]   # each entry is already a [⚡, 📊, 🪙] row
        kb += [
            [InlineKeyboardButton("🟢 Buy",       callback_data="trade:buy"),
             InlineKeyboardButton("🔴 Sell",      callback_data="trade:sell")],
            [InlineKeyboardButton("🔄 Refresh",   callback_data="portfolio:refresh"),
             InlineKeyboardButton("🤖 Auto-Sell", callback_data="menu:autosell")],
            [InlineKeyboardButton("💣 Sell All",  callback_data="portfolio:sell_all_confirm")],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu:main")],
        ]
        await send_fn("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        return

    # Paper portfolio
    portfolio = get_portfolio(uid)
    sol_bal   = portfolio.get("SOL", 0)
    positions = {k: v for k, v in portfolio.items() if k != "SOL" and v > 0}
    lines     = [f"📄 *Paper Portfolio*\n\nSOL: `{sol_bal:.4f}`\n"]
    total_usd = 0.0
    token_rows = []

    if positions:
        lines.append("*Positions — tap ⚡ to trade:*")
        for mint, raw_amt in positions.items():
            pair = fetch_sol_pair(mint)
            cfg  = as_configs.get(mint)
            if pair:
                sym       = pair.get("baseToken", {}).get("symbol", mint[:8])
                price     = float(pair.get("priceUsd", 0) or 0)
                dec       = int(pair.get("baseToken", {}).get("decimals", 6) or 6)
                ui        = raw_amt / (10 ** dec)
                val       = price * ui
                total_usd += val
                buy_price = cfg.get("buy_price_usd", 0) if cfg else 0
                gain_pct  = ((price - buy_price) / buy_price * 100) if buy_price else 0
                gain_str  = f" (+{gain_pct:.0f}% 🔥)" if gain_pct >= 100 else f" ({gain_pct:+.0f}%)" if buy_price else ""
                as_tag    = " 🤖" if cfg and cfg.get("enabled") else ""
                lines.append(f"`{sym}`{as_tag}: {ui:,.4f} ≈ `${val:,.4f}`{gain_str}")
                if cfg and cfg.get("enabled"):
                    pending = [t["label"] for t in cfg.get("mult_targets", []) if not t["triggered"]]
                    if pending:
                        lines.append(f"  ↳ Next target: {pending[0]}")
                token_rows.append([
                    InlineKeyboardButton(f"⚡ {sym}", callback_data=f"qt:{mint}"),
                    InlineKeyboardButton("📊", url=f"https://dexscreener.com/solana/{mint}"),
                    InlineKeyboardButton("🪙", url=f"https://pump.fun/{mint}"),
                ])
                if cfg and cfg.get("enabled"):
                    token_rows.append([
                        InlineKeyboardButton(f"🤖 {sym} Auto-Sell Config", callback_data=f"as:view:{mint}")
                    ])
            else:
                lines.append(f"`{mint[:8]}...`: {raw_amt:,} raw")
                token_rows.append([
                    InlineKeyboardButton(f"⚡ {mint[:6]}", callback_data=f"qt:{mint}"),
                    InlineKeyboardButton("📊", url=f"https://dexscreener.com/solana/{mint}"),
                    InlineKeyboardButton("🪙", url=f"https://pump.fun/{mint}"),
                ])
        if total_usd:
            lines.append(f"\n*Est. Value:* `${total_usd:,.4f}`")
    else:
        lines.append("No positions yet.")

    kb = token_rows[:]   # each entry is already a [⚡, 📊, 🪙] row
    kb += [
        [InlineKeyboardButton("🟢 Buy",       callback_data="trade:buy"),
         InlineKeyboardButton("🔴 Sell",      callback_data="trade:sell")],
        [InlineKeyboardButton("🔄 Refresh",   callback_data="portfolio:refresh"),
         InlineKeyboardButton("🤖 Auto-Sell", callback_data="menu:autosell")],
        [InlineKeyboardButton("💣 Sell All",  callback_data="portfolio:sell_all_confirm")],
        [InlineKeyboardButton("🗑️ Reset",     callback_data="settings:reset_paper"),
         InlineKeyboardButton("⬅️ Menu",      callback_data="menu:main")],
    ]
    await send_fn("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))


async def _show_autosell(send_fn, uid: int):
    configs = load_auto_sell().get(str(uid), {})
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
    cfg         = get_auto_buy(uid)
    cfg         = _ab_reset_day_if_needed(cfg)
    enabled     = cfg.get("enabled", False)
    sol_amount  = cfg.get("sol_amount", 0.1)
    min_score   = cfg.get("min_score", 70)
    max_mcap    = cfg.get("max_mcap", 500_000)
    daily_limit = cfg.get("daily_limit_sol", 1.0)
    spent       = cfg.get("spent_today", 0.0)
    bought      = cfg.get("bought", [])
    mode        = "📄 Paper" if get_mode(uid) == "paper" else "🔴 Live"

    status = "🟢 ENABLED" if enabled else "🔴 DISABLED"
    return (
        f"*🤖 Auto-Buy Settings*\n\n"
        f"Status: *{status}*\n"
        f"Mode: *{mode}*\n\n"
        f"SOL per trade: `{sol_amount} SOL`\n"
        f"Min heat score: `{min_score}/100`\n"
        f"Max MCap: `${max_mcap:,.0f}`\n"
        f"Daily SOL limit: `{'Unlimited ♾️' if daily_limit == 0 else str(daily_limit) + ' SOL'}`\n"
        f"Spent today: `{spent:.3f} SOL`\n"
        f"Bought today: `{len(bought)}` token(s)\n\n"
        f"_Auto-buys fire when scanner alerts a token above your min score._"
    )


def _autobuy_kb(uid: int) -> InlineKeyboardMarkup:
    cfg     = get_auto_buy(uid)
    enabled = cfg.get("enabled", False)
    toggle_lbl = "⏸️ Disable" if enabled else "▶️ Enable"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_lbl,            callback_data="autobuy:toggle")],
        [InlineKeyboardButton("💰 SOL Amount",        callback_data="autobuy:set_sol"),
         InlineKeyboardButton("🌡️ Min Score",         callback_data="autobuy:set_score")],
        [InlineKeyboardButton("🏦 Max MCap",          callback_data="autobuy:set_mcap"),
         InlineKeyboardButton("📅 Daily Limit",       callback_data="autobuy:set_daily")],
        [InlineKeyboardButton("🔄 Reset Today",       callback_data="autobuy:reset_day")],
        [InlineKeyboardButton("⬅️ Back",              callback_data="menu:main")],
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
            "Higher = fewer but better trades.\n"
            f"Current: `{cfg.get('min_score', 70)}/120`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("35", callback_data="autobuy:score_preset:35"),
                 InlineKeyboardButton("40", callback_data="autobuy:score_preset:40"),
                 InlineKeyboardButton("45", callback_data="autobuy:score_preset:45"),
                 InlineKeyboardButton("50", callback_data="autobuy:score_preset:50")],
                [InlineKeyboardButton("55", callback_data="autobuy:score_preset:55"),
                 InlineKeyboardButton("60", callback_data="autobuy:score_preset:60"),
                 InlineKeyboardButton("65", callback_data="autobuy:score_preset:65"),
                 InlineKeyboardButton("70", callback_data="autobuy:score_preset:70")],
                [InlineKeyboardButton("75", callback_data="autobuy:score_preset:75"),
                 InlineKeyboardButton("80", callback_data="autobuy:score_preset:80"),
                 InlineKeyboardButton("90", callback_data="autobuy:score_preset:90"),
                 InlineKeyboardButton("100", callback_data="autobuy:score_preset:100")],
                [InlineKeyboardButton("⬅️ Back", callback_data="autobuy:menu")],
            ])
        )

    elif action == "score_preset":
        val = int(query.data.split(":")[2])
        cfg["min_score"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
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

    elif action == "menu":
        clear_state(uid)
        await _show_autobuy(query.edit_message_text, uid)


# ─── Commands ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_state(uid)
    # Auto-subscribe to live alerts
    s = sc.load_state()
    s["scanning"] = True
    targets = s.get("scan_targets", [])
    if uid not in targets:
        targets.append(uid)
    s["scan_targets"] = targets
    sc.save_state(s)
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


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Mode: *{'📄 Paper' if get_mode(uid) == 'paper' else '🔴 Live'}*\n\nSelect:",
        parse_mode="Markdown", reply_markup=settings_kb(uid)
    )


async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: `/alert <symbol|CA> <above|below> <price>`\n"
            "Example: `/alert BONK above 0.00005`",
            parse_mode="Markdown"
        )
        return
    uid, query, direction = update.effective_user.id, context.args[0], context.args[1].lower()
    if direction not in ("above", "below"):
        await update.message.reply_text("Direction must be `above` or `below`.", parse_mode="Markdown")
        return
    try:
        target = float(context.args[2])
    except ValueError:
        await update.message.reply_text("Invalid price.")
        return
    msg  = await update.message.reply_text("Setting alert...")
    pair = fetch_sol_pair(query)
    if not pair:
        await msg.edit_text("Token not found.")
        return
    add_alert(uid, pair["baseToken"]["address"], pair["baseToken"]["symbol"], target, direction)
    await msg.edit_text(
        f"✅ Alert set — `${pair['baseToken']['symbol']}` {direction} `${target}`",
        parse_mode="Markdown", reply_markup=back_kb("menu:alerts")
    )


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    alerts = get_alerts(uid)
    text   = f"*🔔 Alerts* — {len(alerts)} active\n\nTap ❌ to remove." if alerts else "No alerts set."
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=alerts_kb(uid))


async def cmd_autosell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Loading auto-sell...")
    await _show_autosell(msg.edit_text, update.effective_user.id)


async def cmd_stoploss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick access to global stop-loss settings."""
    uid = update.effective_user.id
    gsl = get_global_sl()
    on = gsl.get("enabled", False)
    status_txt = "🟢 Enabled" if on else "🔴 Disabled"
    await update.message.reply_text(
        f"*🌍 Global Stop-Loss*\n\n"
        f"Status: {status_txt}\n"
        f"Trigger: price drops `{gsl.get('pct', 50)}%` from buy price\n"
        f"Action: sell `{gsl.get('sell_pct', 100)}%` of position\n\n"
        f"Applies to ALL tracked positions as a safety net.",
        parse_mode="Markdown",
        reply_markup=_gsl_menu_kb(gsl)
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
    s = sc.load_state()
    s["scanning"] = True
    targets = s.get("scan_targets", [])
    if uid not in targets:
        targets.append(uid)
    s["scan_targets"] = targets
    sc.save_state(s)
    await update.message.reply_text(
        "🟢 *Live Scanner Active!*\n\n"
        "Scanning pump.fun + DexScreener every 15 seconds.\n"
        "You'll be alerted instantly when Heat Score ≥ 55/100.\n\n"
        "Use /stopscan to pause your alerts.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔕 Pause Alerts", callback_data="scanner:toggle"),
            InlineKeyboardButton("📋 Watchlist",    callback_data="scanner:watchlist"),
            InlineKeyboardButton("🏆 Top Alerts",   callback_data="scanner:topalerts"),
        ]])
    )


async def cmd_stopscan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s   = sc.load_state()
    targets = s.get("scan_targets", [])
    if uid in targets:
        targets.remove(uid)
    s["scan_targets"] = targets
    sc.save_state(s)
    await update.message.reply_text(
        "⏸ *Alerts paused for you.*\n\nThe scanner keeps running in the background.\nUse /scan to resume your alerts.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔔 Resume Alerts", callback_data="scanner:toggle"),
            InlineKeyboardButton("📋 Menu",          callback_data="menu:main"),
        ]])
    )


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wl = sc.get_watchlist()
    if not wl:
        await update.message.reply_text(
            "📋 *Watchlist is empty.*\n\nTokens scoring 50–69 appear here automatically as the scanner runs.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏆 Top Alerts", callback_data="scanner:topalerts"),
                InlineKeyboardButton("⬅️ Menu",       callback_data="menu:main"),
            ]])
        )
        return
    items = sorted(wl.values(), key=lambda x: -x.get("ts", 0))[:20]
    lines = ["*📋 Watchlist* — tokens scoring 50–69\n"]
    for t in items:
        lines.append(
            f"⚪ *{t['name']}* (${t['symbol']}) — {t['score']}/100\n"
            f"   MCap: ${t.get('mcap', 0):,.0f}"
        )
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏆 Top Alerts", callback_data="scanner:topalerts"),
            InlineKeyboardButton("⬅️ Menu",       callback_data="menu:main"),
        ]])
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
            "🏆 *No alerts fired today yet.*\n\nThe scanner is live — alerts will appear here as hot tokens are found.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Watchlist", callback_data="scanner:watchlist"),
                InlineKeyboardButton("⬅️ Menu",      callback_data="menu:main"),
            ]])
        )
        return
    top = sorted(alerts, key=lambda x: -x.get("timestamp", 0))[:10]
    lines = ["*🏆 Recent Alerts Today*\n"]
    for i, e in enumerate(top, 1):
        label = sc.priority_label(e["score"])
        lines.append(f"{i}. {label} *{e['name']}* (${e['symbol']}) — {e['score']}/100")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 Watchlist", callback_data="scanner:watchlist"),
            InlineKeyboardButton("⬅️ Menu",      callback_data="menu:main"),
        ]])
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
            lines.append(f"  Heat: {heat}/120")
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
            f"Score: {token_entry.get('score', '?')}/120",
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
            loop = asyncio.get_event_loop()
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

        loop    = asyncio.get_event_loop()
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

        loop = asyncio.get_event_loop()
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

        loop    = asyncio.get_event_loop()
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

        loop = asyncio.get_event_loop()
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

        loop   = asyncio.get_event_loop()
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

    await send_fn(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✨ Create New Wallet", callback_data="wallet:create"),
             InlineKeyboardButton("📥 Import Wallet",     callback_data="wallet:import")],
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
        from solders.keypair import Keypair as _KP
        kp     = _KP()
        pubkey = str(kp.pubkey())
        secret = kp.to_base58_string()
        set_state(uid, pending_wallet_key=secret)
        await query.edit_message_text(
            f"*✨ New Wallet Generated*\n\n"
            f"📬 Address: `{pubkey}`\n\n"
            f"🔑 Private Key:\n`{secret}`\n\n"
            f"⚠️ *SAVE THIS KEY NOW — it cannot be recovered!*\n\n"
            f"Tap Save to set this as your active wallet.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Save as Active Wallet", callback_data="wallet:save_pending")],
                [InlineKeyboardButton("❌ Discard",               callback_data="wallet:menu")],
            ])
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
        set_state(uid, waiting_for="wallet_import_key")
        await query.edit_message_text(
            "*📥 Import Wallet*\n\nPaste your base58 private key:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="wallet:menu")
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
                [InlineKeyboardButton("✏️ Set Channel",      callback_data="pumplive:set_channel")],
                *([ [InlineKeyboardButton("🗑️ Remove Channel", callback_data="pumplive:clear_channel")] ] if ch else []),
                [InlineKeyboardButton(dm_lbl,                callback_data="pumplive:toggle")],
                [InlineKeyboardButton("⬅️ Back",             callback_data="pumplive:menu")],
            ])
        )

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
                [InlineKeyboardButton("✏️ Set Channel",      callback_data="pumpgrad:set_channel")],
                *([ [InlineKeyboardButton("🗑️ Remove Channel", callback_data="pumpgrad:clear_channel")] ] if ch else []),
                [InlineKeyboardButton(dm_lbl,                callback_data="pumpgrad:toggle")],
                [InlineKeyboardButton("⬅️ Back",             callback_data="pumpgrad:menu")],
            ])
        )

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
    loop = asyncio.get_event_loop()

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
        # Estimate tokens from price (may be 0 if not listed yet)
        tok_est = int((sol / price) * (10 ** decimals)) if price > 0 else 0
        portfolio["SOL"]  = portfolio.get("SOL", 0) - sol
        portfolio[mint]   = portfolio.get(mint, 0) + tok_est
        update_portfolio(uid, portfolio)
        log_trade(uid, "paper", "buy", mint, sym, sol_amount=sol,
                  token_amount=tok_est, price_usd=price, mcap=mcap)
        if price > 0:
            setup_auto_sell(uid, mint, sym, price, tok_est, decimals)
        await query.edit_message_text(
            f"📄 *Paper Buy — ${sym}*\n"
            f"Spent: `{sol} SOL`\n"
            f"Got: `{tok_est/(10**decimals):,.4f}` tokens\n"
            + (f"Price: `${price:.8f}`\n" if price else "⚠️ Not yet listed on DexScreener\n")
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


# ─── Scanner callback ──────────────────────────────────────────────────────────

async def scanner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    action = query.data.split(":")[1]
    await query.answer()

    if action == "toggle":
        s       = sc.load_state()
        s["scanning"] = True
        targets = s.get("scan_targets", [])
        if uid in targets:
            # Pause alerts for this user
            targets.remove(uid)
            s["scan_targets"] = targets
            sc.save_state(s)
            await query.edit_message_text(
                "⏸ *Alerts paused for you.*\n\nScanner keeps running in the background.\nTap Resume to get alerts again.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔔 Resume Alerts", callback_data="scanner:toggle"),
                    InlineKeyboardButton("⬅️ Menu",          callback_data="menu:main"),
                ]])
            )
        else:
            # Resume alerts for this user
            targets.append(uid)
            s["scan_targets"] = targets
            sc.save_state(s)
            await query.edit_message_text(
                f"🟢 *Live alerts resumed!*\n\n"
                f"Scanning every 15 seconds.\n"
                f"Alerts fire when Heat Score ≥ {sc.get_user_min_score(uid)}/120.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔕 Pause Alerts", callback_data="scanner:toggle"),
                    InlineKeyboardButton("📋 Watchlist",    callback_data="scanner:watchlist"),
                ]])
            )

    elif action == "watchlist":
        wl = sc.get_watchlist()
        if not wl:
            await query.edit_message_text(
                "📋 *Watchlist is empty.*\n\nTokens scoring 50–69 appear here automatically.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏆 Top Alerts", callback_data="scanner:topalerts"),
                    InlineKeyboardButton("⬅️ Menu",       callback_data="menu:main"),
                ]])
            )
            return
        items = sorted(wl.values(), key=lambda x: -x.get("ts", 0))[:20]
        lines = ["*📋 Watchlist* — tokens scoring 50–69\n"]
        for t in items:
            lines.append(
                f"⚪ *{t['name']}* (${t['symbol']}) — {t['score']}/100\n"
                f"   MCap: ${t.get('mcap', 0):,.0f}"
            )
        await query.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏆 Top Alerts", callback_data="scanner:topalerts"),
                InlineKeyboardButton("⬅️ Menu",       callback_data="menu:main"),
            ]])
        )

    elif action == "topalerts":
        alerts = sc.get_todays_alerts()
        if not alerts:
            await query.edit_message_text(
                "🏆 *No alerts fired today yet.*\n\nThe scanner is live — alerts will appear here as tokens are found.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 Watchlist", callback_data="scanner:watchlist"),
                    InlineKeyboardButton("⬅️ Menu",      callback_data="menu:main"),
                ]])
            )
            return
        top   = sorted(alerts, key=lambda x: -x.get("timestamp", 0))[:10]
        lines = ["*🏆 Recent Alerts Today*\n"]
        for i, e in enumerate(top, 1):
            label = sc.priority_label(e["score"])
            lines.append(f"{i}. {label} *{e['name']}* (${e['symbol']}) — {e['score']}/100")
        await query.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Watchlist", callback_data="scanner:watchlist"),
                InlineKeyboardButton("⬅️ Menu",      callback_data="menu:main"),
            ]])
        )

    elif action == "set_threshold":
        cur = sc.get_user_min_score(uid)
        await query.edit_message_text(
            f"*🌡️ Alert Score Threshold*\n\n"
            f"Current: `{cur}/120`\n\n"
            f"You'll only receive alerts for tokens scoring at or above this value.\n"
            f"Lower = more alerts · Higher = fewer but stronger signals.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("40", callback_data="scanner:threshold:40"),
                 InlineKeyboardButton("45", callback_data="scanner:threshold:45"),
                 InlineKeyboardButton("50", callback_data="scanner:threshold:50"),
                 InlineKeyboardButton("55", callback_data="scanner:threshold:55")],
                [InlineKeyboardButton("60", callback_data="scanner:threshold:60"),
                 InlineKeyboardButton("65", callback_data="scanner:threshold:65"),
                 InlineKeyboardButton("70", callback_data="scanner:threshold:70"),
                 InlineKeyboardButton("75", callback_data="scanner:threshold:75")],
                [InlineKeyboardButton("80", callback_data="scanner:threshold:80"),
                 InlineKeyboardButton("85", callback_data="scanner:threshold:85"),
                 InlineKeyboardButton("90", callback_data="scanner:threshold:90"),
                 InlineKeyboardButton("100", callback_data="scanner:threshold:100")],
                [InlineKeyboardButton("⬅️ Back", callback_data="menu:main")],
            ])
        )

    elif action == "threshold":
        val = int(query.data.split(":")[2])
        sc.set_user_min_score(uid, val)
        await query.edit_message_text(
            f"✅ Alert threshold set to `{val}/120`\n\nYou'll receive alerts for tokens scoring ≥ {val}.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Main Menu", callback_data="menu:main"),
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
    elif action == "trade":
        mode = "📄 Paper" if get_mode(uid) == "paper" else "🔴 Live"
        await query.edit_message_text(f"*💰 Trade* — {mode}\n\nChoose:",
                                       parse_mode="Markdown", reply_markup=trade_kb())
    elif action == "portfolio":
        await query.edit_message_text("Loading...")
        await _show_portfolio(query.edit_message_text, uid)
    elif action == "alerts":
        alerts = get_alerts(uid)
        await query.edit_message_text(
            f"*🔔 Alerts* — {len(alerts)} active\n\nTap ❌ to remove.",
            parse_mode="Markdown", reply_markup=alerts_kb(uid)
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
    if chosen == "paper" and prev != "paper":
        reset_portfolio(uid)
    label = "📄 Paper" if chosen == "paper" else "🔴 Live"
    note  = "Switched to paper trading. Virtual portfolio reset to 10 SOL." if (chosen == "paper" and prev != "paper") else ("📄 Already in paper mode." if chosen == "paper" else "⚠️ Real trades active.")
    await query.edit_message_text(f"✅ Mode: *{label}*\n\n{note}", parse_mode="Markdown",
                                   reply_markup=back_kb())


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    action = query.data.split(":")[1]
    await query.answer()
    if action == "reset_paper":
        reset_portfolio(uid)
        await query.edit_message_text("🗑️ Paper portfolio reset to `10 SOL`.",
                                       parse_mode="Markdown", reply_markup=back_kb())


async def alert_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]
    await query.answer()

    if action == "new":
        set_state(uid, waiting_for="alert_token")
        await query.edit_message_text(
            "🔔 *New Alert*\n\nSend token symbol or CA:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="menu:alerts")]])
        )
    elif action == "del":
        remove_alert(uid, int(parts[2]))
        alerts = get_alerts(uid)
        await query.edit_message_text(
            f"*🔔 Alerts* — {len(alerts)} active",
            parse_mode="Markdown", reply_markup=alerts_kb(uid)
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
        presets_str = format_as_presets(user_presets)
        
        # Build display of current presets
        presets_display = "📊 *Current Auto-Sell Presets:*\n"
        for i, p in enumerate(user_presets):
            presets_display += f"{i+1}. {p['mult']:.1f}x → Sell {p['sell_pct']}%\n"
        
        await query.edit_message_text(
            presets_display + 
            f"\nThese will apply to your next trades.\n\n"
            f"Choose an option:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Edit Presets", callback_data="as_preset:edit")],
                [InlineKeyboardButton("↩️ Back", callback_data="menu:main")],
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
        await _show_portfolio(query.edit_message_text, uid)

    elif action == "sell_all_confirm":
        await query.answer()
        mode = get_mode(uid)
        if mode == "live":
            pubkey   = get_wallet_pubkey()
            accounts = get_token_accounts(pubkey) if pubkey else []
            count    = len(accounts)
        else:
            portfolio = get_portfolio(uid)
            count     = len([k for k, v in portfolio.items() if k != "SOL" and v > 0])
        if count == 0:
            await query.answer("No token positions to sell.", show_alert=True)
            return
        mode_label = "🔴 Live" if mode == "live" else "📄 Paper"
        await query.edit_message_text(
            f"⚠️ *Sell All — Confirm*\n\n"
            f"Sell *{count} token position(s)* at 100% each.\n"
            f"Mode: {mode_label}\n\n"
            f"This cannot be undone. Proceed?",
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
            portfolio = get_portfolio(uid)
            positions = {k: v for k, v in list(portfolio.items()) if k != "SOL" and v > 0}
            total_sol = 0.0
            for mint, raw_held in positions.items():
                pair  = fetch_sol_pair(mint)
                sym   = pair.get("baseToken", {}).get("symbol", mint[:8]) if pair else mint[:8]
                quote = jupiter_quote(mint, SOL_MINT, raw_held)
                if quote and "outAmount" in quote:
                    sol_recv = int(quote["outAmount"]) / 1e9
                    portfolio.pop(mint, None)
                    portfolio["SOL"] = portfolio.get("SOL", 0) + sol_recv
                    total_sol += sol_recv
                    remove_auto_sell(uid, mint)
                    results.append(f"✅ `${sym}` → `{sol_recv:.4f} SOL`")
                else:
                    results.append(f"❌ `${sym}` — quote failed")
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
                quote    = jupiter_quote(mint, SOL_MINT, raw_held)
                if quote and "outAmount" in quote:
                    sig      = execute_swap_live(quote)
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
            portfolio = get_portfolio(uid)
            raw_held  = portfolio.get(mint, 0)
            if raw_held <= 0:
                await query.edit_message_text(f"No `${sym}` position to sell.", reply_markup=_pct_kb(mint))
                return
            sell_raw = max(1, int(raw_held * pct / 100))
            quote    = jupiter_quote(mint, SOL_MINT, sell_raw)
            if not quote or "outAmount" not in quote:
                await query.edit_message_text("Quote failed. Try again.", reply_markup=_pct_kb(mint))
                return
            sol_recv          = int(quote["outAmount"]) / 1e9
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
            quote    = jupiter_quote(mint, SOL_MINT, sell_raw)
            if not quote or "outAmount" not in quote:
                await query.edit_message_text("Quote failed. Try again.", reply_markup=_pct_kb(mint))
                return
            sig = execute_swap_live(quote)
            sol_recv = int(quote.get("outAmount", 0)) / 1e9
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
            portfolio = get_portfolio(uid)
            sol_bal   = portfolio.get("SOL", 0)
            sol_spend = sol_bal * pct / 100
            if sol_spend < 0.001:
                await query.edit_message_text(f"Insufficient SOL balance (`{sol_bal:.4f}`).", reply_markup=_pct_kb(mint))
                return
            lamports = int(sol_spend * 1_000_000_000)
            quote    = jupiter_quote(SOL_MINT, mint, lamports)
            if not quote or "outAmount" not in quote:
                await query.edit_message_text("Quote failed. Try again.", reply_markup=_pct_kb(mint))
                return
            out_raw            = int(quote["outAmount"])
            portfolio["SOL"]   = sol_bal - sol_spend
            portfolio[mint]    = portfolio.get(mint, 0) + out_raw
            update_portfolio(uid, portfolio)
            setup_auto_sell(uid, mint, sym, price, out_raw, dec)
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
            lamports = int(sol_spend * 1_000_000_000)
            quote    = jupiter_quote(SOL_MINT, mint, lamports)
            if not quote or "outAmount" not in quote:
                await query.edit_message_text("Quote failed. Try again.", reply_markup=_pct_kb(mint))
                return
            sig     = execute_swap_live(quote)
            out_raw = int(quote.get("outAmount", 0))
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

    await query.edit_message_text(f"Executing {action}...")
    loop  = asyncio.get_event_loop()
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
            quote    = jupiter_quote(SOL_MINT, pending["mint"], lamports)
            if not quote:
                await query.edit_message_text("Jupiter quote failed.", reply_markup=back_kb())
                return
            sig = await loop.run_in_executor(None, execute_swap_live, quote)
            pending["raw_out"] = int(quote.get("outAmount", pending.get("tok_est", 0)))
    else:
        sig = await loop.run_in_executor(None, execute_swap_live, pending["quote"])

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
                pending["price_usd"], raw_out, pending["decimals"]
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
        await query.edit_message_text(
            f"✅ *{action.title()} Submitted*\n"
            f"Token: `{pending['symbol']}`\n"
            f"Tx: `{sig}`\n"
            f"[Solscan](https://solscan.io/tx/{sig})  "
            f"[DexScreener](https://dexscreener.com/solana/{mint})  "
            f"[Pump](https://pump.fun/{mint})"
            + ("\n\n🤖 Auto-sell configured: 2x→50%, 4x→50%" if action == "buy" else ""),
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
        await query.edit_message_text(
            "🟢 *Buy*\n\nHow much SOL to spend?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
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
    query     = update.callback_query
    uid       = query.from_user.id
    direction = query.data.split(":")[1]
    await query.answer()
    set_state(uid, waiting_for="alert_price", alert_direction=direction)
    symbol = get_state(uid, "alert_symbol", "token")
    await query.edit_message_text(
        f"🔔 `${symbol}` {direction}...\n\nEnter target price (USD):",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
    )


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = query.from_user.id
    context.user_data.pop("pending_buy", None)
    context.user_data.pop("pending_sell", None)
    clear_state(uid)
    await query.answer("Cancelled")
    await show_main_menu(query, uid, edit=True)


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

    elif state == "alert_token":
        msg  = await update.message.reply_text("Finding token...")
        pair = fetch_sol_pair(text)
        if not pair:
            await msg.edit_text("Not found.", reply_markup=back_kb("menu:alerts"))
            return
        mint   = pair["baseToken"]["address"]
        symbol = pair["baseToken"]["symbol"]
        set_state(uid, waiting_for="alert_direction", alert_mint=mint, alert_symbol=symbol)
        await msg.edit_text(
            f"🔔 *Alert for ${symbol}*\n\nAlert when price goes:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↑ Above", callback_data="alert_dir:above"),
                 InlineKeyboardButton("↓ Below", callback_data="alert_dir:below")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ])
        )

    elif state == "alert_price":
        try:
            target = float(text)
        except ValueError:
            await update.message.reply_text("Invalid price. Try `0.00005`.", parse_mode="Markdown")
            return
        mint      = get_state(uid, "alert_mint")
        symbol    = get_state(uid, "alert_symbol")
        direction = get_state(uid, "alert_direction")
        clear_state(uid)
        add_alert(uid, mint, symbol, target, direction)
        await update.message.reply_text(
            f"✅ Alert set — `${symbol}` {direction} `${target}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔔 View Alerts", callback_data="menu:alerts")
            ]])
        )

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
                    InlineKeyboardButton("↩️ Try Again", callback_data="wallet:import")
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
            if val < 35 or val > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a score between 35 and 100.")
            return
        cfg = get_auto_buy(uid)
        cfg["min_score"] = val
        set_auto_buy(uid, cfg)
        clear_state(uid)
        await update.message.reply_text(
            f"✅ Auto-buy min score set to `{val}/120`",
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
    trades = load_trade_log()
    now    = time.time()
    cutoff = now - days * 86400 if days else 0
    trades = [t for t in trades if t.get("uid") == uid and t.get("ts", 0) >= cutoff]

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
    trades   = load_trade_log()
    my_trades = [t for t in trades if t.get("uid") == uid]
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
    as_configs = load_auto_sell().get(str(uid), {})
    portfolio  = get_portfolio(uid)
    unreal_lines = []
    total_unreal_sol = 0.0
    for mint, cfg in as_configs.items():
        buy_price = cfg.get("buy_price_usd", 0)
        symbol    = cfg.get("symbol", mint[:6])
        decimals  = cfg.get("decimals", 6)
        if not buy_price:
            continue
        raw_held = portfolio.get(mint, 0) if mode == "paper" else 0
        if mode == "live":
            # Try to get current balance from auto_sell initial_raw as estimate
            raw_held = cfg.get("initial_raw", 0)
        if not raw_held:
            continue
        try:
            price, _ = fetch_token_price(mint)
            if not price:
                continue
            pnl_pct = ((price - buy_price) / buy_price) * 100
            ui_amt  = raw_held / (10 ** decimals)
            cur_val_sol = price * ui_amt / 150  # rough SOL estimate
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
        gs = load_global_settings()
        watcher_enabled = gs.get(f"watcher_enabled_{uid}", True)
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
        gs = load_global_settings()
        watcher_enabled = gs.get(f"watcher_enabled_{uid}", True)
        gs[f"watcher_enabled_{uid}"] = not watcher_enabled
        save_global_settings(gs)
        
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
        gs = load_global_settings()
        hunter_enabled = gs.get(f"launch_hunter_enabled_{uid}", True)
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
        gs = load_global_settings()
        hunter_enabled = gs.get(f"launch_hunter_enabled_{uid}", True)
        gs[f"launch_hunter_enabled_{uid}"] = not hunter_enabled
        save_global_settings(gs)
        
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
    gs = load_global_settings()
    return {
        "main": gs.get("main_alert_channel_id", getattr(config, 'MAIN_CHANNEL_ID', None)),
        "launches": gs.get("launch_alert_channel_id", getattr(config, 'LAUNCH_ALERT_CHANNEL_ID', None)),
    }

def set_alert_channel(channel_type: str, channel_id: int) -> bool:
    """Save alert channel ID to global settings."""
    gs = load_global_settings()
    if channel_type == "main":
        gs["main_alert_channel_id"] = channel_id
    elif channel_type == "launches":
        gs["launch_alert_channel_id"] = channel_id
    else:
        return False
    save_global_settings(gs)
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
    
    main_status = format_channel_id(channels.get("main"))
    launches_status = format_channel_id(channels.get("launches"))
    
    await update.message.reply_text(
        "⚙️ <b>Alert Channel Settings</b>\n\n"
        f"📊 <b>Main Channel</b> (portfolio/pumpfun alerts)\n"
        f"   Status: {main_status}\n\n"
        f"🚀 <b>Launch Channel</b> (early token launches)\n"
        f"   Status: {launches_status}\n\n"
        f"<i>Click below to configure channels</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Set Main Channel", callback_data="channels:set:main")],
            [InlineKeyboardButton("🚀 Set Launch Channel", callback_data="channels:set:launches")],
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
            "launches": "🚀 Launch Channel (new tokens)"
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
            main_id = channels.get("main")
            if main_id:
                await context.bot.send_message(
                    chat_id=main_id,
                    text="✅ <b>Main Channel Test</b>\n\nThis channel is configured correctly!",
                    parse_mode="HTML"
                )
                msg += "✅ Main channel working\n"
            else:
                msg += "⚠️ Main channel not set\n"
        except Exception as e:
            msg += f"❌ Main channel error: {str(e)[:50]}\n"
        
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
        
        channels_obj = get_alert_channels()
        main_status = format_channel_id(channels_obj.get("main"))
        launches_status = format_channel_id(channels_obj.get("launches"))
        
        msg += f"\n<b>Current Configuration:</b>\n"
        msg += f"📊 Main: {main_status}\n"
        msg += f"🚀 Launch: {launches_status}"
        
        await query.edit_message_text(
            msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Set Main", callback_data="channels:set:main")],
                [InlineKeyboardButton("🚀 Set Launch", callback_data="channels:set:launches")],
                [InlineKeyboardButton("⬅️ Back", callback_data="channels:menu")],
            ])
        )
    
    elif action == "menu":
        channels_obj = get_alert_channels()
        main_status = format_channel_id(channels_obj.get("main"))
        launches_status = format_channel_id(channels_obj.get("launches"))
        
        await query.edit_message_text(
            "⚙️ <b>Alert Channel Settings</b>\n\n"
            f"📊 <b>Main Channel</b> (portfolio/pumpfun alerts)\n"
            f"   Status: {main_status}\n\n"
            f"🚀 <b>Launch Channel</b> (early token launches)\n"
            f"   Status: {launches_status}\n\n"
            f"<i>Click below to configure channels</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Set Main Channel", callback_data="channels:set:main")],
                [InlineKeyboardButton("🚀 Set Launch Channel", callback_data="channels:set:launches")],
                [InlineKeyboardButton("🧪 Test Channels", callback_data="channels:test")],
                [InlineKeyboardButton("⬅️ Menu", callback_data="menu:main")],
            ])
        )


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


# ─── Bot command list ─────────────────────────────────────────────────────────


async def post_init(app):
    # Inject auto-buy callback into pumpfeed (avoids circular import)
    pf.set_grad_autobuy_fn(execute_auto_buy)
    # Start pump.fun live feed WebSocket listener as background task
    asyncio.create_task(pf.run_pumpfeed(app.bot))
    # Poll pump.fun API for graduated tokens → pumpgrad DM notifications
    asyncio.create_task(pf.run_gradwatch(app.bot))
    # Monitor portfolio tokens for crash signals (distribution watcher)
    asyncio.create_task(pf.run_portfolio_watch(app.bot))
    # Monitor blockchain for brand new token launches (early hunter)
    asyncio.create_task(pf.run_launch_hunter(app.bot))

    await app.bot.set_my_commands([
        BotCommand("start",      "Launch the bot"),
        BotCommand("menu",       "Show all options & buttons"),
        BotCommand("price",      "Look up a token price"),
        BotCommand("top",        "Our top scouted tokens ranked by MCap gain"),
        BotCommand("buy",        "Buy a token (paper or live)"),
        BotCommand("sell",       "Sell a token from your portfolio"),
        BotCommand("portfolio",  "View your holdings & balances"),
        BotCommand("autosell",   "Configure auto-sell targets per token"),
        BotCommand("stoploss",   "Global stop-loss settings (safety net)"),
        BotCommand("mode",       "Switch between paper and live trading"),
        BotCommand("alert",      "Set a price alert for a token"),
        BotCommand("alerts",     "View & manage your active alerts"),
        BotCommand("scan",       "Resume live token alerts (always-on scanner)"),
        BotCommand("stopscan",   "Pause your live token alerts"),
        BotCommand("watchlist",  "Tokens scoring 50–69 (worth watching)"),
        BotCommand("heatscore",  "Heat score any token on demand"),
        BotCommand("topalerts",  "Best scanner alerts from today"),
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
    app.add_handler(CommandHandler("autosell",   cmd_autosell))
    app.add_handler(CommandHandler("mode",       cmd_mode))
    app.add_handler(CommandHandler("alert",      cmd_alert))
    app.add_handler(CommandHandler("alerts",     cmd_alerts))
    app.add_handler(CommandHandler("scan",       cmd_scan))
    app.add_handler(CommandHandler("stopscan",   cmd_stopscan))
    app.add_handler(CommandHandler("watchlist",  cmd_watchlist))
    app.add_handler(CommandHandler("heatscore",  cmd_heatscore))
    app.add_handler(CommandHandler("topalerts",  cmd_topalerts))
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

    # Button callbacks
    app.add_handler(CallbackQueryHandler(menu_callback,                pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(market_callback,              pattern=r"^market:"))
    app.add_handler(CallbackQueryHandler(trade_callback,               pattern=r"^trade:"))
    app.add_handler(CallbackQueryHandler(mode_callback,                pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(settings_callback,            pattern=r"^settings:"))
    app.add_handler(CallbackQueryHandler(alert_callback,               pattern=r"^alert:"))
    app.add_handler(CallbackQueryHandler(alert_dir_callback,           pattern=r"^alert_dir:"))
    app.add_handler(CallbackQueryHandler(autosell_callback,            pattern=r"^as:"))
    app.add_handler(CallbackQueryHandler(as_preset_callback,           pattern=r"^as_preset:"))
    app.add_handler(CallbackQueryHandler(custom_target_type_callback,  pattern=r"^ct_type:"))
    app.add_handler(CallbackQueryHandler(portfolio_callback,           pattern=r"^portfolio:"))
    app.add_handler(CallbackQueryHandler(qt_callback,                  pattern=r"^qt:"))
    app.add_handler(CallbackQueryHandler(qp_callback,                  pattern=r"^qp:"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(confirm_callback,             pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(quick_callback,               pattern=r"^quick:"))
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
    app.add_handler(CallbackQueryHandler(intel_callback,               pattern=r"^intel:"))

    # Text input
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Background jobs
    app.job_queue.run_repeating(check_price_alerts, interval=ALERT_CHECK_SECS, first=15)
    app.job_queue.run_repeating(check_auto_sell,    interval=ALERT_CHECK_SECS, first=30)

    async def run_scanner_job(ctx):
        s        = sc.load_state()
        chat_ids = s.get("scan_targets", [])
        await sc.run_scan(ctx.bot, chat_ids, on_alert=handle_scanner_autobuy)

    app.job_queue.run_repeating(run_scanner_job, interval=15, first=5)

    print("@DigitalDegenX_Bot running...")
    app.run_polling()
