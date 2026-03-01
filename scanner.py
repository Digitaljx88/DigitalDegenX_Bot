"""
Heat Score Scanner — per ALERTS.md + AGENTS.md
Scores new Solana tokens and fires Telegram alerts when score >= 70.
"""

import json
import os
import time
import requests
from datetime import datetime, timezone

DATA_DIR           = os.path.join(os.path.dirname(__file__), "data")
SCANNER_STATE_FILE = os.path.join(DATA_DIR, "scanner_state.json")
DAILY_LOG_FILE     = os.path.join(DATA_DIR, "scanner_log.json")
os.makedirs(DATA_DIR, exist_ok=True)

DEXSCREENER_SEARCH   = "https://api.dexscreener.com/latest/dex/search?q="
DEXSCREENER_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN    = "https://api.dexscreener.com/latest/dex/tokens/"
RUGCHECK_REPORT      = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

ALERT_COOLDOWN_SECS  = 3600   # one alert per token per hour
MCAP_MIN             = 1_000
MCAP_MAX             = 10_000_000

# ── Narrative keywords ────────────────────────────────────────────────────────
NARRATIVES = {
    "AI":       ["ai", "agent", "gpt", "robot", "artificial", "neural", "llm", "ml", "agi"],
    "Political":["trump", "maga", "biden", "elon", "doge", "political", "president", "vote",
                 "congress", "patriot", "freedom", "america"],
    "Animal":   ["dog", "cat", "pepe", "frog", "shib", "inu", "doge", "wolf", "bear", "bull",
                 "penguin", "monkey", "ape", "hamster", "fish", "bird"],
    "Gaming":   ["game", "play", "nft", "pixel", "arcade", "quest", "rpg", "guild", "warrior"],
    "RWA":      ["gold", "oil", "real", "estate", "asset", "commodity", "bond", "rwa"],
}

# ─── State helpers ────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(SCANNER_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"scanning": False, "watchlist": {}, "alerted": {}, "scan_targets": []}


