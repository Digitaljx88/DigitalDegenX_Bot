"""
Heat Score Scanner — per ALERTS.md + AGENTS.md.
Scores fresh Solana tokens on a normalized 0-100 scale and alerts at 70+.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import time
import requests
from datetime import datetime, timezone
import wallet_tracker
import wallet_fingerprint
import wallet_cluster
import launch_predictor
import intelligence_tracker
import birdeye
import geckoterminal
import heat_score_v2
import settings_manager
import config as _cfg
import heat_momentum

_rugcheck_executor = concurrent.futures.ThreadPoolExecutor(max_workers=12, thread_name_prefix="rugcheck")

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


DATA_DIR           = os.path.join(os.path.dirname(__file__), "data")
SCANNER_STATE_FILE = os.path.join(DATA_DIR, "scanner_state.json")
DAILY_LOG_FILE     = os.path.join(DATA_DIR, "scanner_log.json")
os.makedirs(DATA_DIR, exist_ok=True)

DEXSCREENER_SEARCH   = "https://api.dexscreener.com/latest/dex/search?q="
DEXSCREENER_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN    = "https://api.dexscreener.com/latest/dex/tokens/"
RUGCHECK_REPORT      = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

ALERT_COOLDOWN_SECS  = 3600   # one alert per token per hour max
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
        return {
            "scanning": True,
            "watchlist": {},
            "alerted": {},
            "scan_targets": [],
            "seen_tokens": {},
        }


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


def get_user_min_score(uid: int) -> int:
    """Per-user alert score threshold, defaults to 55 (range 40–100)."""
    s = load_state()
    return s.get("user_min_score", {}).get(str(uid), 55)


def set_user_min_score(uid: int, score: int):
    s = load_state()
    score = max(1, min(100, score))  # clamp to valid range
    s.setdefault("user_min_score", {})[str(uid)] = score
    save_state(s)


def get_alert_channel() -> str | None:
    """Return the alert channel ID/username, or None if not set.
    Priority: scanner_state alert_channel → main_alert_channel_id → launch_alert_channel_id."""
    ch = load_state().get("alert_channel")
    if ch:
        return ch
    try:
        gs_path = os.path.join(DATA_DIR, "global_settings.json")
        with open(gs_path) as f:
            gs = json.load(f)
        # Prefer the main alert channel set via channel settings
        main_ch = gs.get("main_alert_channel_id")
        if main_ch:
            print(f"[SCANNER] Using main alert channel: {main_ch}", flush=True)
            return main_ch
        # Last resort: launch channel
        launch_ch = gs.get("launch_alert_channel_id")
        if launch_ch:
            print(f"[SCANNER] Using launch channel as scout fallback: {launch_ch}", flush=True)
            return launch_ch
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[SCANNER] get_alert_channel fallback failed: {e}", flush=True)
    return None


def set_alert_channel(channel: str | None):
    s = load_state()
    if channel:
        s["alert_channel"] = channel
    else:
        s.pop("alert_channel", None)
    save_state(s)


def add_to_watchlist(mint: str, data: dict):
    s = load_state()
    s.setdefault("watchlist", {})[mint] = data
    save_state(s)


def has_seen_token(mint: str) -> bool:
    return mint in load_state().get("seen_tokens", {})


def mark_seen_token(mint: str):
    s = load_state()
    s.setdefault("seen_tokens", {})[mint] = time.time()
    save_state(s)


def get_todays_alerts() -> list:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [e for e in load_log() if e.get("date") == today and e.get("alerted")]


# ─── Data fetching ────────────────────────────────────────────────────────────

MAX_TOKEN_AGE_HOURS  = 4     # ignore tokens older than this

def _parse_pairs(pairs: list, tokens: dict):
    """Merge DexScreener pairs into the tokens dict. Skip old tokens."""
    cutoff_ms = (time.time() - MAX_TOKEN_AGE_HOURS * 3600) * 1000
    for p in pairs:
        if p.get("chainId") != "solana":
            continue
        mint = p.get("baseToken", {}).get("address", "")
        if not mint:
            continue
        # Skip tokens older than MAX_TOKEN_AGE_HOURS
        pair_created = p.get("pairCreatedAt", 0) or 0
        if pair_created and pair_created < cutoff_ms:
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
            "pair_created": pair_created,
        })


def fetch_new_tokens() -> list[dict]:
    """
    Fetch recent Solana tokens (last 24h only), sorted newest first.
    """
    tokens: dict[str, dict] = {}
    cutoff_ms = (time.time() - MAX_TOKEN_AGE_HOURS * 3600) * 1000

    # Source 1: Newest Solana pairs sorted by creation time (primary source)
    try:
        pairs = requests.get(
            "https://api.dexscreener.com/latest/dex/pairs/solana",
            timeout=10
        ).json().get("pairs") or []
        # Sort newest first, only keep tokens created in last 24h
        pairs = [p for p in pairs if (p.get("pairCreatedAt") or 0) >= cutoff_ms]
        pairs.sort(key=lambda p: p.get("pairCreatedAt", 0), reverse=True)
        _parse_pairs(pairs[:150], tokens)
    except Exception:
        pass

    # Source 2: DexScreener new token profiles (pump.fun with social data)
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

    # Source 3: DexScreener boosted tokens
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

    # Source 4: Direct lookup for profile/boost mints missing market data
    profile_mints = [m for m, v in tokens.items() if not v.get("name")]
    for mint in profile_mints[:25]:
        try:
            pairs = requests.get(DEXSCREENER_TOKEN + mint, timeout=10).json().get("pairs") or []
            _parse_pairs(pairs, tokens)
        except Exception:
            continue

    # Filter: must have name + mcap in range + created within 24h
    result = [
        v for v in tokens.values()
        if v.get("name")
        and MCAP_MIN <= v.get("mcap", 0) <= MCAP_MAX
        and (v.get("pair_created", 0) or 0) >= cutoff_ms
    ]

    # Sort newest first
    result.sort(key=lambda t: t.get("pair_created", 0), reverse=True)
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
        return 10, f"{holders} unique wallets (200-499)"
    if holders >= 100:
        return 5, f"{holders} unique wallets (100-199)"
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
    markets      = (rc.get("markets") or [])
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
        return 0, f"Dev holds {dev_pct:.1f}% (>50% - disqualified)", True
    if dev_pct == 0:
        return 10, "Dev holds 0% (sold or never held)", False
    if dev_pct < 10:
        return 5, f"Dev holds {dev_pct:.1f}% (<10%)", False
    if dev_pct <= 20:
        return 0, f"Dev holds {dev_pct:.1f}% (>10%)", False
    return 0, f"Dev holds {dev_pct:.1f}% (20-50%)", False


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
        return 10, f"Top 10 hold {top10_pct:.1f}% (<30% — healthy)", False
    if top10_pct < 50:
        return 5, f"Top 10 hold {top10_pct:.1f}% (30-50%)", False
    return 0, f"Top 10 hold {top10_pct:.1f}% (>50% - disqualified)", True


def score_age(token: dict) -> tuple[int, str]:
    """5 pts — token age sweet spot."""
    pair_created = token.get("pair_created", 0) or 0
    if not pair_created:
        return 2, "Age unknown"
    now_ms   = time.time() * 1000
    age_mins = (now_ms - pair_created) / 60_000
    age_hrs  = age_mins / 60

    if age_mins < 30:
        return 2, f"{age_mins:.0f} mins old (very early)"
    if age_hrs <= 4:
        return 5, f"{age_mins:.0f} mins old (fresh)"
    return 0, f"{age_hrs:.0f} hrs old (too old)"


def score_liquidity_strength(token: dict, rc: dict) -> tuple[int, str]:
    """10 pts — liquidity strength via dev buy + bonding curve reserves."""
    dev_sol = float(token.get("solAmount", 0) or 0)
    reserves_sol = max(0.0, float(token.get("vSolInBondingCurve", 30) or 30) - 30.0)
    total_liquidity = dev_sol + reserves_sol

    if dev_sol >= 50 and reserves_sol >= 50:
        return 10, f"Strong launch (Dev: {dev_sol:.1f}◎ + Reserves: {reserves_sol:.1f}◎)"
    if dev_sol >= 20 or reserves_sol >= 30:
        return 7, f"Healthy liquidity (Dev: {dev_sol:.1f}◎, Res: {reserves_sol:.1f}◎)"
    if dev_sol >= 5:
        return 3, f"Minimal launch ({dev_sol:.2f}◎ dev buy)"
    return 0, f"Very weak liquidity ({total_liquidity:.2f}◎)"


def score_price_trajectory(token: dict) -> tuple[int, str, bool]:
    """
    Detects pump-and-dump / dying tokens using price change data.
    Returns (pts, reason, disqualify).

    DQ conditions (hard skip — already dumped):
      - h1 price change <= -60%  → clearly post-pump death spiral
      - h24 >= +300% AND h1 <= -40% → pumped yesterday, cratering now

    Penalty points (deducted from base score):
      - h1 change -30% to -60%  → heavy sell pressure, -10pts
      - h1 change -15% to -30%  → declining momentum, -5pts

    Bonus points (still rising):
      - h1 change > +20% and h24 < +500%  → actively pumping, +5pts
      - h1 change > +5%                   → healthy uptrend, +2pts
    """
    h1  = float(token.get("price_h1",  0) or 0)
    h24 = float(token.get("price_h24", 0) or 0)

    # Hard disqualification: token has already peaked and is dumping
    if h1 <= -60:
        return 0, f"Post-pump dump: price -{ abs(h1):.0f}% in 1h — SKIP", True
    if h24 >= 300 and h1 <= -40:
        return 0, f"Pump-and-dump: +{h24:.0f}% h24 / {h1:.0f}% h1 — SKIP", True

    # Penalty: declining price momentum
    if h1 <= -30:
        return -10, f"Heavy sell pressure: {h1:.0f}% in 1h", False
    if h1 <= -15:
        return -5, f"Declining: {h1:.0f}% in 1h", False

    # Neutral – slight decay but not disqualifying
    if h1 < -5:
        return 0, f"Slight decline: {h1:.0f}% in 1h", False

    # Bonus: actively moving up
    if h1 > 20 and h24 < 500:
        return 5, f"Pumping now: +{h1:.0f}% in 1h", False
    if h1 > 5:
        return 2, f"Uptrend: +{h1:.0f}% in 1h", False

    return 0, f"Flat price: {h1:+.0f}% h1 / {h24:+.0f}% h24", False


def score_volume_momentum(token: dict) -> tuple[int, str]:
    """20 pts — volume momentum acceleration (m1 → m5 → h1 progression)."""
    m1 = float(token.get("volume_m1", 0) or 0)
    m5 = float(token.get("volume_m5", 0) or 0)
    h1 = float(token.get("volume_h1", 0) or 0)

    if not h1:
        return 0, "No volume data"

    # Calculate escalation
    if m1 > 0 and m5 > 0:
        m1_annualized = m1 * 60  # 1m extrapolated to 1h
        m1_to_m5_ratio = m1_annualized / m5 if m5 > 0 else 0
        
        if m1_to_m5_ratio >= 4:
            return 15, f"Extreme 1m spike ({m1_to_m5_ratio:.1f}x 5m average)"

    if m5 > 0:
        m5_to_h1_ratio = m5 / h1 if h1 > 0 else 0
        
        if m5_to_h1_ratio >= 3:
            return 12, f"3x+ 5m momentum ({m5_to_h1_ratio:.1f}x hourly)"
        if m5_to_h1_ratio >= 2:
            return 8, f"2x 5m momentum ({m5_to_h1_ratio:.1f}x hourly)"

    if h1 > 100_000:
        return 5, f"Strong 1h volume (${h1:,.0f})"
    if h1 > 50_000:
        return 2, f"Moderate 1h volume (${h1:,.0f})"
    return 0, f"Flat volume (${h1:,.0f} h1)"


def score_watched_wallet_entry(token_mint: str) -> tuple[int, str]:
    """15 pts — detect tracked wallet cluster entry signals."""
    try:
        entries = wallet_tracker.detect_wallet_entry(token_mint, time_window_secs=60)
        
        if not entries:
            return 0, "No tracked wallet activity"
        
        # Count unique wallets
        unique_wallets = len(set(e["wallet"] for e in entries))
        
        # Score based on cluster intensity
        if unique_wallets >= 3:
            wallet_str = ", ".join([e["name"] for e in entries[:3]])
            return 15, f"Cluster signal: {unique_wallets} smart wallets ({wallet_str})"
        
        if unique_wallets == 2:
            wallet_str = ", ".join([e["name"] for e in entries])
            return 10, f"Multi-wallet: {wallet_str} entering"
        
        if unique_wallets == 1:
            entry = entries[0]
            age_secs = time.time() - entry["entry_ts"]
            if age_secs <= 30:
                return 5, f"Smart wallet early entry ({entry['name']}, {age_secs:.0f}s ago)"
            return 3, f"Smart wallet active ({entry['name']})"
        
        return 0, "Tracked wallets detected but weak signal"
    
    except Exception as e:
        # Wallet tracker not initialized or error
        return 0, "Wallet tracking unavailable"


def score_cluster_boost(token_mint: str) -> tuple[int, str]:
    """Phase 4 cluster boost (+0/+5/+10/+15) — reward co-investing smart wallet clusters."""
    try:
        entries = wallet_tracker.detect_wallet_entry(token_mint, time_window_secs=120)
        if not entries:
            return 0, "No cluster signal"

        # Build cluster entries for scoring
        cluster_entries = [
            {"wallet": e["wallet"], "ts": e["entry_ts"], "reputation": e.get("reputation", 50)}
            for e in entries
        ]

        # Persist new co-investment edges
        wallet_cluster.record_token_entries(token_mint, cluster_entries)

        result = wallet_cluster.score_cluster_strength(token_mint, cluster_entries)
        boost  = result.get("boost", 0)
        reason = result.get("reason", "No cluster pattern")
        return boost, reason
    except Exception:
        return 0, "Cluster scoring unavailable"


def score_bundle_risk(token_mint: str, rc: dict) -> tuple[int, str]:
    """0 pts (penalty only) — detect coordinated wallet clusters (bundles).
    Returns a negative modifier: 0 = clean, up to -15 = heavy bundle.
    """
    try:
        # Get early buyers from rugcheck top holders as proxy
        early_buyers = [h["address"] for h in (rc.get("topHolders") or [])[:8] if h.get("address")]
        if not early_buyers:
            return 0, "No holder data for bundle check"
        result = wallet_fingerprint.score_bundle_risk(token_mint, early_buyers)
        risk = result.get("bundle_risk", 0)
        if risk >= 8:
            return -15, f"🚨 {result.get('reason', 'Heavy bundle detected')}"
        if risk >= 5:
            return -8, f"⚠️ {result.get('reason', 'Bundle cluster found')}"
        if risk >= 3:
            return -3, f"🟡 {result.get('reason', 'Mild bundle pattern')}"
        return 0, "No bundle pattern detected"
    except Exception:
        return 0, "Bundle check unavailable"


def score_buy_sell_pressure(token_mint: str) -> tuple[int, str]:
    """0-10 pts — buy/sell directional pressure from recent trades (Birdeye).
    
    Scores:
    - 10pts: 70%+ buys (strong momentum)
    - 5pts: 60-70% buys (moderate momentum)
    - 0pts: 40-60% buys (neutral)
    - silent: <40% buys (sell pressure — no penalty, just no points)
    """
    api_key = getattr(_cfg, "BIRDEYE_API_KEY", "")
    if not api_key:
        return 0, "Birdeye API not configured"
    
    try:
        pressure = birdeye.get_buy_sell_pressure(token_mint, api_key)
        if pressure.get("error"):
            return 0, f"Birdeye unavailable ({pressure['error']})"
        
        score = pressure.get("pressure_score", 0)
        direction = pressure.get("direction", "NEUTRAL")
        buy_ratio = pressure.get("buy_ratio", 0)
        
        reason = (
            f"{direction} pressure ({buy_ratio*100:.0f}% buys, "
            f"{pressure['buy_count']}B/{pressure['sell_count']}S)"
        )
        
        return score, reason
    except Exception as e:
        return 0, f"Pressure score error: {str(e)[:40]}"


def score_volume_trend(token_mint: str) -> tuple[int, str]:
    """0-5 pts — volume trend acceleration (GeckoTerminal).
    
    Scores:
    - 5pts: Explosive volume increase (3x+ over 4h baseline)
    - 3pts: Strong volume increase (2x+ over 4h baseline)
    - 1pt: Mild volume increase
    - 0pts: Neutral or declining volume
    """
    try:
        trend = geckoterminal.get_volume_trend(token_mint)
        if trend.get("error"):
            return 0, f"GeckoTerminal unavailable ({trend['error']})"
        
        intensity = trend.get("trend_intensity", 0)
        trend_type = trend.get("trend", "STEADY")
        score = trend.get("trend_score", 0)
        
        vol_1h = trend.get("vol_1h", 0)
        vol_4h = trend.get("vol_4h", 0)
        
        reason = (
            f"{trend_type} volume trend (1h: ${vol_1h:,.0f}, "
            f"4h avg: ${vol_4h:,.0f}, intensity: {intensity:+d})"
        )
        
        return max(0, score), reason
    except Exception as e:
        return 0, f"Volume trend error: {str(e)[:40]}"


def calculate_heat_score(token: dict, rc: dict) -> dict:
    """
    Run the extended scoring model but return a normalized 0-100 heat score.
    Returns full score breakdown dict. Returns disqualified flag if instantly DQ.
    """
    # New scoring order: momentum first, then liquidity, then wallet, then rest
    mom_pts,  mom_reason  = score_volume_momentum(token)
    liq_pts,  liq_reason  = score_liquidity_strength(token, rc)
    mid_mint = token.get("mint", "")
    wal_pts,  wal_reason  = score_watched_wallet_entry(mid_mint)
    clus_pts, clus_reason = score_cluster_boost(mid_mint)
    pres_pts, pres_reason = score_buy_sell_pressure(mid_mint)  # ← Birdeye pressure
    trend_pts, trend_reason = score_volume_trend(mid_mint)  # ← GeckoTerminal trend
    traj_pts, traj_reason, traj_dq = score_price_trajectory(token)  # ← pump-dump guard

    wall_pts, wall_reason = score_wallets(rc)
    twit_pts, twit_reason = score_twitter(token)
    narr_pts, narr_reason = score_narrative(token)
    migr_pts, migr_reason = score_migration(token, rc)
    dev_pts,  dev_reason, dev_dq  = score_dev_wallet(rc)
    hold_pts, hold_reason, hold_dq = score_top_holders(rc)
    age_pts,  age_reason  = score_age(token)
    bund_pts, bund_reason = score_bundle_risk(mid_mint, rc)

    # Phase 5: run prediction AFTER all base scores are computed so breakdown is available
    _pre_breakdown = {
        "momentum":   (mom_pts,  mom_reason),
        "liquidity":  (liq_pts,  liq_reason),
        "wallet_rep": (wal_pts,  wal_reason),
        "cluster":    (clus_pts, clus_reason),
        "pressure":   (pres_pts, pres_reason),  # ← NEW: Birdeye buy/sell
        "trend":      (trend_pts, trend_reason),  # ← NEW: GeckoTerminal OHLCV
        "wallets":    (wall_pts, wall_reason),
        "twitter":    (twit_pts, twit_reason),
        "narrative":  (narr_pts, narr_reason),
        "migration":  (migr_pts, migr_reason),
        "dev":        (dev_pts,  dev_reason),
        "holders":    (hold_pts, hold_reason),
        "age":        (age_pts,  age_reason),
        "bundle":     (bund_pts, bund_reason),
    }
    try:
        pred = launch_predictor.predict_launch(token, rc, _pre_breakdown)
        pred_pts    = pred.get("boost", 0)
        pred_reason = pred.get("reason", "No archetype match")
        pred_arch   = pred.get("archetype", "NONE")
        pred_conf   = pred.get("confidence", 0)
    except Exception:
        pred_pts, pred_reason, pred_arch, pred_conf = 0, "Prediction unavailable", "NONE", 0
    disqualified = None
    if traj_dq:
        disqualified = traj_reason
    elif dev_dq:
        disqualified = dev_reason
    elif hold_dq:
        disqualified = hold_reason
    elif rc.get("rugged"):
        disqualified = "Flagged as rugged by RugCheck"

    # Intelligence boosts from tracked wallet reputation + trending narratives
    try:
        top_holder_addrs = [
            h.get("address") or h.get("owner", "")
            for h in (rc.get("topHolders") or [])
        ]
        intel_wallet_boost = intelligence_tracker.get_wallet_score_boost(top_holder_addrs)

        _narr_text = f"{token.get('name','')} {token.get('symbol','')} {token.get('description','')}"
        _matched_narratives = intelligence_tracker.detect_narratives(
            token.get("name", ""), token.get("symbol", ""), token.get("description", "")
        )
        intel_narrative_boost = intelligence_tracker.get_narrative_score_boost(_matched_narratives)
    except Exception:
        intel_wallet_boost    = 0.0
        intel_narrative_boost = 0.0

    # Convert the richer internal score into the documented 0-100 scale.
    # traj_pts can be negative (penalty) or positive (bonus) — applied directly
    base_total = mom_pts + liq_pts + pres_pts + trend_pts + wal_pts + wall_pts + twit_pts + narr_pts + migr_pts + dev_pts + hold_pts + age_pts + traj_pts
    raw_total = max(0.0, float(base_total + clus_pts + bund_pts + pred_pts + intel_wallet_boost + intel_narrative_boost))
    total = max(0, min(100, int(round((raw_total / 125.0) * 100))))

    # Risk level (adjusted for 120pt scale)
    red_flags = []
    if rc.get("risks"):
        red_flags = [r["name"] for r in rc["risks"] if r.get("level") in ("danger", "warn")]
    if token.get("liquidity", 0) < 5000:
        red_flags.append("Very low liquidity")

    if total >= 85 and not red_flags:
        risk = "LOW"
    elif total >= 70 or (total >= 55 and red_flags):
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
        "raw_total":     raw_total,
        "total":         total,
        "disqualified":  disqualified,
        "risk":          risk,
        "red_flags":     red_flags,
        "breakdown": {
            "momentum":   (mom_pts,  mom_reason),
            "liquidity":  (liq_pts,  liq_reason),
            "wallet_rep": (wal_pts,  wal_reason),
            "cluster":    (clus_pts, clus_reason),
            "pressure":   (pres_pts, pres_reason),
            "trend":      (trend_pts, trend_reason),
            "trajectory": (traj_pts, traj_reason),
            "wallets":    (wall_pts, wall_reason),
            "twitter":    (twit_pts, twit_reason),
            "narrative":  (narr_pts, narr_reason),
            "migration":  (migr_pts, migr_reason),
            "dev":        (dev_pts,  dev_reason),
            "holders":    (hold_pts, hold_reason),
            "age":        (age_pts,  age_reason),
            "bundle":     (bund_pts, bund_reason),
            "predict":    (pred_pts, pred_reason),
            "intel_wallet":    (int(intel_wallet_boost),    f"Smart wallet boost ({intel_wallet_boost:.1f}pts)"),
            "intel_narrative": (int(intel_narrative_boost), f"Trending narrative boost ({intel_narrative_boost:.1f}pts)"),
        },
        "archetype":     pred_arch,
        "archetype_conf": pred_conf,
    }


def calculate_heat_score_with_settings(token: dict, rc: dict, user_id: int = None) -> dict:
    """
    Calculate heat score using v2 engine with user settings.
    Falls back to v1 if user_id not provided.
    
    Args:
        token: Token data dict
        rc: RugCheck result dict
        user_id: Telegram user ID (optional, uses defaults if not provided)
    
    Returns:
        dict compatible with old format for backward compatibility
    """
    # Get user settings
    if user_id:
        user_cfg = settings_manager.get_user_settings(user_id)
    else:
        user_cfg = settings_manager._get_defaults().copy()
    
    # Map scanner field names to heat_score_v2 expected names
    t = dict(token)  # shallow copy to avoid mutating original
    if "volume_m5" in t and "volume_5m_usd" not in t:
        t["volume_5m_usd"] = t["volume_m5"]
    if "volume_h1" in t and "volume_1h_usd" not in t:
        t["volume_1h_usd"] = t["volume_h1"]
    if "volume_h24" in t and "volume_24h_usd" not in t:
        t["volume_24h_usd"] = t["volume_h24"]
    if "pair_created" in t and "created_timestamp" not in t:
        # pair_created is in milliseconds, created_timestamp in seconds
        pc = t["pair_created"]
        t["created_timestamp"] = pc / 1000 if pc > 1e12 else pc
    if "liquidity" in t and "liquidity_usd" not in t:
        t["liquidity_usd"] = t["liquidity"]
    if "twitter_url" in t and "twitter" not in t:
        t["twitter"] = t["twitter_url"]

    # Calculate v2 score
    result_v2 = heat_score_v2.calculate_heat_score_v2(t, rc, user_cfg)
    
    # Determine alert tier based on user's thresholds
    score = result_v2["score"]
    
    if score >= user_cfg.get("alert_ultra_hot_threshold", 90):
        priority = "🔴 ULTRA_HOT"
    elif score >= user_cfg.get("alert_hot_threshold", 80):
        priority = "🟠 HOT"
    elif score >= user_cfg.get("alert_warm_threshold", 70):
        priority = "🟡 WARM"
    else:
        priority = "⚪ SCOUTED"
    
    # Build breakdown dict in old format for backward compatibility
    factors = result_v2["factors"]
    breakdown = {
        "momentum": (factors["momentum"]["pts"], factors["momentum"]["reason"]),
        "liquidity": (factors["liquidity"]["pts"], factors["liquidity"]["reason"]),
        "risk_safety": (factors["risk_safety"]["pts"], factors["risk_safety"]["reason"]),
        "social_narrative": (factors["social_narrative"]["pts"], factors["social_narrative"]["reason"]),
        "wallets": (factors["wallets"]["pts"], factors["wallets"]["reason"]),
        "migration": (factors["migration"]["pts"], factors["migration"]["reason"]),
        "directional_bias": (factors["directional_bias"]["pts"], factors["directional_bias"]["reason"]),
        "volume_trend": (factors["volume_trend"]["pts"], factors["volume_trend"]["reason"]),
    }
    
    # Return in old format for compatibility
    return {
        "mint": token.get("mint", ""),
        "name": token.get("name", ""),
        "symbol": token.get("symbol", ""),
        "mcap": token.get("mcap", 0),
        "price_usd": token.get("price_usd", 0),
        "volume_h1": token.get("volume_h1", 0),
        "total_holders": rc.get("totalHolders", 0),
        "pair_created": token.get("pair_created", 0),
        "dex": token.get("dex", ""),
        "raw_total": result_v2["raw_score"],
        "total": score,
        "disqualified": result_v2["disqualified"],
        "risk": result_v2["risk_level"],
        "red_flags": [],  # Could extract from risk details
        "breakdown": breakdown,
        "archetype": "SCOUT_V2",
        "archetype_conf": 100,
        "v2_result": result_v2,  # Store full v2 result for detailed display
    }


def priority_label(score: int) -> str:
    """Priority label for heat score (0-100 scale)."""
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
        return f"  • {key.replace('_', ' ').title()}: *{pts}pts* — {reason}"

    flags_str  = ", ".join(r["red_flags"]) if r["red_flags"] else "None"
    risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(r["risk"], "⚪")
    price_usd  = r.get("price_usd", 0)
    price_str  = f"${price_usd:.8f}" if price_usd and price_usd < 0.01 else (f"${price_usd:.4f}" if price_usd else "N/A")

    # Archetype tag line
    arch      = r.get("archetype", "NONE")
    arch_conf = r.get("archetype_conf", 0)
    arch_line = ""
    if arch and arch != "NONE" and arch_conf >= 50:
        from launch_predictor import ARCHETYPES as _ARCH
        arch_emoji = _ARCH.get(arch, {}).get("emoji", "📖")
        arch_desc  = _ARCH.get(arch, {}).get("description", arch)
        arch_line  = f"📖 Playbook: *{arch_emoji} {arch_desc}* ({arch_conf}%)\n"

    # Build signal lines dynamically from breakdown
    signal_lines = "\n".join(line(cat) for cat in bd.keys())

    name_e   = _esc(r['name'])
    symbol_e = _esc(r['symbol'])
    return (
        f"{label}\n"
        f"🚨 *HOT TOKEN ALERT* 🚨\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 *{name_e}* (${symbol_e})\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🌡️ Heat Score: *{r['total']}/100*"
        + (f"  eff *{r['effective_score']}*" if r.get('effective_score', r['total']) != r['total'] else "")
        + "\n"
        + (f"📈 Momentum: *{r['velocity_label']}*\n" if r.get('velocity_label') else "")
        + f"{arch_line}"
        + f"💵 Price: `{price_str}`\n"
        f"🏦 MCap: `${r['mcap']:,.0f}`\n"
        f"📊 Vol (1h): `${r['volume_h1']:,.0f}`\n"
        f"👛 Holders: `{r['total_holders']}`\n"
        f"⏰ Age: `{age_str(r['pair_created'])}`\n"
        f"🏪 DEX: `{r.get('dex', 'N/A')}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📋 *Mint:*\n`{mint}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📈 *Signals:*\n"
        f"{signal_lines}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{risk_emoji} Risk: *{r['risk']}*  |  ⚠️ Flags: {flags_str}"
    )


def format_scouted_alert(r: dict) -> str:
    """Lighter notification for tokens entering the scouted watchlist (50–69)."""
    mint      = r["mint"]
    price_usd = r.get("price_usd", 0)
    price_str = f"${price_usd:.8f}" if price_usd and price_usd < 0.01 else (f"${price_usd:.4f}" if price_usd else "N/A")
    name_e    = _esc(r["name"])
    symbol_e  = _esc(r["symbol"])
    bd        = r.get("breakdown", {})
    flags_str = ", ".join(r.get("red_flags", [])) or "None"

    best_signals = []
    for cat, val in bd.items():
        pts, reason = val if isinstance(val, (list, tuple)) else (val, "")
        if pts > 0:
            best_signals.append(f"  • {cat.replace('_', ' ').title()}: *{pts}pts* — {reason}")

    signal_block = "\n".join(best_signals[:4]) if best_signals else "  • No strong signals yet"

    return (
        f"👀 *SCOUTED — ${symbol_e}*\n"
        f"*{name_e}*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🌡️ Heat Score: *{r['total']}/100* — On Radar (50–69)\n"
        f"💵 Price: `{price_str}`\n"
        f"🏦 MCap: `${r['mcap']:,.0f}`\n"
        f"⏰ Age: `{age_str(r['pair_created'])}`\n"
        f"🏪 DEX: `{r.get('dex', 'N/A')}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📈 *Top Signals:*\n{signal_block}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Flags: {flags_str}\n"
        f"📋 `{mint}`"
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
        f"*{_esc(r['name'])}* (${_esc(r['symbol'])})",
        f"💵 Price: `{price_str}`",
        f"🏦 MCap: `${r['mcap']:,.0f}` | 📊 Vol 1h: `${r['volume_h1']:,.0f}`",
        f"👛 Holders: `{r['total_holders']}` | ⏰ Age: `{age_str(r['pair_created'])}`",
        f"━━━━━━━━━━━━━━━━━━━",
        f"📋 *Mint:*\n`{mint}`",
        f"━━━━━━━━━━━━━━━━━━━",
        f"*Breakdown:*",
    ]
    for cat, (pts, reason) in bd.items():
        lines.append(f"`{cat:<12}` *{pts:>2}pts*  {reason}")

    if r["disqualified"]:
        lines.append(f"\n❌ *DISQUALIFIED:* {r['disqualified']}")
    if r["red_flags"]:
        lines.append(f"⚠️ Flags: {', '.join(r['red_flags'])}")
    return "\n".join(lines)


# ─── Main scan loop (called by bot's job queue) ───────────────────────────────

async def run_scan(bot, chat_ids: list[int], on_alert=None):
    """
    Fetch, score, and alert. Called every N seconds by bot job queue.
    chat_ids: list of user IDs to send alerts to.
    on_alert: optional async callback(bot, result) called when a token fires an alert.
    """
    alert_channel = get_alert_channel()
    if not chat_ids and not alert_channel:
        return

    # Global default thresholds used for channel alerts (no per-user settings for channels)
    try:
        _ch_cfg = settings_manager.get_user_settings(0)  # uid=0 gives pure defaults
    except Exception:
        _ch_cfg = {}
    CH_SCOUTED_THR = _ch_cfg.get("alert_scouted_threshold", 35)
    CH_HOT_THR     = _ch_cfg.get("alert_hot_threshold", 70)

    t_fetch_start = time.time()
    tokens = fetch_new_tokens()
    print(f"[SCANNER] Fetched {len(tokens)} tokens after filters", flush=True)
    if not tokens:
        print(f"[SCANNER] No tokens passed fetch filters (mcap {MCAP_MIN}-{MCAP_MAX}, age <{MAX_TOKEN_AGE_HOURS}h)", flush=True)

    # ── Pre-filter before expensive IO ───────────────────────────────────────
    to_process = []
    for token in tokens:
        mint = token.get("mint", "")
        if not mint or has_seen_token(mint):
            continue
        mcap = token.get("mcap", 0)
        if not (MCAP_MIN <= mcap <= MCAP_MAX):
            continue
        to_process.append(token)

    # ── Parallel RugCheck fetch (all tokens simultaneously) ───────────────────
    t_rugcheck_start = time.time()
    loop = asyncio.get_event_loop()
    rugcheck_futures = [
        loop.run_in_executor(_rugcheck_executor, fetch_rugcheck, t["mint"])
        for t in to_process
    ]
    rugcheck_results = await asyncio.gather(*rugcheck_futures, return_exceptions=True)
    t_rugcheck_end = time.time()
    if to_process:
        print(
            f"[SCANNER] RugCheck x{len(to_process)} parallel: {t_rugcheck_end - t_rugcheck_start:.2f}s "
            f"(was ~{len(to_process) * 10:.0f}s sequential)",
            flush=True,
        )

    for token, rc_raw in zip(to_process, rugcheck_results):
        mint = token["mint"]
        rc   = rc_raw if isinstance(rc_raw, dict) else {}

        # Score using v2 engine
        result = calculate_heat_score_with_settings(token, rc)
        score  = result["total"]
        sym    = result.get("symbol", mint[:8])
        dq     = result.get("disqualified")

        # ── Heat velocity tracking ────────────────────────────────────────────
        heat_momentum.record(mint, score)
        velocity, velocity_label = heat_momentum.get_velocity(mint)
        velocity_boost = heat_momentum.velocity_score_boost(mint)
        effective_score = min(100, score + velocity_boost)

        # Attach velocity info to result for alert formatting
        result["velocity"]       = velocity
        result["velocity_label"] = velocity_label
        result["effective_score"] = effective_score

        print(
            f"[SCANNER] {sym} ({mint[:8]}) → score={score} "
            f"eff={effective_score} vel={velocity:+.1f}pt/min dq={dq}",
            flush=True,
        )

        try:
            intelligence_tracker.process_scored_token(token, rc, score)
        except Exception:
            pass

        # Log every scored token
        narr_breakdown = result["breakdown"].get("social_narrative") or result["breakdown"].get("narrative") or ("", "")
        narr_reason = narr_breakdown[1] if isinstance(narr_breakdown, (list, tuple)) else narr_breakdown.get("reason", "")
        matched_narrative = next((n for n in NARRATIVES if n in narr_reason), "Other")
        append_log({
            "date":      datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "timestamp": time.time(),
            "mint":      mint,
            "name":      result["name"],
            "symbol":    result["symbol"],
            "score":     result["total"],
            "mcap":      mcap,
            "narrative": matched_narrative,
            "archetype": result.get("archetype", "NONE"),
            "alerted":   False,
            "dq":        result.get("disqualified"),
        })

        if result["disqualified"]:
            continue

        # Now determine which users get alerts using their v2 settings
        # For each user, get their alert thresholds and check if token qualifies
        user_scores = {}  # cache v2 scores per user
        user_tiers = {}   # cache tier classification per user
        any_user_qualifies = False
        
        for uid in chat_ids:
            # Get user's v2 settings
            try:
                user_cfg = settings_manager.get_user_settings(uid)
                scouted_threshold = user_cfg.get("alert_scouted_threshold", 50)
                warm_threshold = user_cfg.get("alert_warm_threshold", 70)
                hot_threshold = user_cfg.get("alert_hot_threshold", 80)
                ultra_hot_threshold = user_cfg.get("alert_ultra_hot_threshold", 90)
            except Exception:
                # Fallback if settings_manager has issues
                scouted_threshold, warm_threshold, hot_threshold, ultra_hot_threshold = 50, 70, 80, 90
            
            user_scores[uid] = (scouted_threshold, warm_threshold, hot_threshold, ultra_hot_threshold)
            
            # Classify into tier — use effective_score (includes velocity boost)
            eff = result.get("effective_score", score)
            if eff >= ultra_hot_threshold:
                user_tiers[uid] = "ULTRA_HOT"
                any_user_qualifies = True
            elif eff >= hot_threshold:
                user_tiers[uid] = "HOT"
                any_user_qualifies = True
            elif eff >= warm_threshold:
                user_tiers[uid] = "WARM"
                any_user_qualifies = True
            elif eff >= scouted_threshold:
                user_tiers[uid] = "SCOUTED"
                any_user_qualifies = True
            else:
                user_tiers[uid] = None  # Below user's threshold

        eff = result.get("effective_score", score)
        # Only mark as seen if at least one user qualifies or score is high enough
        if any_user_qualifies or (alert_channel and eff >= CH_SCOUTED_THR):
            mark_seen_token(mint)

        # Separate scouted from hot alerts
        scouted_uids = [uid for uid, tier in user_tiers.items() if tier == "SCOUTED"]
        hot_uids = [uid for uid, tier in user_tiers.items() if tier in ("HOT", "WARM", "ULTRA_HOT")]

        # ─── Send SCOUTED alerts ───────────────────────────────────────────
        if scouted_uids or (alert_channel and score >= CH_SCOUTED_THR):
            add_to_watchlist(mint, {
                "name": result["name"], "symbol": result["symbol"],
                "score": score, "mcap": mcap, "mint": mint,
                "ts": time.time(),
            })
            
            if (scouted_uids or alert_channel) and cooldown_ok(mint):
                mark_alerted(mint)
                log = load_log()
                for e in reversed(log):
                    if e["mint"] == mint:
                        e["alerted"] = True
                        break
                with open(DAILY_LOG_FILE, "w") as f:
                    json.dump(log, f, indent=2)

                scout_msg = format_scouted_alert(result)[:4000]
                from telegram import InlineKeyboardMarkup, InlineKeyboardButton
                scout_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🟢 Buy",      callback_data=f"quick:buy:{mint}"),
                     InlineKeyboardButton("🤖 Analyze",  callback_data=f"quick:analyze:{mint}"),
                     InlineKeyboardButton("🔔 Alert",    callback_data=f"quick:alert:{mint}")],
                    [InlineKeyboardButton("📊 Chart",    url=f"https://dexscreener.com/solana/{mint}"),
                     InlineKeyboardButton("🔫 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
                     InlineKeyboardButton("🪙 Pump",     url=f"https://pump.fun/{mint}")],
                ])
                for uid in scouted_uids:
                    try:
                        await bot.send_message(
                            chat_id=uid, text=scout_msg,
                            parse_mode="Markdown", reply_markup=scout_kb,
                            disable_web_page_preview=True,
                        )
                    except Exception:
                        try:
                            await bot.send_message(
                                chat_id=uid, text=scout_msg,
                                reply_markup=scout_kb, disable_web_page_preview=True,
                            )
                        except Exception as e:
                            print(f"[SCANNER] scouted DM error uid={uid}: {e}", flush=True)
                if alert_channel and score >= CH_SCOUTED_THR:
                    channel_scout_kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("📊 Chart",    url=f"https://dexscreener.com/solana/{mint}"),
                         InlineKeyboardButton("🔫 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
                         InlineKeyboardButton("🪙 Pump",     url=f"https://pump.fun/{mint}")],
                    ])
                    try:
                        await bot.send_message(
                            chat_id=alert_channel, text=scout_msg,
                            parse_mode="Markdown", reply_markup=channel_scout_kb,
                            disable_web_page_preview=True,
                        )
                    except Exception as e:
                        print(f"[SCANNER] scouted channel error ch={alert_channel}: {e}", flush=True)
                if on_alert and score >= 50:
                    try:
                        await on_alert(bot, result)
                    except Exception:
                        pass

        # ─── Send HOT alerts (70+) ──────────────────────────────────────────
        should_alert_hot = (hot_uids or alert_channel) and cooldown_ok(mint)

        if should_alert_hot:
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
            for uid in hot_uids:
                try:
                    await bot.send_message(
                        chat_id=uid, text=msg,
                        parse_mode="Markdown", reply_markup=kb,
                        disable_web_page_preview=True
                    )
                except Exception:
                    try:
                        await bot.send_message(
                            chat_id=uid, text=msg,
                            reply_markup=kb, disable_web_page_preview=True
                        )
                    except Exception as e:
                        print(f"[SCANNER] DM send error uid={uid}: {e}", flush=True)

            # Post to alert channel (URL buttons only — callback buttons don't work in channels)
            if alert_channel and score >= CH_HOT_THR:
                channel_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📊 Chart",    url=f"https://dexscreener.com/solana/{mint}"),
                     InlineKeyboardButton("🔫 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
                     InlineKeyboardButton("🪙 Pump",     url=f"https://pump.fun/{mint}")],
                ])
                try:
                    await bot.send_message(
                        chat_id=alert_channel, text=msg,
                        parse_mode="Markdown", reply_markup=channel_kb,
                        disable_web_page_preview=True
                    )
                except Exception:
                    try:
                        await bot.send_message(
                            chat_id=alert_channel, text=msg,
                            reply_markup=channel_kb, disable_web_page_preview=True
                        )
                    except Exception as e:
                        print(f"[SCANNER] channel send error ch={alert_channel}: {e}", flush=True)

            # Trigger auto-buy callback
            if on_alert:
                try:
                    await on_alert(bot, result)
                except Exception:
                    pass

        # Trigger auto-buy independently for tokens that didn't fire a user alert
        # (autobuy has its own min_score threshold checked in execute_auto_buy)
        elif on_alert and not result.get("disqualified"):
            try:
                await on_alert(bot, result)
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
