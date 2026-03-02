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
import feed as fd

import config as _cfg
from config import (
    TELEGRAM_TOKEN, SOLANA_RPC, WALLET_PRIVATE_KEY,
    OPENCLAW_CONTAINER, ADMIN_IDS, PAPER_START_SOL, ALERT_CHECK_SECS,
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
TRADE_LOG_FILE     = os.path.join(DATA_DIR, "trade_log.json")

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
    p = load_portfolios()
    p[str(uid)] = {"SOL": PAPER_START_SOL}
    save_portfolios(p)


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


def setup_auto_sell(uid: int, mint: str, symbol: str,
                    buy_price_usd: float, raw_amount: int, decimals: int):
    """Called after every buy to create default auto-sell config."""
    existing = get_auto_sell(uid, mint)
    config = {
        "symbol":        symbol,
        "buy_price_usd": buy_price_usd,
        "initial_raw":   raw_amount,
        "decimals":      decimals,
        "enabled":       True,
        # Multiplier targets: sell_pct of current holdings when triggered
        "mult_targets": [
            {"mult": 2.0,  "sell_pct": 50, "triggered": False, "label": "2x"},
            {"mult": 4.0,  "sell_pct": 50, "triggered": False, "label": "4x"},
        ],
        # Market cap milestone alerts (USD)
        "mcap_alerts": [
            {"mcap": 100_000,   "triggered": False, "label": "100K"},
            {"mcap": 500_000,   "triggered": False, "label": "500K"},
            {"mcap": 1_000_000, "triggered": False, "label": "1M"},
        ],
        # Custom targets added by user
        "custom_targets": existing.get("custom_targets", []) if existing else [],
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Market",       callback_data="menu:market"),
         InlineKeyboardButton("🤖 AI Analyze",   callback_data="menu:analyze")],
        [InlineKeyboardButton("💰 Trade",         callback_data="menu:trade"),
         InlineKeyboardButton("👜 Portfolio",     callback_data="menu:portfolio")],
        [InlineKeyboardButton("🔔 Alerts",        callback_data="menu:alerts"),
         InlineKeyboardButton("🤖 Auto-Sell",     callback_data="menu:autosell")],
        [InlineKeyboardButton(scan_lbl,           callback_data="scanner:toggle"),
         InlineKeyboardButton("📋 Watchlist",     callback_data="scanner:watchlist"),
         InlineKeyboardButton("🏆 Top Alerts",    callback_data="scanner:topalerts")],
        [InlineKeyboardButton("👛 Wallet",        callback_data="wallet:menu"),
         InlineKeyboardButton("📡 Feeds",         callback_data="feed:menu"),
         InlineKeyboardButton(f"⚙️ Mode: {mode}", callback_data="menu:settings")],
    ])


def market_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Top Meme Coins", callback_data="market:top")],
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
    rows = []
    for mint, cfg in configs.items():
        sym     = cfg.get("symbol", mint[:6])
        enabled = "✅" if cfg.get("enabled") else "⏸️"
        rows.append([InlineKeyboardButton(
            f"{enabled} ${sym}", callback_data=f"as:view:{mint}"
        )])
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
        [InlineKeyboardButton("➕ Add Custom Target", callback_data=f"as:addcustom:{mint}")],
        [InlineKeyboardButton("🔄 Reset Targets",     callback_data=f"as:reset:{mint}")],
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
        [InlineKeyboardButton("🤖 Analyze",  callback_data=f"quick:analyze:{mint}"),
         InlineKeyboardButton("🟢 Buy",      callback_data=f"quick:buy:{mint}"),
         InlineKeyboardButton("🔔 Alert",    callback_data=f"quick:alert:{mint}")],
        [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}"),
         InlineKeyboardButton("🪙 Pump.fun",    url=f"https://pump.fun/{mint}"),
         InlineKeyboardButton("🔫 RugCheck",    url=f"https://rugcheck.xyz/tokens/{mint}")],
        [InlineKeyboardButton("⬅️ Back",     callback_data="menu:market")],
    ])


# ─── AI ───────────────────────────────────────────────────────────────────────

