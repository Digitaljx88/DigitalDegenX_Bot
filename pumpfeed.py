"""
pump.fun live launch feed — real-time new token notifications via
the PumpPortal WebSocket (wss://pumpportal.fun/api/data).

Supports per-user filters: MCap, dev buy, socials, keywords,
blocked words, tracked wallets, and blocked wallets.
"""

import asyncio
import json
import os
import time
import requests
import websockets
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from scanner import calculate_heat_score, fetch_rugcheck, priority_label
import wallet_tracker

DATA_DIR   = os.path.join(os.path.dirname(__file__), "data")
STATE_FILE = os.path.join(DATA_DIR, "pumpfeed_state.json")
os.makedirs(DATA_DIR, exist_ok=True)

WS_URL         = "wss://pumpportal.fun/api/data"
PUMPFUN_API    = "https://frontend-api-v3.pump.fun"
DEDUP_TTL      = 86400
GRAD_SOL       = 85.0
VIRT_OFFSET    = 30.0
GRADWATCH_SECS = 30          # poll every 30s for faster detection
GRAD_MAX_AGE_H = 4           # only fetch tokens graduated within last 4 hours

# Injected by bot.py at startup to avoid circular import
_grad_autobuy_fn = None


def _build_scanner_token(token: dict, meta: dict, sol_usd: float, dex: str = "") -> dict:
    """Map a pumpfeed token + meta to the dict format expected by calculate_heat_score."""
    mcap_sol = float(token.get("marketCapSol", 0) or 0)
    return {
        "mint":        token.get("mint", ""),
        "name":        token.get("name", ""),
        "symbol":      token.get("symbol", ""),
        "description": meta.get("description", ""),
        "twitter_url": meta.get("twitter", ""),
        "mcap":        mcap_sol * sol_usd,
        "price_usd":   0.0,
        "volume_m5":   0,
        "volume_h1":   0,
        "pair_created": 0,
        "dex":         dex,
        "liquidity":   max(0.0, float(token.get("vSolInBondingCurve", 0) or 0) - VIRT_OFFSET) * sol_usd,
    }

def set_grad_autobuy_fn(fn):
    global _grad_autobuy_fn
    _grad_autobuy_fn = fn


DEFAULT_FILTERS = {
    "min_mcap_sol":        0.0,
    "max_mcap_sol":        0.0,
    "min_vol_sol":         0.0,   # real SOL raised in bonding curve
    "max_vol_sol":         0.0,
    "min_dev_sol":         0.0,   # dev initial buy in SOL
    "max_dev_sol":         0.0,
    "max_token_age_mins":  0.0,   # only alert within X mins of creation (0 = any)
    "require_social":      False,
    "require_description": False,
    "keywords":            [],    # include — must match ≥1
    "blocked_words":       [],    # exclude — must match 0
    "tracked_wallets":     [],    # always notify
    "blocked_wallets":     [],    # never notify
}

# ─── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        # Migrate old list-format to dict-format
        if isinstance(s.get("subscribers"), list):
            old = s["subscribers"]
            s["subscribers"] = {
                str(uid): {"active": True, "filters": dict(DEFAULT_FILTERS)}
                for uid in old
            }
            save_state(s)
        return s
    except (FileNotFoundError, json.JSONDecodeError):
        return {"subscribers": {}, "seen": {}}


def save_state(s: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


def _prune_seen(s: dict):
    cutoff = time.time() - DEDUP_TTL
    s["seen"] = {m: t for m, t in s.get("seen", {}).items() if t > cutoff}


# ─── Alert channel helpers ─────────────────────────────────────────────────────

def get_pumplive_channel() -> str | None:
    return load_state().get("pumplive_channel")

def set_pumplive_channel(ch: str | None):
    s = load_state()
    if ch:
        s["pumplive_channel"] = ch
    else:
        s.pop("pumplive_channel", None)
    save_state(s)

def get_pumpgrad_channel() -> str | None:
    return load_state().get("pumpgrad_channel")

def set_pumpgrad_channel(ch: str | None):
    s = load_state()
    if ch:
        s["pumpgrad_channel"] = ch
    else:
        s.pop("pumpgrad_channel", None)
    save_state(s)


def get_subscribers() -> dict:
    return load_state().get("subscribers", {})


def is_subscribed(uid: int) -> bool:
    subs = load_state().get("subscribers", {})
    return subs.get(str(uid), {}).get("active", False)


def subscribe(uid: int):
    s = load_state()
    key = str(uid)
    if key not in s.setdefault("subscribers", {}):
        s["subscribers"][key] = {"active": True, "filters": dict(DEFAULT_FILTERS)}
    else:
        s["subscribers"][key]["active"] = True
    save_state(s)


def unsubscribe(uid: int):
    s = load_state()
    key = str(uid)
    if key in s.get("subscribers", {}):
        s["subscribers"][key]["active"] = False
    save_state(s)


def get_filters(uid: int) -> dict:
    s    = load_state()
    user = s.get("subscribers", {}).get(str(uid), {})
    return {**DEFAULT_FILTERS, **user.get("filters", {})}


def set_filters(uid: int, filters: dict):
    s   = load_state()
    key = str(uid)
    if key not in s.setdefault("subscribers", {}):
        s["subscribers"][key] = {"active": False, "filters": {}}
    s["subscribers"][key]["filters"] = filters
    save_state(s)


def reset_filters(uid: int):
    set_filters(uid, dict(DEFAULT_FILTERS))


# ─── SOL price ────────────────────────────────────────────────────────────────

_sol_price_cache: dict = {"price": 0.0, "ts": 0.0}


def get_sol_price() -> float:
    now = time.time()
    if now - _sol_price_cache["ts"] < 60 and _sol_price_cache["price"]:
        return _sol_price_cache["price"]
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            timeout=5,
        ).json()
        price = float(r["solana"]["usd"])
        _sol_price_cache.update({"price": price, "ts": now})
        return price
    except Exception:
        return _sol_price_cache["price"] or 150.0