def save_state(s: dict):
    with open(SCANNER_STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


def load_log() -> list:
    try:
        with open(DAILY_LOG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def append_log(entry: dict):
    log = load_log()
    log.append(entry)
    # Keep last 500 entries
    if len(log) > 500:
        log = log[-500:]
    with open(DAILY_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


def is_scanning() -> bool:
    return load_state().get("scanning", False)


def set_scanning(val: bool):
    s = load_state()
    s["scanning"] = val
    save_state(s)


def get_watchlist() -> dict:
    return load_state().get("watchlist", {})


def cooldown_ok(mint: str) -> bool:
    alerted = load_state().get("alerted", {})
    last    = alerted.get(mint, 0)
    return (time.time() - last) >= ALERT_COOLDOWN_SECS


def mark_alerted(mint: str):
    s = load_state()
    s.setdefault("alerted", {})[mint] = time.time()
    save_state(s)


def add_to_watchlist(mint: str, data: dict):
    s = load_state()
    s.setdefault("watchlist", {})[mint] = data
    save_state(s)


def get_todays_alerts() -> list:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [e for e in load_log() if e.get("date") == today and e.get("alerted")]


# ─── Data fetching ────────────────────────────────────────────────────────────

def _parse_pairs(pairs: list, tokens: dict):
    """Merge DexScreener pairs into the tokens dict."""
    for p in pairs:
        if p.get("chainId") != "solana":
            continue
        mint = p.get("baseToken", {}).get("address", "")
        if not mint:
            continue
        mcap = float(p.get("marketCap") or p.get("fdv") or 0)
        entry = tokens.setdefault(mint, {"mint": mint})
        entry.update({
            "name":         p.get("baseToken", {}).get("name", ""),
            "symbol":       p.get("baseToken", {}).get("symbol", ""),
            "mcap":         mcap,
            "price_usd":    float(p.get("priceUsd") or 0),
            "volume_h1":    float((p.get("volume") or {}).get("h1", 0)),
            "volume_h6":    float((p.get("volume") or {}).get("h6", 0)),
            "volume_h24":   float((p.get("volume") or {}).get("h24", 0)),
            "volume_m5":    float((p.get("volume") or {}).get("m5", 0)),
            "txns_m5_buys": int((p.get("txns") or {}).get("m5", {}).get("buys", 0)),
            "price_h24":    float((p.get("priceChange") or {}).get("h24", 0)),
            "price_h1":     float((p.get("priceChange") or {}).get("h1", 0)),
            "liquidity":    float((p.get("liquidity") or {}).get("usd", 0)),
            "dex":          p.get("dexId", ""),
            "pair_address": p.get("pairAddress", ""),
            "pair_created": p.get("pairCreatedAt", 0),
        })


def fetch_new_tokens() -> list[dict]:
    """
    Fetch candidate tokens from multiple DexScreener sources.
    Returns list of scored token dicts.
    """
    tokens: dict[str, dict] = {}

    # Source 1: DexScreener new token profiles (pump.fun with social data)
    try:
        profiles = requests.get(DEXSCREENER_PROFILES, timeout=10).json()
        if isinstance(profiles, list):
            sol_profiles = [p for p in profiles if p.get("chainId") == "solana"]
            for p in sol_profiles[:50]:
                mint = p.get("tokenAddress", "")
                if mint:
                    entry = tokens.setdefault(mint, {"mint": mint})
                    entry.update({
                        "twitter_url": next(
                            (l["url"] for l in p.get("links", []) if l.get("type") == "twitter"),
                            None
                        ),
                        "description": p.get("description", ""),
                        "has_icon":    bool(p.get("icon")),
                    })
    except Exception:
        pass

    # Source 2: DexScreener boosted tokens (paid promotions = community attention)
    try:
        boosts = requests.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=10).json()
        if isinstance(boosts, list):
            sol_boosts = [b for b in boosts if b.get("chainId") == "solana"]
            for b in sol_boosts[:20]:
                mint = b.get("tokenAddress", "")
                if mint:
                    tokens.setdefault(mint, {"mint": mint})
    except Exception:
        pass

    # Source 3: Newest Solana pairs sorted by creation time
    try:
        pairs = requests.get(
            "https://api.dexscreener.com/latest/dex/pairs/solana",
            timeout=10
        ).json().get("pairs") or []
        # Sort newest first, take top 100
        pairs.sort(key=lambda p: p.get("pairCreatedAt", 0), reverse=True)
        _parse_pairs(pairs[:100], tokens)
    except Exception:
        pass

    # Source 4: Multiple DexScreener searches to maximise pump.fun token coverage
    search_terms = ["pump", "sol meme", "solana meme", "new token", "pepe", "ai agent", "doge", "cat", "trump"]
    for term in search_terms:
        try:
            pairs = requests.get(DEXSCREENER_SEARCH + term, timeout=10).json().get("pairs") or []
            _parse_pairs(pairs, tokens)
        except Exception:
            continue

    # Source 5: Direct token lookup for mints discovered via profiles/boosts
    profile_mints = [m for m, v in tokens.items() if not v.get("name")]
    for mint in profile_mints[:20]:
        try:
            pairs = requests.get(DEXSCREENER_TOKEN + mint, timeout=10).json().get("pairs") or []
            _parse_pairs(pairs, tokens)
        except Exception:
            continue

    # Filter: must have name + mcap in range
    result = [
        v for v in tokens.values()
        if v.get("name") and MCAP_MIN <= v.get("mcap", 0) <= MCAP_MAX
    ]
    return result


def fetch_rugcheck(mint: str) -> dict:
    try:
        r = requests.get(RUGCHECK_REPORT.format(mint=mint), timeout=10)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


# ─── Heat Score engine ────────────────────────────────────────────────────────

def score_volume(token: dict) -> tuple[int, str]:
    """20 pts — detect volume spike."""
    m5  = token.get("volume_m5", 0)
    h1  = token.get("volume_h1", 0)
    if not h1:
        return 0, "No volume data"
    # Annualize m5 to hourly rate
    m5_projected = m5 * 12
    ratio = m5_projected / h1 if h1 else 0
    if ratio >= 3:
        return 20, f"3x+ volume spike ({ratio:.1f}x projected vs h1)"
    if ratio >= 2:
        return 10, f"2x volume spike ({ratio:.1f}x projected vs h1)"
    if h1 > 50_000:
        return 5, f"Strong h1 volume (${h1:,.0f})"
    return 0, f"Normal volume (${h1:,.0f} h1)"


def score_wallets(rc: dict) -> tuple[int, str]:
    """15 pts — unique wallet count from RugCheck."""
    holders = rc.get("totalHolders", 0) or 0
    if holders >= 500:
        return 15, f"{holders} unique wallets (500+)"
    if holders >= 200:
        return 10, f"{holders} unique wallets (200-500)"
    if holders >= 100:
        return 5, f"{holders} unique wallets (100-200)"
    return 0, f"Only {holders} wallets"


def score_twitter(token: dict) -> tuple[int, str]:
    """
    15 pts — Twitter/social activity.
    Without live Twitter API: score based on social link presence + description signals.
    """
    twitter = token.get("twitter_url")
    desc    = (token.get("description") or "").lower()
    social_words = ["twitter", "telegram", "community", "trending", "viral",
                    "100x", "gem", "bullish", "mooning"]
    social_hits  = sum(1 for w in social_words if w in desc)

    if twitter and social_hits >= 3:
        return 10, "Twitter linked + strong social signals in description"
    if twitter:
        return 5, "Twitter linked"
    if social_hits >= 2:
        return 3, "Social signals in description (no Twitter)"
    return 0, "No Twitter link found"


def score_narrative(token: dict) -> tuple[int, str]:
    """15 pts — match current hot narratives."""
    name = (token.get("name") or "").lower()
    sym  = (token.get("symbol") or "").lower()
    desc = (token.get("description") or "").lower()
    text = f"{name} {sym} {desc}"

    matched = []
    for narrative, keywords in NARRATIVES.items():
        hits = sum(1 for k in keywords if k in text)
        if hits >= 2:
            matched.append((narrative, hits))
        elif hits == 1:
            matched.append((narrative, 0.5))

    matched.sort(key=lambda x: -x[1])
    if not matched:
        return 0, "No narrative match"
    top = matched[0]
    if top[1] >= 2:
        return 15, f"Strong {top[0]} narrative match"
    return 8, f"Partial {top[0]} narrative match"


def score_migration(token: dict, rc: dict) -> tuple[int, str]:
    """10 pts — Raydium migration status."""
    dex          = (token.get("dex") or "").lower()
    markets      = (rc.get("markets") or "")
    pair_created = token.get("pair_created", 0) or 0
    now_ms       = time.time() * 1000
    age_mins     = (now_ms - pair_created) / 60_000 if pair_created else 9999

    if "raydium" in dex:
        if age_mins <= 30:
            return 10, f"Migrated to Raydium {age_mins:.0f} mins ago"
        return 7, f"On Raydium (migrated {age_mins:.0f} mins ago)"
    if "pump" in str(markets).lower() or "pump" in dex:
        return 0, "Still on pump.fun"
    return 3, f"On {dex or 'unknown DEX'}"


def score_dev_wallet(rc: dict) -> tuple[int, str, bool]:
    """10 pts — dev wallet sell check. Returns (pts, reason, disqualify)."""
    creator  = rc.get("creator", "")
    holders  = rc.get("topHolders") or []
    dev_pct  = 0.0

    for h in holders:
        if h.get("owner") == creator or h.get("address") == creator:
            dev_pct = float(h.get("pct", 0))
            break

    if dev_pct > 50:
        return 0, f"Dev holds {dev_pct:.1f}% — DISQUALIFIED", True
    if dev_pct == 0:
        return 10, "Dev holds 0% (sold or never held)", False
    if dev_pct < 10:
        return 5, f"Dev holds {dev_pct:.1f}% (<10%)", False
    if dev_pct < 20:
        return 0, f"Dev holds {dev_pct:.1f}% (>10%)", False
    return 0, f"Dev holds {dev_pct:.1f}% (>20%)", False


def score_top_holders(rc: dict) -> tuple[int, str, bool]:
    """10 pts — top holder concentration. Returns (pts, reason, disqualify)."""
    holders = rc.get("topHolders") or []
    if not holders:
        return 5, "No holder data available", False

    top10_pct  = sum(float(h.get("pct", 0)) for h in holders[:10])
    max_single = max((float(h.get("pct", 0)) for h in holders), default=0)

    if max_single > 20:
        return 0, f"Single wallet holds {max_single:.1f}% — DISQUALIFIED", True
    if top10_pct < 30:
        return 10, f"Top 10 hold {top10_pct:.1f}% (<30% — healthy)"  , False
    if top10_pct < 50:
        return 5, f"Top 10 hold {top10_pct:.1f}% (30-50%)", False
    return 0, f"Top 10 hold {top10_pct:.1f}% (>50% — concentrated)", False


def score_age(token: dict) -> tuple[int, str]:
    """5 pts — token age sweet spot."""
    pair_created = token.get("pair_created", 0) or 0
    if not pair_created:
        return 0, "Age unknown"
    now_ms   = time.time() * 1000
    age_mins = (now_ms - pair_created) / 60_000
    age_hrs  = age_mins / 60

    if age_mins < 30:
        return 2, f"{age_mins:.0f} mins old (too early)"
    if age_hrs <= 4:
        return 5, f"{age_mins:.0f} mins old (sweet spot)"
    if age_hrs <= 24:
        return 2, f"{age_hrs:.1f} hrs old (aging)"
    return 0, f"{age_hrs:.0f} hrs old (too old)"


def calculate_heat_score(token: dict, rc: dict) -> dict:
    """
    Run all 8 scoring categories. Returns full score breakdown dict.
    Returns None if instantly disqualified.
    """
    vol_pts,  vol_reason  = score_volume(token)
    wall_pts, wall_reason = score_wallets(rc)
    twit_pts, twit_reason = score_twitter(token)
    narr_pts, narr_reason = score_narrative(token)
    migr_pts, migr_reason = score_migration(token, rc)
    dev_pts,  dev_reason, dev_dq  = score_dev_wallet(rc)
    hold_pts, hold_reason, hold_dq = score_top_holders(rc)
    age_pts,  age_reason  = score_age(token)

    # Instant disqualifiers
    disqualified = None
    if dev_dq:
        disqualified = dev_reason
    elif hold_dq:
        disqualified = hold_reason
    elif rc.get("rugged"):
        disqualified = "Flagged as rugged by RugCheck"
    elif rc.get("mintAuthority"):
        disqualified = "Mint authority not renounced"

    total = vol_pts + wall_pts + twit_pts + narr_pts + migr_pts + dev_pts + hold_pts + age_pts

    # Risk level
    red_flags = []
    if rc.get("risks"):
        red_flags = [r["name"] for r in rc["risks"] if r.get("level") in ("danger", "warn")]
    if token.get("liquidity", 0) < 5000:
        red_flags.append("Very low liquidity")

    if total >= 85 or (total >= 70 and not red_flags):
        risk = "LOW"
    elif total >= 70 and red_flags:
        risk = "MEDIUM"
    else:
        risk = "HIGH"

    return {
        "mint":          token.get("mint", ""),
        "name":          token.get("name", ""),
        "symbol":        token.get("symbol", ""),
        "mcap":          token.get("mcap", 0),
        "price_usd":     token.get("price_usd", 0),
        "volume_h1":     token.get("volume_h1", 0),
        "total_holders": rc.get("totalHolders", 0),
        "pair_created":  token.get("pair_created", 0),
        "dex":           token.get("dex", ""),
        "total":         total,
        "disqualified":  disqualified,
        "risk":          risk,
        "red_flags":     red_flags,
        "breakdown": {
            "volume":    (vol_pts,  vol_reason),
            "wallets":   (wall_pts, wall_reason),
            "twitter":   (twit_pts, twit_reason),
            "narrative": (narr_pts, narr_reason),
            "migration": (migr_pts, migr_reason),
            "dev":       (dev_pts,  dev_reason),
            "holders":   (hold_pts, hold_reason),
            "age":       (age_pts,  age_reason),
        },
    }


def priority_label(score: int) -> str:
    if score >= 90: return "🔴 ULTRA HOT"
    if score >= 80: return "🟠 HOT"
    if score >= 70: return "🟡 WARM"
    if score >= 50: return "⚪ WATCH"
    return "❌ SKIP"


def age_str(pair_created_ms: int) -> str:
    if not pair_created_ms:
        return "unknown"
    age_mins = (time.time() * 1000 - pair_created_ms) / 60_000
    if age_mins < 60:
        return f"{age_mins:.0f}m"
    return f"{age_mins/60:.1f}h"


def format_alert(r: dict) -> str:
    """Format the Telegram alert message per ALERTS.md spec."""
    bd    = r["breakdown"]
    mint  = r["mint"]
    label = priority_label(r["total"])

    def line(key):
        pts, reason = bd[key]
        return f"  • {key.title()}: *{pts}pts* — {reason}"

    flags_str  = ", ".join(r["red_flags"]) if r["red_flags"] else "None"
    risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(r["risk"], "⚪")
    price_usd  = r.get("price_usd", 0)
    price_str  = f"${price_usd:.8f}" if price_usd and price_usd < 0.01 else (f"${price_usd:.4f}" if price_usd else "N/A")

    return (
        f"{label}\n"
        f"🚨 *HOT TOKEN ALERT* 🚨\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{r['name']}* (${r['symbol']})\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🌡️ Heat Score: *{r['total']}/100*\n"
        f"💵 Price: `{price_str}`\n"
        f"🏦 MCap: `${r['mcap']:,.0f}`\n"
        f"📊 Vol (1h): `${r['volume_h1']:,.0f}`\n"
        f"👛 Holders: `{r['total_holders']}`\n"
        f"⏰ Age: `{age_str(r['pair_created'])}`\n"
        f"🏪 DEX: `{r.get('dex', 'N/A')}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📋 *Mint:*\n`{mint}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📈 *Signals:*\n"
        f"{line('volume')}\n"
        f"{line('wallets')}\n"
        f"{line('twitter')}\n"
        f"{line('narrative')}\n"
        f"{line('migration')}\n"
        f"{line('dev')}\n"
        f"{line('holders')}\n"
        f"{line('age')}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{risk_emoji} Risk: *{r['risk']}*  |  ⚠️ Flags: {flags_str}"
    )


def format_heat_score_card(r: dict) -> str:
    """Shorter card for manual /heatscore lookups."""
    bd        = r["breakdown"]
    label     = priority_label(r["total"])
    mint      = r["mint"]
    price_usd = r.get("price_usd", 0)
    price_str = f"${price_usd:.8f}" if price_usd and price_usd < 0.01 else (f"${price_usd:.4f}" if price_usd else "N/A")

    lines = [
        f"🌡️ *Heat Score: {r['total']}/100* — {label}\n",
        f"*{r['name']}* (${r['symbol']})",
        f"💵 Price: `{price_str}`",
        f"🏦 MCap: `${r['mcap']:,.0f}` | 📊 Vol 1h: `${r['volume_h1']:,.0f}`",
        f"👛 Holders: `{r['total_holders']}` | ⏰ Age: `{age_str(r['pair_created'])}`",
        f"━━━━━━━━━━━━━━━━━━━",
        f"📋 *Mint:*\n`{mint}`",
        f"━━━━━━━━━━━━━━━━━━━",
        f"*Breakdown:*",
    ]
    for cat, (pts, reason) in bd.items():
        lines.append(f"`{cat:<10}` *{pts:>2}pts*  {reason}")

    if r["disqualified"]:
        lines.append(f"\n❌ *DISQUALIFIED:* {r['disqualified']}")
    if r["red_flags"]:
        lines.append(f"⚠️ Flags: {', '.join(r['red_flags'])}")
    return "\n".join(lines)


# ─── Main scan loop (called by bot's job queue) ───────────────────────────────

async def run_scan(bot, chat_ids: list[int]):
    """
    Fetch, score, and alert. Called every N seconds by bot job queue.
    chat_ids: list of user IDs to send alerts to.
    Runs whenever scanning is ON *or* any channel feed is enabled.
    """
    import feed as fd
    cfg = fd.load_feed_config()
    feeds_active = cfg.get("launch_enabled") or cfg.get("migrate_enabled")

    scanning_on = is_scanning()
    if not scanning_on and not feeds_active:
        return

    tokens = fetch_new_tokens()
    state  = load_state()

    for token in tokens:
        mint = token.get("mint", "")
        if not mint:
            continue

        # Quick pre-filter: skip if mcap out of range
        mcap = token.get("mcap", 0)
        if not (MCAP_MIN <= mcap <= MCAP_MAX):
            continue

        # Fetch RugCheck for tokens with basic market data
        rc     = fetch_rugcheck(mint)
        result = calculate_heat_score(token, rc)

        # Log every scored token
        append_log({
            "date":      datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "timestamp": time.time(),
            "mint":      mint,
            "name":      result["name"],
            "symbol":    result["symbol"],
            "score":     result["total"],
            "alerted":   False,
            "dq":        result.get("disqualified"),
        })

        score = result["total"]

        # Instant disqualify
        if result["disqualified"]:
            continue

        # Watchlist (50-69)
        if 50 <= score < 70:
            add_to_watchlist(mint, {
                "name": result["name"], "symbol": result["symbol"],
                "score": score, "mcap": mcap, "mint": mint,
                "ts": time.time(),
            })
            continue

        # Always try launch feed (all scored tokens, feed applies its own filters)
        import feed as fd
        label = priority_label(score)
        token["total_holders"]      = rc.get("totalHolders", 0)
        token["matched_narrative"]  = result["breakdown"]["narrative"][1]
        await fd.maybe_post_launch(bot, token, score, label)

        # Migration feed: check if token just graduated to Raydium
        dex_id = token.get("dex", "")
        if "raydium" in dex_id.lower():
            await fd.maybe_post_migration(bot, token)

        # Alert threshold — only send DMs if user scanning is on
        if scanning_on and score >= 70 and cooldown_ok(mint):
            mark_alerted(mint)

            # Update log entry
            log = load_log()
            for e in reversed(log):
                if e["mint"] == mint:
                    e["alerted"] = True
                    break
            with open(DAILY_LOG_FILE, "w") as f:
                json.dump(log, f, indent=2)

            msg = format_alert(result)[:4000]   # Telegram 4096 char limit
            from telegram import InlineKeyboardMarkup, InlineKeyboardButton
            kb  = InlineKeyboardMarkup([
                [InlineKeyboardButton("🟢 Buy",      callback_data=f"quick:buy:{mint}"),
                 InlineKeyboardButton("🤖 Analyze",  callback_data=f"quick:analyze:{mint}"),
                 InlineKeyboardButton("🔔 Alert",    callback_data=f"quick:alert:{mint}")],
                [InlineKeyboardButton("📊 Chart",    url=f"https://dexscreener.com/solana/{mint}"),
                 InlineKeyboardButton("🔫 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
                 InlineKeyboardButton("🪙 Pump",     url=f"https://pump.fun/{mint}")],
            ])
            for uid in chat_ids:
                try:
                    await bot.send_message(
                        chat_id=uid, text=msg,
                        parse_mode="Markdown", reply_markup=kb,
                        disable_web_page_preview=True
                    )
                except Exception:
                    pass


async def score_single_token(mint_or_symbol: str) -> dict | None:
    """Score a single token by mint or symbol. Used for /heatscore command."""
    from bot import fetch_sol_pair  # imported here to avoid circular import
    pair = fetch_sol_pair(mint_or_symbol)
    if not pair:
        return None

    mint = pair["baseToken"]["address"]
    token = {
        "mint":         mint,
        "name":         pair.get("baseToken", {}).get("name", ""),
        "symbol":       pair.get("baseToken", {}).get("symbol", ""),
        "mcap":         float(pair.get("marketCap") or pair.get("fdv") or 0),
        "price_usd":    float(pair.get("priceUsd") or 0),
        "volume_h1":    float((pair.get("volume") or {}).get("h1", 0)),
        "volume_h6":    float((pair.get("volume") or {}).get("h6", 0)),
        "volume_h24":   float((pair.get("volume") or {}).get("h24", 0)),
        "volume_m5":    float((pair.get("volume") or {}).get("m5", 0)),
        "price_h1":     float((pair.get("priceChange") or {}).get("h1", 0)),
        "liquidity":    float((pair.get("liquidity") or {}).get("usd", 0)),
        "dex":          pair.get("dexId", ""),
        "pair_created": pair.get("pairCreatedAt", 0),
        "description":  "",
        "twitter_url":  None,
    }
    rc     = fetch_rugcheck(mint)
    result = calculate_heat_score(token, rc)
    return result
