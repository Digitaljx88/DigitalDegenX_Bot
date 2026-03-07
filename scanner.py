"""
Heat Score Scanner — per ALERTS.md + AGENTS.md.
Scores fresh Solana tokens on a normalized 0-100 scale and alerts at 70+.
"""
from __future__ import annotations

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
MCAP_MIN             = 10_000
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
    """Return the alert channel ID/username, or None if not set."""
    return load_state().get("alert_channel")


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
    if dev_dq:
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
    base_total = mom_pts + liq_pts + wal_pts + wall_pts + twit_pts + narr_pts + migr_pts + dev_pts + hold_pts + age_pts
    raw_total = max(0.0, float(base_total + clus_pts + bund_pts + pred_pts + intel_wallet_boost + intel_narrative_boost))
    total = max(0, min(100, int(round((raw_total / 120.0) * 100))))

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
        f"🌡️ Heat Score: *{r['total']}/100*\n"
        f"{arch_line}"
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
        f"{signal_lines}\n"
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

    tokens = fetch_new_tokens()

    for token in tokens:
        mint = token.get("mint", "")
        if not mint:
            continue
        if has_seen_token(mint):
            continue

        # Quick pre-filter: skip if mcap out of range
        mcap = token.get("mcap", 0)
        if not (MCAP_MIN <= mcap <= MCAP_MAX):
            continue

        # Fetch RugCheck and score
        rc     = fetch_rugcheck(mint)
        result = calculate_heat_score(token, rc)

        # Intelligence tracking: update narrative stats + auto-track wallets
        score = result["total"]
        try:
            intelligence_tracker.process_scored_token(token, rc, score)
        except Exception:
            pass

        # Log every scored token
        narr_reason = result["breakdown"]["narrative"][1]
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

        mark_seen_token(mint)

        # Watchlist (50-69)
        if 50 <= score < 70:
            add_to_watchlist(mint, {
                "name": result["name"], "symbol": result["symbol"],
                "score": score, "mcap": mcap, "mint": mint,
                "ts": time.time(),
            })

        # Alert threshold — check per-user min score, respect global cooldown
        # Find which users this token qualifies for
        eligible_uids = [uid for uid in chat_ids if score >= get_user_min_score(uid)]

        should_alert = (eligible_uids or alert_channel) and cooldown_ok(mint)

        if should_alert:
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
            for uid in eligible_uids:
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
            if alert_channel:
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