# ─── URI metadata ─────────────────────────────────────────────────────────────

def fetch_uri_metadata(uri: str) -> dict:
    if not uri or not uri.startswith("http"):
        return {}
    try:
        j = requests.get(uri, timeout=5).json()
        return {
            "description": (j.get("description") or "").strip(),
            "image":    j.get("image")    or "",
            "twitter":  j.get("twitter")  or "",
            "telegram": j.get("telegram") or "",
            "website":  j.get("website")  or "",
        }
    except Exception:
        return {}


# ─── Filter logic ─────────────────────────────────────────────────────────────

def passes_filter(token: dict, meta: dict, filters: dict) -> bool:
    """Return True if this token passes all of the user's active filters."""
    mcap_sol = float(token.get("marketCapSol", 0) or 0)
    dev_sol  = float(token.get("solAmount",    0) or 0)
    vol_sol  = max(0.0, float(token.get("vSolInBondingCurve", VIRT_OFFSET) or VIRT_OFFSET) - VIRT_OFFSET)
    creator  = (token.get("traderPublicKey") or "").strip()
    name     = (token.get("name",   "") or "").lower()
    symbol   = (token.get("symbol", "") or "").lower()
    desc     = (meta.get("description", "") or "").lower()
    text_all = f"{name} {symbol} {desc}"

    has_social = bool(meta.get("twitter") or meta.get("telegram"))
    has_desc   = bool(meta.get("description", "").strip())

    # Tracked wallets always bypass all filters
    tracked = [w.strip() for w in filters.get("tracked_wallets", []) if w.strip()]
    if creator and creator in tracked:
        return True

    # Blocked wallets — never notify
    blocked_wallets = [w.strip() for w in filters.get("blocked_wallets", []) if w.strip()]
    if creator and creator in blocked_wallets:
        return False

    # Token age — only alert within X minutes of creation
    max_age = filters.get("max_token_age_mins") or 0
    if max_age > 0:
        created_ts = token.get("created_timestamp") or token.get("blockTime")
        if created_ts:
            # PumpPortal gives blockTime in seconds; created_timestamp in ms
            ts_sec = created_ts / 1000 if created_ts > 1e10 else created_ts
            age_mins = (time.time() - ts_sec) / 60
            if age_mins > max_age:
                return False

    # MCap range
    min_mcap = filters.get("min_mcap_sol") or 0
    max_mcap = filters.get("max_mcap_sol") or 0
    if min_mcap > 0 and mcap_sol < min_mcap:
        return False
    if max_mcap > 0 and mcap_sol > max_mcap:
        return False

    # SOL volume (real SOL raised in bonding curve)
    min_vol = filters.get("min_vol_sol") or 0
    max_vol = filters.get("max_vol_sol") or 0
    if min_vol > 0 and vol_sol < min_vol:
        return False
    if max_vol > 0 and vol_sol > max_vol:
        return False

    # Dev buy range
    min_dev = filters.get("min_dev_sol") or 0
    max_dev = filters.get("max_dev_sol") or 0
    if min_dev > 0 and dev_sol < min_dev:
        return False
    if max_dev > 0 and dev_sol > max_dev:
        return False

    # Require social
    if filters.get("require_social") and not has_social:
        return False

    # Require description
    if filters.get("require_description") and not has_desc:
        return False

    # Include keywords — must match at least one
    kws = [k.strip().lower() for k in filters.get("keywords", []) if k.strip()]
    if kws and not any(k in text_all for k in kws):
        return False

    # Blocked words — must match zero
    bws = [k.strip().lower() for k in filters.get("blocked_words", []) if k.strip()]
    if any(k in text_all for k in bws):
        return False

    return True


# ─── UI helpers ───────────────────────────────────────────────────────────────

def _sol_range_str(mn, mx) -> str:
    if mn and mx:
        return f"{mn}–{mx}◎"
    if mn:
        return f"≥{mn}◎"
    if mx:
        return f"≤{mx}◎"
    return "any"


def filter_status_text(uid: int) -> str:
    active  = is_subscribed(uid)
    filters = get_filters(uid)

    status = "🟢 *ON*" if active else "🔴 *OFF*"

    mcap_str = _sol_range_str(filters["min_mcap_sol"], filters["max_mcap_sol"])
    vol_str  = _sol_range_str(filters.get("min_vol_sol", 0), filters.get("max_vol_sol", 0))
    dev_str  = _sol_range_str(filters["min_dev_sol"],  filters["max_dev_sol"])
    age_str  = f"≤{filters['max_token_age_mins']:.0f}m" if filters.get("max_token_age_mins") else "any"
    soc_str  = "✅ Required" if filters["require_social"]      else "any"
    dsc_str  = "✅ Required" if filters["require_description"] else "any"
    kws      = filters["keywords"]
    bws      = filters["blocked_words"]
    tracked  = filters["tracked_wallets"]
    blocked  = filters["blocked_wallets"]

    channel  = get_pumplive_channel()
    ch_str   = f"`{channel}`" if channel else "not set"

    lines = [
        "📡 *PUMP LIVE — FILTER SETTINGS*",
        "",
        f"Status: {status}",
        f"📣 Channel: {ch_str}",
        "━━━━━━━━━━━━━━━━━━━",
        f"💰 MCap: `{mcap_str}`",
        f"📈 SOL Vol: `{vol_str}`",
        f"🛒 Dev Buy: `{dev_str}`",
        f"🕐 Token Age: `{age_str}`",
        f"🔗 Socials: {soc_str}",
        f"📝 Description: {dsc_str}",
    ]
    if kws:
        lines.append(f"🏷️ Keywords: `{', '.join(kws)}`")
    else:
        lines.append("🏷️ Keywords: any")

    if bws:
        lines.append(f"🚫 Blocked words: `{', '.join(bws)}`")
    else:
        lines.append("🚫 Blocked words: none")

    tw = len(tracked)
    bw = len(blocked)
    lines.append(f"👛 Tracked wallets: {tw}  ·  🚫 Blocked wallets: {bw}")
    lines += [
        "━━━━━━━━━━━━━━━━━━━",
        "_All active filters must pass. Tracked wallets bypass filters._",
    ]
    return "\n".join(lines)