def ask_ai(prompt: str) -> str:
    try:
        result = subprocess.run(
            ["docker", "exec", OPENCLAW_CONTAINER, "sh", "-c",
             f"OPENCLAW_STATE_DIR=/data/.openclaw openclaw agent "
             f"--agent main --session-id meme-bot "
             f"--message {json.dumps(prompt)} --json"],
            capture_output=True, text=True, timeout=60,
        )
        output = result.stdout.strip()
        if not output:
            return result.stderr.strip() or "No AI response."
        try:
            data = json.loads(output)
            return data.get("text") or data.get("content") or data.get("message") or output
        except json.JSONDecodeError:
            return output
    except subprocess.TimeoutExpired:
        return "AI timed out."
    except Exception as e:
        return f"AI error: {e}"


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
                            InlineKeyboardButton("📊 View", callback_data=f"quick:analyze:{alert['mint']}")
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

            price, mcap = fetch_token_price(mint)
            if price is None:
                continue

            symbol  = cfg.get("symbol", mint[:6])
            changed = False

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
                                    InlineKeyboardButton("📊 View", callback_data=f"quick:analyze:{mint}"),
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
            await msg.edit_text(
                f"📄 *Paper Buy Done*\n"
                f"Spent: `{amount} SOL`\n"
                f"Received: `{out_amount:,} {symbol}` (raw)\n"
                f"Buy Price: `${price_usd:.8f}`\n"
                f"Price Impact: `{price_impact}%`\n"
                f"SOL left: `{portfolio['SOL']:.4f}`\n\n"
                f"🤖 Auto-sell configured: 2x→50%, 4x→50%\n"
                f"🎯 MCap alerts: 100K / 500K / 1M",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚙️ Auto-Sell Settings",
                                         callback_data=f"as:view:{token_mint}")],
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
    try:
        pairs = requests.get(DEXSCREENER_SEARCH + "solana+meme", timeout=10).json().get("pairs") or []
        top10 = sorted(
            [p for p in pairs if p.get("chainId") == "solana"],
            key=lambda p: float(p.get("volume", {}).get("h24", 0) or 0),
            reverse=True
        )[:10]
        if not top10:
            await send_fn("Could not fetch top tokens.", reply_markup=back_kb("menu:market"))
            return
        lines = ["*🔥 Top 10 Solana Meme Coins (24h Volume)*\n"]
        for i, p in enumerate(top10, 1):
            sym = p.get("baseToken", {}).get("symbol", "N/A")
            pr  = p.get("priceUsd", "N/A")
            h24 = p.get("priceChange", {}).get("h24", "N/A")
            try: vol = f"${float(p.get('volume',{}).get('h24',0)):,.0f}"
            except: vol = "N/A"
            lines.append(f"{i}. *${sym}* `${pr}` | {h24}% | {vol}")
        await send_fn(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="market:top"),
                 InlineKeyboardButton("⬅️ Back",    callback_data="menu:market")]
            ])
        )
    except Exception as e:
        await send_fn(f"Error: {e}")


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
        [InlineKeyboardButton("⬅️ Portfolio", callback_data=back)],
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
                as_tag = " 🤖" if as_configs.get(acc["mint"], {}).get("enabled") else ""
                lines.append(f"`{sym}`{as_tag}: {acc['ui_amount']:,.4f} ≈ `${val:,.4f}`")
                token_rows.append(InlineKeyboardButton(f"⚡ {sym}", callback_data=f"qt:{acc['mint']}"))
        else:
            lines.append("No token positions found.")

        # Group token buttons 3 per row
        kb = [token_rows[i:i+3] for i in range(0, len(token_rows), 3)]
        kb += [
            [InlineKeyboardButton("🟢 Buy",       callback_data="trade:buy"),
             InlineKeyboardButton("🔴 Sell",      callback_data="trade:sell")],
            [InlineKeyboardButton("🔄 Refresh",   callback_data="portfolio:refresh"),
             InlineKeyboardButton("🤖 Auto-Sell", callback_data="menu:autosell")],
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
                token_rows.append(InlineKeyboardButton(f"⚡ {sym}", callback_data=f"qt:{mint}"))
            else:
                lines.append(f"`{mint[:8]}...`: {raw_amt:,} raw")
                token_rows.append(InlineKeyboardButton(f"⚡ {mint[:6]}", callback_data=f"qt:{mint}"))
        if total_usd:
            lines.append(f"\n*Est. Value:* `${total_usd:,.4f}`")
    else:
        lines.append("No positions yet.")

    kb = [token_rows[i:i+3] for i in range(0, len(token_rows), 3)]
    kb += [
        [InlineKeyboardButton("🟢 Buy",       callback_data="trade:buy"),
         InlineKeyboardButton("🔴 Sell",      callback_data="trade:sell")],
        [InlineKeyboardButton("🔄 Refresh",   callback_data="portfolio:refresh"),
         InlineKeyboardButton("🤖 Auto-Sell", callback_data="menu:autosell")],
        [InlineKeyboardButton("🗑️ Reset",     callback_data="settings:reset_paper"),
         InlineKeyboardButton("⬅️ Menu",      callback_data="menu:main")],
    ]
    await send_fn("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))


async def _show_autosell(send_fn, uid: int):
    configs = load_auto_sell().get(str(uid), {})
    if not configs:
        await send_fn(
            "*🤖 Auto-Sell*\n\nNo positions tracked yet.\n"
            "Buy a token and auto-sell is configured automatically.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 Trade", callback_data="menu:trade")],
                [InlineKeyboardButton("⬅️ Back",  callback_data="menu:main")],
            ])
        )
        return
    count = len(configs)
    await send_fn(
        f"*🤖 Auto-Sell Monitor*\n\n{count} position(s) tracked.\n\n"
        "Default: 2x → sell 50% | 4x → sell 50%\n"
        "MCap alerts: 100K / 500K / 1M\n\n"
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
    return "\n".join(lines)


async def do_analyze(send_fn, query: str):
    pair = fetch_sol_pair(query)
    if not pair:
        await send_fn(f"Token `{query}` not found.", parse_mode="Markdown", reply_markup=back_kb())
        return
    market_txt = format_pair(pair).replace("*", "").replace("`", "")
    prompt = (
        f"You are a Solana meme coin analyst. Briefly analyze this token. "
        f"Cover: trend, momentum, liquidity risk, buy/avoid recommendation. "
        f"Plain text, under 200 words.\n\n{market_txt}"
    )
    loop = asyncio.get_running_loop()
    ai   = await loop.run_in_executor(None, ask_ai, prompt)
    sym  = pair["baseToken"]["symbol"]
    mint = pair["baseToken"]["address"]
    await send_fn(
        f"🤖 *Analysis — ${sym}*\n\n{ai}", parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Buy",   callback_data=f"quick:buy:{mint}"),
             InlineKeyboardButton("🔔 Alert", callback_data=f"quick:alert:{mint}")],
            [InlineKeyboardButton("⬅️ Menu",  callback_data="menu:main")],
        ])
    )


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


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/analyze <symbol or CA>`", parse_mode="Markdown")
        return
    msg = await update.message.reply_text("Analyzing...")
    await do_analyze(msg.edit_text, context.args[0])


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


# ─── Scanner commands ──────────────────────────────────────────────────────────

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


# ─── Feed commands + callback ──────────────────────────────────────────────────

async def cmd_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        fd.feed_status_text(), parse_mode="Markdown",
        reply_markup=fd.feed_settings_kb()
    )


async def feed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":")
    action = parts[1]
    await query.answer()

    cfg = fd.load_feed_config()

    if action == "menu" or action == "back":
        await query.edit_message_text(
            fd.feed_status_text(), parse_mode="Markdown",
            reply_markup=fd.feed_settings_kb()
        )

    elif action == "toggle_launch":
        cfg["launch_enabled"] = not cfg["launch_enabled"]
        fd.save_feed_config(cfg)
        await query.edit_message_text(
            fd.feed_status_text(), parse_mode="Markdown",
            reply_markup=fd.feed_settings_kb()
        )

    elif action == "toggle_migrate":
        cfg["migrate_enabled"] = not cfg["migrate_enabled"]
        fd.save_feed_config(cfg)
        await query.edit_message_text(
            fd.feed_status_text(), parse_mode="Markdown",
            reply_markup=fd.feed_settings_kb()
        )

    elif action == "toggle_post_all":
        cfg["post_all"] = not cfg.get("post_all", False)
        fd.save_feed_config(cfg)
        await query.edit_message_text(
            fd.feed_status_text(), parse_mode="Markdown",
            reply_markup=fd.feed_settings_kb()
        )

    elif action == "set_launch_ch":
        set_state(uid, waiting_for="feed_launch_channel")
        await query.edit_message_text(
            "📡 *Set Launch Channel*\n\nSend the channel ID (e.g. `-1001234567890`).\n"
            "The bot must be an admin of that channel.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="feed:back")
            ]])
        )

    elif action == "set_migrate_ch":
        set_state(uid, waiting_for="feed_migrate_channel")
        await query.edit_message_text(
            "🚀 *Set Migration Channel*\n\nSend the channel ID:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="feed:back")
            ]])
        )

    elif action == "set_mcap":
        set_state(uid, waiting_for="feed_mcap")
        await query.edit_message_text(
            f"💰 *Set MCap Range*\n\nCurrent: ${cfg['min_mcap']:,} – ${cfg['max_mcap']:,}\n\n"
            "Send two numbers separated by a dash.\nExample: `10000-500000`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="feed:back")
            ]])
        )

    elif action == "set_heat":
        set_state(uid, waiting_for="feed_heat")
        await query.edit_message_text(
            f"🌡️ *Min Heat Score*\n\nCurrent: {cfg['min_heat_score']}\n\n"
            "Send a number 0–100 (0 = post everything):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="feed:back")
            ]])
        )

    elif action == "set_wallets":
        set_state(uid, waiting_for="feed_wallets")
        await query.edit_message_text(
            f"👛 *Min Unique Wallets*\n\nCurrent: {cfg['min_wallets']}\n\nSend a number:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="feed:back")
            ]])
        )

    elif action == "set_narrative":
        await query.edit_message_text(
            f"🏷️ *Narrative Filter*\n\nCurrent: {cfg['narrative_filter']}\n\nChoose:",
            parse_mode="Markdown",
            reply_markup=fd.narrative_kb()
        )

    elif action == "narr":
        narrative = parts[2] if len(parts) > 2 else "All"
        cfg["narrative_filter"] = narrative
        fd.save_feed_config(cfg)
        await query.edit_message_text(
            fd.feed_status_text(), parse_mode="Markdown",
            reply_markup=fd.feed_settings_kb()
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
                "🟢 *Live alerts resumed!*\n\n"
                "Scanning every 15 seconds.\n"
                "Alerts fire when Heat Score ≥ 55/100.",
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
    elif action == "analyze":
        set_state(uid, waiting_for="analyze_token")
        await query.edit_message_text("🤖 *AI Analysis*\n\nSend a token symbol or CA:",
                                       parse_mode="Markdown", reply_markup=back_kb())
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
    query = update.callback_query
    await query.answer("Refreshing...")
    await query.edit_message_text("Loading...")
    await _show_portfolio(query.edit_message_text, query.from_user.id)


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

    if action == "analyze":
        await query.edit_message_text("Analyzing...")
        await do_analyze(query.edit_message_text, mint)
    elif action == "buy":
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


# ─── Forwarded message handler (channel ID finder) ────────────────────────────

async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detect forwarded channel messages and reply with the channel ID."""
    msg  = update.message
    if not msg:
        return
    fwd  = msg.forward_origin if hasattr(msg, "forward_origin") else None
    chat = getattr(fwd, "chat", None) if fwd else None
    # Older API: forward_from_chat
    if not chat:
        chat = getattr(msg, "forward_from_chat", None)
    if chat:
        cid  = chat.id
        name = getattr(chat, "title", str(cid))
        await msg.reply_text(
            f"📡 *Channel ID found!*\n\n"
            f"Channel: *{name}*\n"
            f"ID: `{cid}`\n\n"
            f"Copy the ID above and paste it into `/feed` → Set Channel.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📡 Open Feed Settings", callback_data="feed:menu")
            ]])
        )
        return
    # Not a channel forward — fall through to normal text handling


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

    elif state == "analyze_token":
        clear_state(uid)
        msg = await update.message.reply_text("Analyzing...")
        await do_analyze(msg.edit_text, text)

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

    elif state == "feed_launch_channel":
        clear_state(uid)
        cfg = fd.load_feed_config()
        cfg["launch_channel"] = text.strip()
        fd.save_feed_config(cfg)
        await update.message.reply_text(
            f"✅ Launch channel set to `{text.strip()}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📡 Feed Settings", callback_data="feed:menu")
            ]])
        )

    elif state == "feed_migrate_channel":
        clear_state(uid)
        cfg = fd.load_feed_config()
        cfg["migrate_channel"] = text.strip()
        fd.save_feed_config(cfg)
        await update.message.reply_text(
            f"✅ Migration channel set to `{text.strip()}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📡 Feed Settings", callback_data="feed:menu")
            ]])
        )

    elif state == "feed_mcap":
        clear_state(uid)
        try:
            parts_mcap = text.replace(",", "").split("-")
            mn, mx = int(float(parts_mcap[0])), int(float(parts_mcap[1]))
            cfg = fd.load_feed_config()
            cfg["min_mcap"], cfg["max_mcap"] = mn, mx
            fd.save_feed_config(cfg)
            await update.message.reply_text(
                f"✅ MCap range set: ${mn:,} – ${mx:,}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📡 Feed Settings", callback_data="feed:menu")
                ]])
            )
        except Exception:
            await update.message.reply_text("Invalid format. Try `10000-500000`.")

    elif state == "feed_heat":
        clear_state(uid)
        try:
            v = int(float(text))
            cfg = fd.load_feed_config()
            cfg["min_heat_score"] = max(0, min(100, v))
            fd.save_feed_config(cfg)
            await update.message.reply_text(
                f"✅ Min heat score set to {v}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📡 Feed Settings", callback_data="feed:menu")
                ]])
            )
        except Exception:
            await update.message.reply_text("Enter a number 0–100.")

    elif state == "feed_wallets":
        clear_state(uid)
        try:
            v = int(float(text))
            cfg = fd.load_feed_config()
            cfg["min_wallets"] = max(0, v)
            fd.save_feed_config(cfg)
            await update.message.reply_text(
                f"✅ Min wallets set to {v}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📡 Feed Settings", callback_data="feed:menu")
                ]])
            )
        except Exception:
            await update.message.reply_text("Enter a valid number.")

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

        # Free-form AI chat
        msg = await update.message.reply_text("Thinking...")
        prompt = (
            f"You are a Solana meme coin trading assistant. "
            f"User said: \"{text}\"\n\n"
            f"Respond helpfully, under 150 words, plain text."
        )
        loop = asyncio.get_running_loop()
        ai   = await loop.run_in_executor(None, ask_ai, prompt)
        await msg.edit_text(
            ai,
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

    text = "\n".join(lines)
    kb   = InlineKeyboardMarkup([[
        InlineKeyboardButton("All", callback_data="analytics:all"),
        InlineKeyboardButton("7d",  callback_data="analytics:7d"),
        InlineKeyboardButton("30d", callback_data="analytics:30d"),
        InlineKeyboardButton("🔄",  callback_data="analytics:all"),
    ]])
    return text, kb


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


# ─── Bot command list ─────────────────────────────────────────────────────────

# Module-level holder so the watcher task isn't garbage-collected
_helius_watcher = None


async def post_init(app):
    global _helius_watcher
    # Start Helius real-time WebSocket watcher if API key is configured
    if HELIUS_API_KEY:
        try:
            import helius_ws
            _helius_watcher = helius_ws.HeliusWatcher(HELIUS_API_KEY, app.bot, fd)
            _helius_watcher.start()
            print("[Helius] Real-time WebSocket watcher started")
        except Exception as e:
            print(f"[Helius] Failed to start watcher: {e}")

    await app.bot.set_my_commands([
        BotCommand("start",      "Launch the bot"),
        BotCommand("menu",       "Show all options & buttons"),
        BotCommand("price",      "Look up a token price"),
        BotCommand("top",        "Top 10 Solana meme coins by volume"),
        BotCommand("analyze",    "AI-powered token analysis"),
        BotCommand("buy",        "Buy a token (paper or live)"),
        BotCommand("sell",       "Sell a token from your portfolio"),
        BotCommand("portfolio",  "View your holdings & balances"),
        BotCommand("autosell",   "Configure auto-sell targets per token"),
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
        BotCommand("feed",       "Configure channel feed settings"),
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
    app.add_handler(CommandHandler("analyze",    cmd_analyze))
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
    app.add_handler(CommandHandler("feed",       cmd_feed))
    app.add_handler(CommandHandler("analytics",  cmd_analytics))

    # Button callbacks
    app.add_handler(CallbackQueryHandler(menu_callback,                pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(market_callback,              pattern=r"^market:"))
    app.add_handler(CallbackQueryHandler(trade_callback,               pattern=r"^trade:"))
    app.add_handler(CallbackQueryHandler(mode_callback,                pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(settings_callback,            pattern=r"^settings:"))
    app.add_handler(CallbackQueryHandler(alert_callback,               pattern=r"^alert:"))
    app.add_handler(CallbackQueryHandler(alert_dir_callback,           pattern=r"^alert_dir:"))
    app.add_handler(CallbackQueryHandler(autosell_callback,            pattern=r"^as:"))
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
    app.add_handler(CallbackQueryHandler(feed_callback,                pattern=r"^feed:"))
    app.add_handler(CallbackQueryHandler(analytics_callback,           pattern=r"^analytics:"))

    # Forwarded messages (channel ID finder)
    app.add_handler(MessageHandler(filters.FORWARDED & ~filters.COMMAND, handle_forwarded))

    # Text input
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Background jobs
    app.job_queue.run_repeating(check_price_alerts, interval=ALERT_CHECK_SECS, first=15)
    app.job_queue.run_repeating(check_auto_sell,    interval=ALERT_CHECK_SECS, first=30)

    async def run_scanner_job(ctx):
        s        = sc.load_state()
        chat_ids = s.get("scan_targets", [])
        await sc.run_scan(ctx.bot, chat_ids)

    app.job_queue.run_repeating(run_scanner_job, interval=15, first=5)

    print("@DigitalDegenX_Bot running...")
    app.run_polling()