def filter_kb(uid: int) -> InlineKeyboardMarkup:
    active  = is_subscribed(uid)
    filters = get_filters(uid)
    soc_lbl = "🔗 Social: ✅" if filters["require_social"]      else "🔗 Social: ANY"
    dsc_lbl = "📝 Desc: ✅"   if filters["require_description"] else "📝 Desc: ANY"
    on_lbl  = "🔴 Turn OFF"   if active                         else "🟢 Turn ON"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(on_lbl,        callback_data="pumplive:toggle"),
            InlineKeyboardButton("🔄 Reset",     callback_data="pumplive:reset"),
        ],
        [
            InlineKeyboardButton("💰 MCap",      callback_data="pumplive:set_mcap"),
            InlineKeyboardButton("📈 SOL Vol",   callback_data="pumplive:set_vol"),
            InlineKeyboardButton("🕐 Age",       callback_data="pumplive:set_age"),
        ],
        [
            InlineKeyboardButton("🛒 Dev Buy",   callback_data="pumplive:set_devbuy"),
        ],
        [
            InlineKeyboardButton(soc_lbl,        callback_data="pumplive:toggle_social"),
            InlineKeyboardButton(dsc_lbl,        callback_data="pumplive:toggle_desc"),
        ],
        [
            InlineKeyboardButton("🏷️ Keywords",  callback_data="pumplive:set_keywords"),
            InlineKeyboardButton("🚫 Block Words",callback_data="pumplive:set_blocked"),
        ],
        [
            InlineKeyboardButton("👛 Track Wallet", callback_data="pumplive:set_tracked"),
            InlineKeyboardButton("🚫 Block Wallet", callback_data="pumplive:set_block_wallet"),
        ],
        [
            InlineKeyboardButton("📣 Alert Channel", callback_data="pumplive:channel_menu"),
        ],
        [
            InlineKeyboardButton("⬅️ Menu", callback_data="menu:main"),
        ],
    ])


# ─── Notification formatting ──────────────────────────────────────────────────

def _bar(pct: float, width: int = 10) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def format_notification(token: dict, meta: dict, sol_usd: float, heat: dict | None = None) -> str:
    name     = token.get("name",   "Unknown")
    symbol   = token.get("symbol", "???")
    mint     = token.get("mint",   "")
    creator  = token.get("traderPublicKey", "")
    sol_amt  = float(token.get("solAmount", 0) or 0)
    mcap_sol = float(token.get("marketCapSol", 0) or 0)
    v_sol    = float(token.get("vSolInBondingCurve", VIRT_OFFSET) or VIRT_OFFSET)
    init_buy = float(token.get("initialBuy", 0) or 0)

    mcap_usd = mcap_sol * sol_usd
    real_sol = max(0.0, v_sol - VIRT_OFFSET)
    progress = min(100.0, real_sol / GRAD_SOL * 100)
    bar      = _bar(progress)

    desc     = meta.get("description", "")
    twitter  = meta.get("twitter",  "")
    telegram = meta.get("telegram", "")
    website  = meta.get("website",  "")

    if len(desc) > 140:
        desc = desc[:137] + "..."

    creator_short = (
        f"`{creator[:5]}...{creator[-4:]}`" if len(creator) > 9 else f"`{creator}`"
    )

    lines = [f"🆕 *NEW ON PUMP.FUN — ${symbol}*", f"*{name}*", ""]
    if desc:
        lines += [f"_{desc}_", ""]

    lines += [
        f"💰 MCap: `${mcap_usd:,.0f}` ({mcap_sol:.1f}◎)",
        f"📊 Bonding: `{bar}` {progress:.1f}%",
    ]
    if sol_amt > 0:
        lines.append(f"🛒 Dev buy: `{sol_amt:.2f}◎`  ({init_buy/1e6:.1f}M tokens)")

    if heat and not heat.get("disqualified"):
        label = priority_label(heat["total"])
        lines.append(f"🌡️ Heat: *{heat['total']}/100* — {label}")
    elif heat and heat.get("disqualified"):
        lines.append(f"🌡️ Heat: ❌ DQ — {heat['disqualified']}")

    social = []
    if twitter:
        social.append(f"[Twitter]({twitter})")
    if telegram:
        social.append(f"[Telegram]({telegram})")
    if website:
        social.append(f"[Web]({website})")
    if social:
        lines.append("🔗 " + "  ·  ".join(social))

    lines += ["", f"👤 Dev: {creator_short}", f"`{mint}`"]
    return "\n".join(lines)


def notification_kb(mint: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Buy 0.1◎", callback_data=f"pf:buy:0.1:{mint}"),
            InlineKeyboardButton("🟢 Buy 0.5◎", callback_data=f"pf:buy:0.5:{mint}"),
            InlineKeyboardButton("🟢 Buy 1◎",   callback_data=f"pf:buy:1.0:{mint}"),
        ],
        [
            InlineKeyboardButton("🤖 Analyze",    callback_data=f"quick:analyze:{mint}"),
            InlineKeyboardButton("🔔 Alert",      callback_data=f"quick:alert:{mint}"),
            InlineKeyboardButton("⚡ Quick Trade", callback_data=f"qt:{mint}"),
        ],
        [
            InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}"),
            InlineKeyboardButton("🪙 pump.fun",    url=f"https://pump.fun/{mint}"),
            InlineKeyboardButton("🔍 RugCheck",    url=f"https://rugcheck.xyz/tokens/{mint}"),
        ],
        [
            InlineKeyboardButton("🌐 Solscan", url=f"https://solscan.io/token/{mint}"),
        ],
    ])


# ─── Handle a single new-token event ──────────────────────────────────────────

async def _handle_token(bot: Bot, token: dict):
    mint = token.get("mint", "")
    if not mint:
        return

    s = load_state()
    _prune_seen(s)
    if mint in s.get("seen", {}):
        return
    s.setdefault("seen", {})[mint] = time.time()
    save_state(s)

    subscribers = s.get("subscribers", {})
    active_subs = [
        int(uid)
        for uid, cfg in subscribers.items()
        if cfg.get("active")
    ]
    channel = get_pumplive_channel()

    if not active_subs and not channel:
        return

    loop    = asyncio.get_running_loop()
    sol_usd = await loop.run_in_executor(None, get_sol_price)
    uri     = token.get("uri", "")
    meta    = await loop.run_in_executor(None, fetch_uri_metadata, uri) if uri else {}
    rc      = await loop.run_in_executor(None, fetch_rugcheck, mint)

    # Record wallet activity if creator is a tracked wallet
    creator = (token.get("traderPublicKey") or "").strip()
    if creator and wallet_tracker.is_wallet_tracked(creator):
        sol_amount = float(token.get("solAmount", 0) or 0)
        buy_usd = sol_amount * sol_usd
        wallet_tracker.record_wallet_activity(creator, mint, buy_usd, time.time())
        # Phase 4: feed co-investment edge into cluster graph
        try:
            import wallet_cluster
            wallet_cluster.record_token_entries(mint, [{"wallet": creator, "ts": time.time()}])
        except Exception:
            pass

    heat = calculate_heat_score(_build_scanner_token(token, meta, sol_usd, dex="pumpfun"), rc)
    print(f"[NEW TOKEN] {token.get('symbol','?')} mint={mint} heat={heat['total'] if heat else 'n/a'} active_subs={len(active_subs)}", flush=True)
    text = format_notification(token, meta, sol_usd, heat)
    kb   = notification_kb(mint)

    for uid in active_subs:
        filters = {**DEFAULT_FILTERS, **subscribers[str(uid)].get("filters", {})}
        if not passes_filter(token, meta, filters):
            continue
        try:
            await bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode="Markdown",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        except Exception:
            pass

    # Post to pump live alert channel (URL-only keyboard — callbacks don't work in channels)
    if channel:
        channel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Chart",    url=f"https://dexscreener.com/solana/{mint}"),
             InlineKeyboardButton("🪙 Pump",     url=f"https://pump.fun/{mint}"),
             InlineKeyboardButton("🔫 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
        ])
        try:
            await bot.send_message(
                chat_id=channel, text=text,
                parse_mode="Markdown", reply_markup=channel_kb,
                disable_web_page_preview=True,
            )
        except Exception:
            pass


# ─── Graduation (100% bonding curve) subscriber state ─────────────────────────

DEFAULT_GRAD_FILTERS = {
    "min_mcap_sol":        0.0,
    "max_mcap_sol":        0.0,
    "min_dev_sol":         0.0,
    "max_dev_sol":         0.0,
    "require_social":      False,
    "require_description": False,
    "keywords":            [],
    "blocked_words":       [],
    "tracked_wallets":     [],
    "blocked_wallets":     [],
}


def get_grad_subscribers() -> dict:
    return load_state().get("grad_subscribers", {})


def is_grad_subscribed(uid: int) -> bool:
    subs = load_state().get("grad_subscribers", {})
    return subs.get(str(uid), {}).get("active", False)


def subscribe_grad(uid: int):
    s = load_state()
    key = str(uid)
    if key not in s.setdefault("grad_subscribers", {}):
        s["grad_subscribers"][key] = {"active": True, "filters": dict(DEFAULT_GRAD_FILTERS)}
    else:
        s["grad_subscribers"][key]["active"] = True
    save_state(s)


def unsubscribe_grad(uid: int):
    s = load_state()
    key = str(uid)
    if key in s.get("grad_subscribers", {}):
        s["grad_subscribers"][key]["active"] = False
    save_state(s)


def is_grad_autobuy(uid: int) -> bool:
    subs = load_state().get("grad_subscribers", {})
    return subs.get(str(uid), {}).get("auto_buy", False)


def set_grad_autobuy(uid: int, val: bool):
    s   = load_state()
    key = str(uid)
    s.setdefault("grad_subscribers", {}).setdefault(key, {"active": False, "filters": {}})
    s["grad_subscribers"][key]["auto_buy"] = val
    save_state(s)


def get_grad_filters(uid: int) -> dict:
    s    = load_state()
    user = s.get("grad_subscribers", {}).get(str(uid), {})
    return {**DEFAULT_GRAD_FILTERS, **user.get("filters", {})}


def set_grad_filters(uid: int, filters: dict):
    s   = load_state()
    key = str(uid)
    if key not in s.setdefault("grad_subscribers", {}):
        s["grad_subscribers"][key] = {"active": False, "filters": {}}
    s["grad_subscribers"][key]["filters"] = filters
    save_state(s)


def reset_grad_filters(uid: int):
    set_grad_filters(uid, dict(DEFAULT_GRAD_FILTERS))


def _prune_grad_seen(s: dict):
    cutoff = time.time() - DEDUP_TTL
    s["grad_seen"] = {m: t for m, t in s.get("grad_seen", {}).items() if t > cutoff}


# ─── Graduation filter logic ───────────────────────────────────────────────────

def passes_grad_filter(token: dict, meta: dict, filters: dict) -> bool:
    """Return True if this graduating token passes all of the user's active grad filters."""
    mcap_sol = float(token.get("marketCapSol", 0) or 0)
    dev_sol  = float(token.get("solAmount",    0) or 0)
    creator  = (token.get("traderPublicKey") or "").strip()
    name     = (token.get("name",   "") or "").lower()
    symbol   = (token.get("symbol", "") or "").lower()
    desc     = (meta.get("description", "") or "").lower()
    text_all = f"{name} {symbol} {desc}"

    has_social = bool(meta.get("twitter") or meta.get("telegram"))
    has_desc   = bool(meta.get("description", "").strip())

    # Tracked wallets always bypass all filters
    tracked = [w.strip() for w in filters.get("tracked_wallets", []) if w.strip()]
    if creator and creator in tracked:
        return True

    # Blocked wallets — never notify
    blocked_wallets = [w.strip() for w in filters.get("blocked_wallets", []) if w.strip()]
    if creator and creator in blocked_wallets:
        return False

    # MCap range
    min_mcap = filters.get("min_mcap_sol") or 0
    max_mcap = filters.get("max_mcap_sol") or 0
    if min_mcap > 0 and mcap_sol < min_mcap:
        return False
    if max_mcap > 0 and mcap_sol > max_mcap:
        return False

    # Dev buy range
    min_dev = filters.get("min_dev_sol") or 0
    max_dev = filters.get("max_dev_sol") or 0
    if min_dev > 0 and dev_sol < min_dev:
        return False
    if max_dev > 0 and dev_sol > max_dev:
        return False

    # Require social
    if filters.get("require_social") and not has_social:
        return False

    # Require description
    if filters.get("require_description") and not has_desc:
        return False

    # Include keywords — must match at least one
    kws = [k.strip().lower() for k in filters.get("keywords", []) if k.strip()]
    if kws and not any(k in text_all for k in kws):
        return False

    # Blocked words — must match zero
    bws = [k.strip().lower() for k in filters.get("blocked_words", []) if k.strip()]
    if any(k in text_all for k in bws):
        return False

    return True


# ─── Graduation UI helpers ─────────────────────────────────────────────────────

def grad_filter_status_text(uid: int) -> str:
    active   = is_grad_subscribed(uid)
    ab_on    = is_grad_autobuy(uid)
    filters  = get_grad_filters(uid)

    status   = "🟢 *ON*" if active else "🔴 *OFF*"
    ab_status = "🟢 ON" if ab_on else "🔴 OFF"
    mcap_str = _sol_range_str(filters["min_mcap_sol"], filters["max_mcap_sol"])
    dev_str  = _sol_range_str(filters["min_dev_sol"],  filters["max_dev_sol"])
    soc_str  = "✅ Required" if filters["require_social"]      else "any"
    dsc_str  = "✅ Required" if filters["require_description"] else "any"
    kws      = filters["keywords"]
    bws      = filters["blocked_words"]
    tracked  = filters["tracked_wallets"]
    blocked  = filters["blocked_wallets"]

    grad_channel = get_pumpgrad_channel()
    ch_str       = f"`{grad_channel}`" if grad_channel else "not set"

    lines = [
        "🎓 *PUMP GRAD — FILTER SETTINGS*",
        "",
        f"Status: {status}  ·  Auto-Buy: {ab_status}",
        f"📣 Channel: {ch_str}",
        "━━━━━━━━━━━━━━━━━━━",
        f"💰 MCap at grad: `{mcap_str}`",
        f"🛒 Dev Buy: `{dev_str}`",
        f"🔗 Socials: {soc_str}",
        f"📝 Description: {dsc_str}",
    ]
    if kws:
        lines.append(f"🏷️ Keywords: `{', '.join(kws)}`")
    else:
        lines.append("🏷️ Keywords: any")
    if bws:
        lines.append(f"🚫 Blocked words: `{', '.join(bws)}`")
    else:
        lines.append("🚫 Blocked words: none")

    tw = len(tracked)
    bw = len(blocked)
    lines.append(f"👛 Tracked wallets: {tw}  ·  🚫 Blocked wallets: {bw}")
    lines += [
        "━━━━━━━━━━━━━━━━━━━",
        "_Fires when a pump.fun token reaches 100% bonding curve._",
    ]
    return "\n".join(lines)


def grad_filter_kb(uid: int) -> InlineKeyboardMarkup:
    active  = is_grad_subscribed(uid)
    ab_on   = is_grad_autobuy(uid)
    filters = get_grad_filters(uid)
    soc_lbl = "🔗 Social: ✅" if filters["require_social"]      else "🔗 Social: ANY"
    dsc_lbl = "📝 Desc: ✅"   if filters["require_description"] else "📝 Desc: ANY"
    on_lbl  = "🔴 Turn OFF"   if active                         else "🟢 Turn ON"
    ab_lbl  = "🤖 Auto-Buy: 🟢 ON" if ab_on                    else "🤖 Auto-Buy: 🔴 OFF"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(on_lbl,           callback_data="pumpgrad:toggle"),
            InlineKeyboardButton("🔄 Reset",        callback_data="pumpgrad:reset"),
        ],
        [
            InlineKeyboardButton(ab_lbl,           callback_data="pumpgrad:toggle_grad_autobuy"),
        ],
        [
            InlineKeyboardButton("💰 MCap",         callback_data="pumpgrad:set_mcap"),
            InlineKeyboardButton("🛒 Dev Buy",      callback_data="pumpgrad:set_devbuy"),
        ],
        [
            InlineKeyboardButton(soc_lbl,           callback_data="pumpgrad:toggle_social"),
            InlineKeyboardButton(dsc_lbl,           callback_data="pumpgrad:toggle_desc"),
        ],
        [
            InlineKeyboardButton("🏷️ Keywords",    callback_data="pumpgrad:set_keywords"),
            InlineKeyboardButton("🚫 Block Words",  callback_data="pumpgrad:set_blocked"),
        ],
        [
            InlineKeyboardButton("👛 Track Wallet", callback_data="pumpgrad:set_tracked"),
            InlineKeyboardButton("🚫 Block Wallet", callback_data="pumpgrad:set_block_wallet"),
        ],
        [
            InlineKeyboardButton("📣 Alert Channel", callback_data="pumpgrad:channel_menu"),
        ],
        [
            InlineKeyboardButton("⬅️ Menu", callback_data="menu:main"),
        ],
    ])


# ─── Graduation notification formatting ───────────────────────────────────────

def format_grad_notification(token: dict, meta: dict, sol_usd: float, heat: dict | None = None) -> str:
    name     = token.get("name",   "Unknown")
    symbol   = token.get("symbol", "???")
    mint     = token.get("mint",   "")
    creator  = token.get("traderPublicKey", "")
    sol_amt  = float(token.get("solAmount", 0) or 0)
    mcap_sol = float(token.get("marketCapSol", 0) or 0)
    init_buy = float(token.get("initialBuy", 0) or 0)

    mcap_usd = mcap_sol * sol_usd

    desc     = meta.get("description", "")
    twitter  = meta.get("twitter",  "")
    telegram = meta.get("telegram", "")
    website  = meta.get("website",  "")

    if len(desc) > 140:
        desc = desc[:137] + "..."

    creator_short = (
        f"`{creator[:5]}...{creator[-4:]}`" if len(creator) > 9 else f"`{creator}`"
    )

    lines = [f"🎓 *GRADUATED TO RAYDIUM — ${symbol}*", f"*{name}*", ""]
    if desc:
        lines += [f"_{desc}_", ""]

    lines += [
        f"💰 MCap: `${mcap_usd:,.0f}` ({mcap_sol:.1f}◎)",
        f"📊 Bonding Curve: `{'█' * 10}` 100% ✅",
    ]
    if sol_amt > 0:
        lines.append(f"🛒 Dev buy: `{sol_amt:.2f}◎`  ({init_buy/1e6:.1f}M tokens)")

    if heat and not heat.get("disqualified"):
        label = priority_label(heat["total"])
        lines.append(f"🌡️ Heat: *{heat['total']}/100* — {label}")
    elif heat and heat.get("disqualified"):
        lines.append(f"🌡️ Heat: ❌ DQ — {heat['disqualified']}")

    social = []
    if twitter:
        social.append(f"[Twitter]({twitter})")
    if telegram:
        social.append(f"[Telegram]({telegram})")
    if website:
        social.append(f"[Web]({website})")
    if social:
        lines.append("🔗 " + "  ·  ".join(social))

    lines += ["", f"👤 Dev: {creator_short}", f"`{mint}`"]
    return "\n".join(lines)


def grad_notification_kb(mint: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Buy 0.1◎", callback_data=f"pf:buy:0.1:{mint}"),
            InlineKeyboardButton("🟢 Buy 0.5◎", callback_data=f"pf:buy:0.5:{mint}"),
            InlineKeyboardButton("🟢 Buy 1◎",   callback_data=f"pf:buy:1.0:{mint}"),
        ],
        [
            InlineKeyboardButton("🤖 Analyze",    callback_data=f"quick:analyze:{mint}"),
            InlineKeyboardButton("🔔 Alert",      callback_data=f"quick:alert:{mint}"),
            InlineKeyboardButton("⚡ Quick Trade", callback_data=f"qt:{mint}"),
        ],
        [
            InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}"),
            InlineKeyboardButton("🪙 pump.fun",    url=f"https://pump.fun/{mint}"),
            InlineKeyboardButton("🔍 RugCheck",    url=f"https://rugcheck.xyz/tokens/{mint}"),
        ],
    ])


# ─── Handle a single graduation event ─────────────────────────────────────────

async def _handle_grad_token(bot: Bot, token: dict):
    mint = token.get("mint", "")
    if not mint:
        return

    s = load_state()
    _prune_grad_seen(s)
    if mint in s.get("grad_seen", {}):
        return
    s.setdefault("grad_seen", {})[mint] = time.time()
    save_state(s)

    grad_subs = s.get("grad_subscribers", {})
    active_subs = [
        int(uid)
        for uid, cfg in grad_subs.items()
        if cfg.get("active")
    ]
    grad_channel = get_pumpgrad_channel()

    if not active_subs and not grad_channel:
        return

    loop    = asyncio.get_running_loop()
    sol_usd = await loop.run_in_executor(None, get_sol_price)
    uri     = token.get("uri", "")
    meta    = await loop.run_in_executor(None, fetch_uri_metadata, uri) if uri else {}
    rc      = await loop.run_in_executor(None, fetch_rugcheck, mint)

    # Record wallet activity if creator is a tracked wallet
    creator = (token.get("traderPublicKey") or "").strip()
    if creator and wallet_tracker.is_wallet_tracked(creator):
        sol_amount = float(token.get("solAmount", 0) or 0)
        buy_usd = sol_amount * sol_usd
        wallet_tracker.record_wallet_activity(creator, mint, buy_usd, time.time())

    # If WS event is missing name/symbol, fill from DexScreener (15s wait for indexing)
    if not token.get("name") or not token.get("symbol"):
        await asyncio.sleep(15)
        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=8
            )
            pairs = r.json().get("pairs") or []
            if pairs:
                base = pairs[0].get("baseToken", {})
                token = dict(token)
                token.setdefault("name",   base.get("name", "Unknown"))
                token.setdefault("symbol", base.get("symbol", "???"))
                # Fill MCap / price from DexScreener
                if not token.get("marketCapSol"):
                    mcap_usd = float(pairs[0].get("marketCap") or pairs[0].get("fdv") or 0)
                    if mcap_usd and sol_usd:
                        token["marketCapSol"] = mcap_usd / sol_usd
        except Exception:
            pass

    # Record wallet activity if creator is a tracked wallet
    creator = (token.get("traderPublicKey") or "").strip()
    if creator and wallet_tracker.is_wallet_tracked(creator):
        sol_amount = float(token.get("solAmount", 0) or 0)
        buy_usd = sol_amount * sol_usd
        wallet_tracker.record_wallet_activity(creator, mint, buy_usd, time.time())
        # Phase 4: feed co-investment edge into cluster graph
        try:
            import wallet_cluster
            wallet_cluster.record_token_entries(mint, [{"wallet": creator, "ts": time.time()}])
        except Exception:
            pass

    heat = calculate_heat_score(_build_scanner_token(token, meta, sol_usd, dex="raydium"), rc)
    print(f"[GRAD WS] {token.get('symbol','?')} mint={mint} heat={heat['total'] if heat else 'n/a'} active_subs={len(active_subs)}", flush=True)
    text = format_grad_notification(token, meta, sol_usd, heat)
    kb   = grad_notification_kb(mint)

    for uid in active_subs:
        filters = {**DEFAULT_GRAD_FILTERS, **grad_subs[str(uid)].get("filters", {})}
        if not passes_grad_filter(token, meta, filters):
            continue
        try:
            await bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode="Markdown",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        except Exception:
            pass
        # Auto-buy on graduation if enabled
        if grad_subs[str(uid)].get("auto_buy") and _grad_autobuy_fn:
            result = {
                "mint":      mint,
                "symbol":    token.get("symbol", "?"),
                "name":      token.get("name", "?"),
                "total":     heat["total"] if heat else 80,
                "mcap":      float(token.get("marketCapSol", 0)) * sol_usd,
                "price_usd": 0.0,
            }
            try:
                await _grad_autobuy_fn(bot, uid, result)
            except Exception:
                pass

    # Post to pump grad alert channel (URL-only keyboard — callbacks don't work in channels)
    if grad_channel:
        channel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Chart",    url=f"https://dexscreener.com/solana/{mint}"),
             InlineKeyboardButton("🪙 Pump",     url=f"https://pump.fun/{mint}"),
             InlineKeyboardButton("🔫 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
        ])
        try:
            await bot.send_message(
                chat_id=grad_channel, text=text,
                parse_mode="Markdown", reply_markup=channel_kb,
                disable_web_page_preview=True,
            )
        except Exception:
            pass


# ─── Graduation polling (pump.fun API) ────────────────────────────────────────

def _fetch_pumpfun_graduated() -> list[dict]:
    """Return recently graduated pump.fun tokens.

    Strategy: sort by created_timestamp DESC (same as new-token alerts) so the
    most recently *created* completed tokens come first.  last_trade_timestamp
    is updated every time someone trades on Raydium, causing old grads to
    appear at the top — created_timestamp is immutable and reliable.
    """
    cutoff_ms = (time.time() - GRAD_MAX_AGE_H * 3600) * 1000
    results = []
    try:
        r = requests.get(
            f"{PUMPFUN_API}/coins",
            params={"offset": "0", "limit": "50", "sort": "created_timestamp",
                    "order": "DESC", "includeNsfw": "false", "complete": "true"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        r.raise_for_status()
        coins = r.json() if isinstance(r.json(), list) else []
        for c in coins:
            if not c.get("complete"):
                continue
            created_ts = c.get("created_timestamp") or 0
            if created_ts >= cutoff_ms:
                results.append(c)
    except Exception:
        pass
    # Ensure newest-created first
    results.sort(key=lambda c: c.get("created_timestamp", 0), reverse=True)
    return results


async def _handle_grad_from_pumpfun(bot: Bot, coin: dict):
    """Send pumpgrad DM notifications for a graduated token from pump.fun API data."""
    mint = coin.get("mint", "")
    if not mint:
        return

    s = load_state()
    _prune_grad_seen(s)
    if mint in s.get("grad_seen", {}):
        return
    s.setdefault("grad_seen", {})[mint] = time.time()
    save_state(s)

    grad_subs = s.get("grad_subscribers", {})
    active_subs = [
        int(uid)
        for uid, cfg in grad_subs.items()
        if cfg.get("active")
    ]
    grad_channel = get_pumpgrad_channel()

    if not active_subs and not grad_channel:
        return

    loop    = asyncio.get_running_loop()
    sol_usd = await loop.run_in_executor(None, get_sol_price)
    rc      = await loop.run_in_executor(None, fetch_rugcheck, mint)

    # v3 API: market_cap is in USD (bonding curve MCap at graduation ≈ $69K)
    mcap_usd = float(coin.get("usd_market_cap") or coin.get("market_cap") or 0)
    mcap_sol = (mcap_usd / sol_usd) if sol_usd > 0 else 0

    token = {
        "mint":            mint,
        "name":            coin.get("name")        or "Unknown",
        "symbol":          coin.get("symbol")      or "???",
        "marketCapSol":    mcap_sol,
        # v3 API doesn't have sol_amount / initial_buy for completed tokens
        "solAmount":       float(coin.get("sol_amount") or coin.get("real_sol_reserves") or 0),
        "initialBuy":      float(coin.get("initial_buy") or 0),
        "traderPublicKey": coin.get("creator")     or "",
        "uri":             coin.get("metadata_uri") or "",
    }
    meta = {
        "description": (coin.get("description") or "").strip(),
        "twitter":     coin.get("twitter")  or "",
        "telegram":    coin.get("telegram") or "",
        "website":     coin.get("website")  or "",
    }

    # Record wallet activity if creator is a tracked wallet
    creator = token.get("traderPublicKey", "")
    if creator and wallet_tracker.is_wallet_tracked(creator):
        sol_amount = float(token.get("solAmount", 0) or 0)
        buy_usd = sol_amount * sol_usd
        wallet_tracker.record_wallet_activity(creator, mint, buy_usd, time.time())
        # Phase 4: feed co-investment edge into cluster graph
        try:
            import wallet_cluster
            wallet_cluster.record_token_entries(mint, [{"wallet": creator, "ts": time.time()}])
        except Exception:
            pass

    heat = calculate_heat_score(_build_scanner_token(token, meta, sol_usd, dex="raydium"), rc)
    print(f"[GRAD REST] {token.get('symbol','?')} mint={mint} heat={heat['total'] if heat else 'n/a'} active_subs={len(active_subs)}", flush=True)
    text = format_grad_notification(token, meta, sol_usd, heat)
    kb   = grad_notification_kb(mint)

    for uid in active_subs:
        filters = {**DEFAULT_GRAD_FILTERS, **grad_subs[str(uid)].get("filters", {})}
        if not passes_grad_filter(token, meta, filters):
            continue
        try:
            await bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode="Markdown",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        except Exception:
            pass
        # Auto-buy on graduation if enabled
        if grad_subs[str(uid)].get("auto_buy") and _grad_autobuy_fn:
            result = {
                "mint":      mint,
                "symbol":    token.get("symbol", "?"),
                "name":      token.get("name", "?"),
                "total":     heat["total"] if heat else 80,
                "mcap":      mcap_usd,
                "price_usd": 0.0,
            }
            try:
                await _grad_autobuy_fn(bot, uid, result)
            except Exception:
                pass

    # Post to pump grad alert channel (URL-only keyboard — callbacks don't work in channels)
    if grad_channel:
        channel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Chart",    url=f"https://dexscreener.com/solana/{mint}"),
             InlineKeyboardButton("🪙 Pump",     url=f"https://pump.fun/{mint}"),
             InlineKeyboardButton("🔫 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
        ])
        try:
            await bot.send_message(
                chat_id=grad_channel, text=text,
                parse_mode="Markdown", reply_markup=channel_kb,
                disable_web_page_preview=True,
            )
        except Exception:
            pass


async def run_gradwatch(bot: Bot):
    """Poll pump.fun API as a fallback for graduation alerts.

    Primary path is still WebSocket migrations, but this catches misses.
    To avoid historical spam, the first poll only seeds `grad_seen` and does
    not notify. Subsequent polls only notify for newly seen mints.
    """
    seeded = False
    while True:
        try:
            s = load_state()
            has_subs = any(cfg.get("active") for cfg in s.get("grad_subscribers", {}).values())
            has_channel = bool(s.get("pumpgrad_channel"))
            if has_subs or has_channel:
                loop = asyncio.get_running_loop()
                coins = await loop.run_in_executor(None, _fetch_pumpfun_graduated)
                if not seeded:
                    s2 = load_state()
                    _prune_grad_seen(s2)
                    now_ts = time.time()
                    for coin in coins:
                        mint = coin.get("mint", "")
                        if mint:
                            s2.setdefault("grad_seen", {})[mint] = now_ts
                    save_state(s2)
                    seeded = True
                    print(f"[GRAD WATCH] seeded {len(coins)} mints", flush=True)
                else:
                    for coin in coins:
                        asyncio.create_task(_handle_grad_from_pumpfun(bot, coin))
        except Exception as e:
            print(f"[GRAD WATCH] error: {e}", flush=True)
        await asyncio.sleep(GRADWATCH_SECS)


# ─── Persistent WebSocket listener ────────────────────────────────────────────

async def run_pumpfeed(bot: Bot):
    """Long-running coroutine. Auto-reconnects on error."""
    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=20,
                open_timeout=15,
            ) as ws:
                # Subscribe to new token launches
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                # Also subscribe to migration/graduation events
                await ws.send(json.dumps({"method": "subscribeMigrations"}))

                async for raw in ws:
                    data = json.loads(raw)
                    if "message" in data and "txType" not in data:
                        continue
                    tx_type = data.get("txType") or data.get("type") or ""
                    if tx_type == "create":
                        s = load_state()
                        has_subs    = any(cfg.get("active") for cfg in s.get("subscribers", {}).values())
                        has_channel = bool(s.get("pumplive_channel"))
                        print(f"[PUMPFEED] new token {data.get('mint','?')} subs={has_subs} ch={has_channel}", flush=True)
                        if has_subs or has_channel:
                            asyncio.create_task(_handle_token(bot, data))
                    elif tx_type in ("complete", "migration", "migrate"):
                        s = load_state()
                        has_subs    = any(cfg.get("active") for cfg in s.get("grad_subscribers", {}).values())
                        has_channel = bool(s.get("pumpgrad_channel"))
                        print(f"[PUMPFEED] graduation {data.get('mint','?')} subs={has_subs} ch={has_channel}", flush=True)
                        if has_subs or has_channel:
                            asyncio.create_task(_handle_grad_token(bot, data))

        except Exception as e:
            print(f"[PUMPFEED] WS error: {e}", flush=True)
            await asyncio.sleep(5)
