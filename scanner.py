"""
Heat Score Scanner — per ALERTS.md + AGENTS.md.
Scores fresh Solana tokens on a normalized 0-100 scale and alerts at 70+.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import time
from collections import defaultdict
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
import db as _db

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


DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

DEXSCREENER_SEARCH   = "https://api.dexscreener.com/latest/dex/search?q="
DEXSCREENER_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN    = "https://api.dexscreener.com/latest/dex/tokens/"
RUGCHECK_REPORT      = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

MCAP_MIN             = 5_000       # global fetch floor — per-user filter applies on top
MCAP_MAX             = 50_000_000  # global fetch ceiling
LIQUIDITY_MIN_USD    = 1_500       # min pool liquidity for graduated DEX tokens (not pump.fun bonding curve)

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

# ─── State helpers (all backed by SQLite via db.py) ───────────────────────────

SEEN_TOKEN_TTL = _db.SEEN_TOKEN_TTL  # legacy compat constant; seen-token cache is session-scoped
_quality_history: dict[str, list[dict]] = defaultdict(list)
_narrative_alert_history: dict[str, list[float]] = defaultdict(list)
QUALITY_HISTORY_SECS = 1800
NARRATIVE_CLUSTER_WINDOW_SECS = 900
NARRATIVE_CLUSTER_LIMIT = 3
WEAK_DISCOVERY_SOURCES = {"dex_search", "dex_profiles", "dex_boosts"}
PRIMARY_DISCOVERY_SOURCES = {"pumpfun_newest", "pumpfun_hot", "dex_pairs", "dex_lookup"}
STRICT_AUTOBUY_MAX_AGE_MINS = 20


def append_log(entry: dict):
    _db.append_scan_log(entry)


def is_scanning() -> bool:
    return _db.is_scanning()


def set_scanning(val: bool):
    _db.set_scanning(val)


def get_watchlist() -> dict:
    return _db.get_watchlist()


def get_user_min_score(uid: int) -> int:
    """Per-user alert score threshold, defaults to 55 (range 1–100)."""
    return _db.get_user_min_score(uid)


def set_user_min_score(uid: int, score: int):
    _db.set_user_min_score(uid, score)


def get_alert_channel() -> str | None:
    """Return the alert channel ID/username, or None if not set.
    Priority: scanner_config alert_channel → main_alert_channel_id → launch_alert_channel_id."""
    ch = _db.get_alert_channel()
    if ch:
        return ch
    main_ch = _db.get_setting("main_alert_channel_id")
    if main_ch:
        print(f"[SCANNER] Using main alert channel: {main_ch}", flush=True)
        return main_ch
    launch_ch = _db.get_setting("launch_alert_channel_id")
    if launch_ch:
        print(f"[SCANNER] Using launch channel as scout fallback: {launch_ch}", flush=True)
        return launch_ch
    return None


def set_alert_channel(channel: str | None):
    _db.set_alert_channel(channel)


def add_to_watchlist(mint: str, data: dict):
    _db.add_to_watchlist(mint, data)


def has_seen_token(mint: str) -> bool:
    return _db.has_seen_token(mint)


def mark_seen_token(mint: str):
    _db.mark_seen_token(mint)


def get_todays_alerts() -> list:
    return _db.get_todays_alerts()


def clear_quality_state():
    _quality_history.clear()
    _narrative_alert_history.clear()


# ─── Data fetching ────────────────────────────────────────────────────────────

MAX_TOKEN_AGE_HOURS  = 4     # ignore tokens older than this

SOURCE_RANKS = {
    "pumpfun_newest": 100,
    "pumpfun_hot": 90,
    "dex_pairs": 70,
    "dex_lookup": 60,
    "dex_profiles": 40,
    "dex_boosts": 30,
    "dex_search": 20,
}


def _merge_token_entry(tokens: dict, mint: str, incoming: dict, *, source_name: str, source_rank: int):
    """
    Merge token data while preserving the freshest launch timestamp and the highest-trust source.
    Lower-signal sources should enrich a token, not overwrite stronger pump.fun/new-pair data.
    """
    entry = tokens.setdefault(mint, {"mint": mint, "_source_rank": source_rank, "_source_name": source_name})
    current_rank = entry.get("_source_rank", -1)

    incoming_created = incoming.get("pair_created") or 0
    existing_created = entry.get("pair_created") or 0
    if incoming_created and incoming_created > existing_created:
        entry["pair_created"] = incoming_created

    for key, value in incoming.items():
        if key == "pair_created":
            continue
        if value in (None, "", 0, 0.0, False):
            if key not in entry:
                entry[key] = value
            continue
        if current_rank <= source_rank or not entry.get(key):
            entry[key] = value

    if source_rank > current_rank:
        entry["_source_rank"] = source_rank
        entry["_source_name"] = source_name
    return entry


def _parse_pairs(pairs: list, tokens: dict, *, source_name: str = "dex_pairs",
                 source_rank: int = SOURCE_RANKS["dex_pairs"], require_existing: bool = False):
    """Merge DexScreener pairs into the tokens dict. Skip old tokens."""
    cutoff_ms = (time.time() - MAX_TOKEN_AGE_HOURS * 3600) * 1000
    for p in pairs:
        if p.get("chainId") != "solana":
            continue
        mint = p.get("baseToken", {}).get("address", "")
        if not mint:
            continue
        if require_existing and mint not in tokens:
            continue
        # Skip tokens older than MAX_TOKEN_AGE_HOURS
        pair_created = p.get("pairCreatedAt", 0) or 0
        if pair_created and pair_created < cutoff_ms:
            continue
        mcap = float(p.get("marketCap") or p.get("fdv") or 0)
        _merge_token_entry(tokens, mint, {
            "name":         p.get("baseToken", {}).get("name", ""),
            "symbol":       p.get("baseToken", {}).get("symbol", ""),
            "mcap":         mcap,
            "price_usd":    float(p.get("priceUsd") or 0),
            "volume_h1":    float((p.get("volume") or {}).get("h1", 0)),
            "volume_h6":    float((p.get("volume") or {}).get("h6", 0)),
            "volume_h24":   float((p.get("volume") or {}).get("h24", 0)),
            "volume_m5":    float((p.get("volume") or {}).get("m5", 0)),
            "txns_m5_buys":  int((p.get("txns") or {}).get("m5", {}).get("buys", 0)),
            "txns_m5_sells": int((p.get("txns") or {}).get("m5", {}).get("sells", 0)),
            "price_h24":     float((p.get("priceChange") or {}).get("h24", 0)),
            "price_h1":     float((p.get("priceChange") or {}).get("h1", 0)),
            "liquidity":    float((p.get("liquidity") or {}).get("usd", 0)),
            "dex":          p.get("dexId", ""),
            "pair_address": p.get("pairAddress", ""),
            "pair_created": pair_created,
        }, source_name=source_name, source_rank=source_rank)


def fetch_new_tokens() -> list[dict]:
    """
    Fetch recent Solana tokens within the configured freshness window, sorted newest first.
    """
    tokens: dict[str, dict] = {}
    cutoff_ms = (time.time() - MAX_TOKEN_AGE_HOURS * 3600) * 1000

    # Source 1: Newest Solana pairs sorted by creation time (primary source)
    try:
        pairs = requests.get(
            "https://api.dexscreener.com/latest/dex/pairs/solana",
            timeout=10
        ).json().get("pairs") or []
        # Sort newest first, only keep tokens created inside the freshness window
        pairs = [p for p in pairs if (p.get("pairCreatedAt") or 0) >= cutoff_ms]
        pairs.sort(key=lambda p: p.get("pairCreatedAt", 0), reverse=True)
        _parse_pairs(pairs[:150], tokens, source_name="dex_pairs", source_rank=SOURCE_RANKS["dex_pairs"])
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
                    _merge_token_entry(tokens, mint, {
                        "twitter_url": next(
                            (l["url"] for l in p.get("links", []) if l.get("type") == "twitter"),
                            None
                        ),
                        "description": p.get("description", ""),
                        "has_icon":    bool(p.get("icon")),
                    }, source_name="dex_profiles", source_rank=SOURCE_RANKS["dex_profiles"])
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
                    _merge_token_entry(tokens, mint, {}, source_name="dex_boosts", source_rank=SOURCE_RANKS["dex_boosts"])
    except Exception:
        pass

    # Source 4: Parallel lookup for profile/boost mints missing market data
    profile_mints = [m for m, v in tokens.items() if not v.get("name")]
    if profile_mints:
        def _fetch_mint_pairs(mint: str) -> list:
            try:
                return requests.get(DEXSCREENER_TOKEN + mint, timeout=10).json().get("pairs") or []
            except Exception:
                return []

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as _ex:
            all_pair_batches = list(_ex.map(_fetch_mint_pairs, profile_mints[:25]))
        for pairs in all_pair_batches:
            if pairs:
                _parse_pairs(pairs, tokens, source_name="dex_lookup", source_rank=SOURCE_RANKS["dex_lookup"])

    # Source 5: pump.fun newest coins (pre-graduation tokens with low mcap)
    try:
        pf_new = requests.get(
            "https://frontend-api.pump.fun/coins?sortBy=created_timestamp&order=DESC&limit=50",
            timeout=10
        ).json()
        if isinstance(pf_new, list):
            now_ms = time.time() * 1000
            for coin in pf_new:
                mint = coin.get("mint", "")
                if not mint:
                    continue
                mcap = float(coin.get("usd_market_cap") or 0)
                # created_timestamp from pump.fun is in milliseconds
                created_ts = coin.get("created_timestamp") or 0
                if created_ts < 1e12:  # convert seconds to ms if needed
                    created_ts *= 1000
                if created_ts < cutoff_ms:
                    continue
                _merge_token_entry(tokens, mint, {
                    "name":         coin.get("name", ""),
                    "symbol":       coin.get("symbol", ""),
                    "mcap":         mcap,
                    "pair_created": created_ts,
                    "description":  coin.get("description", ""),
                    "twitter_url":  coin.get("twitter") or None,
                    "price_usd":    0.0,
                    "volume_h1":    0.0,
                    "volume_m5":    0.0,
                    "liquidity":    0.0,
                    "dex":          "pumpfun",
                }, source_name="pumpfun_newest", source_rank=SOURCE_RANKS["pumpfun_newest"])
    except Exception:
        pass

    # Source 6: pump.fun most active right now (highest recent engagement)
    try:
        pf_hot = requests.get(
            "https://frontend-api.pump.fun/coins?sortBy=last_reply&order=DESC&limit=50",
            timeout=10
        ).json()
        if isinstance(pf_hot, list):
            for coin in pf_hot:
                mint = coin.get("mint", "")
                if not mint:
                    continue
                mcap = float(coin.get("usd_market_cap") or 0)
                created_ts = coin.get("created_timestamp") or 0
                if created_ts < 1e12:
                    created_ts *= 1000
                if created_ts < cutoff_ms:
                    continue
                _merge_token_entry(tokens, mint, {
                    "name":         coin.get("name", ""),
                    "symbol":       coin.get("symbol", ""),
                    "mcap":         mcap,
                    "pair_created": created_ts,
                    "description":  coin.get("description", ""),
                    "twitter_url":  coin.get("twitter") or None,
                    "price_usd":    0.0,
                    "volume_h1":    0.0,
                    "volume_m5":    0.0,
                    "liquidity":    0.0,
                    "dex":          "pumpfun",
                }, source_name="pumpfun_hot", source_rank=SOURCE_RANKS["pumpfun_hot"])
    except Exception:
        pass

    # Source 7: DexScreener keyword searches for trending narratives
    _trending_queries = ["pump", "new", "sol", "ai", "meme", "dog", "cat", "pepe"]
    def _dex_search(q: str) -> list:
        try:
            return requests.get(DEXSCREENER_SEARCH + q, timeout=10).json().get("pairs") or []
        except Exception:
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as _ex7:
        _search_results = list(_ex7.map(_dex_search, _trending_queries))
    for pairs in _search_results:
        # Search is enrichment-only: do not let keyword search originate new autobuy candidates.
        _parse_pairs(
            pairs[:20], tokens,
            source_name="dex_search",
            source_rank=SOURCE_RANKS["dex_search"],
            require_existing=True,
        )

    # Filter: must have name + mcap in range + created within cutoff
    # Also skip graduated DEX tokens with zero pool liquidity (rug indicator)
    result = [
        v for v in tokens.values()
        if v.get("name")
        and MCAP_MIN <= v.get("mcap", 0) <= MCAP_MAX
        and (v.get("pair_created", 0) or 0) >= cutoff_ms
        and not (
            v.get("liquidity", 0) < LIQUIDITY_MIN_USD
            and v.get("dex", "pumpfun") not in ("pumpfun", "")
        )
    ]

    # Sort newest first; break ties in favor of stronger source provenance.
    result.sort(key=lambda t: (t.get("pair_created", 0), t.get("_source_rank", 0)), reverse=True)
    return result


def fetch_rugcheck(mint: str) -> dict:
    try:
        r = requests.get(RUGCHECK_REPORT.format(mint=mint), timeout=10)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}



def calculate_heat_score_with_settings(token: dict, rc: dict, user_id: int = None) -> dict:
    """
    Calculate heat score using v2 engine with user settings.
    Falls back to defaults if user_id not provided.
    """
    if user_id:
        user_cfg = settings_manager.get_user_settings(user_id)
    else:
        user_cfg = settings_manager._get_defaults().copy()

    # Map scanner field names to heat_score_v2 expected names
    t = dict(token)
    if "volume_m5" in t and "volume_5m_usd" not in t:
        t["volume_5m_usd"] = t["volume_m5"]
    if "volume_h1" in t and "volume_1h_usd" not in t:
        t["volume_1h_usd"] = t["volume_h1"]
    if "volume_h24" in t and "volume_24h_usd" not in t:
        t["volume_24h_usd"] = t["volume_h24"]
    if "pair_created" in t and "created_timestamp" not in t:
        pc = t["pair_created"]
        t["created_timestamp"] = pc / 1000 if pc > 1e12 else pc
    if "liquidity" in t and "liquidity_usd" not in t:
        t["liquidity_usd"] = t["liquidity"]
    if "twitter_url" in t and "twitter" not in t:
        t["twitter"] = t["twitter_url"]

    result_v2 = heat_score_v2.calculate_heat_score_v2(t, rc, user_cfg)

    score = result_v2["score"]
    if score >= user_cfg.get("alert_ultra_hot_threshold", 85):
        priority = "🔴 ULTRA_HOT"
    elif score >= user_cfg.get("alert_hot_threshold", 70):
        priority = "🟠 HOT"
    elif score >= user_cfg.get("alert_warm_threshold", 55):
        priority = "🟡 WARM"
    else:
        priority = "⚪ SCOUTED"

    factors = result_v2["factors"]
    breakdown = {
        "momentum":         (factors["momentum"]["pts"],         factors["momentum"]["reason"]),
        "liquidity":        (factors["liquidity"]["pts"],        factors["liquidity"]["reason"]),
        "risk_safety":      (factors["risk_safety"]["pts"],      factors["risk_safety"]["reason"]),
        "social_narrative": (factors["social_narrative"]["pts"], factors["social_narrative"]["reason"]),
        "wallets":          (factors["wallets"]["pts"],          factors["wallets"]["reason"]),
        "migration":        (factors["migration"]["pts"],        factors["migration"]["reason"]),
        "directional_bias": (factors["directional_bias"]["pts"], factors["directional_bias"]["reason"]),
        "volume_trend":     (factors["volume_trend"]["pts"],     factors["volume_trend"]["reason"]),
    }

    return {
        "mint":          token.get("mint", ""),
        "name":          token.get("name", ""),
        "symbol":        token.get("symbol", ""),
        "mcap":          token.get("mcap", 0),
        "price_usd":     token.get("price_usd", 0),
        "price_h1":      token.get("price_h1", 0),
        "volume_h1":     token.get("volume_h1", 0),
        "volume_m5":     token.get("volume_m5", 0),
        "liquidity":     token.get("liquidity", 0),
        "total_holders": rc.get("totalHolders", 0),
        "pair_created":  token.get("pair_created", 0),
        "txns_5m":       token.get("txns_m5_buys", 0) + token.get("txns_m5_sells", 0),
        "dex":           token.get("dex", ""),
        "raw_total":     result_v2["raw_score"],
        "total":         score,
        "disqualified":  result_v2["disqualified"],
        "risk":          result_v2["risk_level"],
        "red_flags":     [],
        "breakdown":     breakdown,
        "archetype":     "SCOUT_V2",
        "archetype_conf": 100,
        "v2_result":     result_v2,
    }


def priority_label(score: int) -> str:
    """Priority label for heat score (0-100 scale). Thresholds match HEAT_SCORE_V2_DEFAULTS."""
    if score >= 85: return "🔴 ULTRA HOT"
    if score >= 70: return "🟠 HOT"
    if score >= 55: return "🟡 WARM"
    if score >= 35: return "⚪ SCOUTED"
    return "📡 TRACKED"


def age_str(pair_created_ms: int) -> str:
    if not pair_created_ms:
        return "unknown"
    age_mins = (time.time() * 1000 - pair_created_ms) / 60_000
    if age_mins < 60:
        return f"{age_mins:.0f}m"
    return f"{age_mins/60:.1f}h"


def age_mins(pair_created_ms: int) -> float:
    if not pair_created_ms:
        return 9_999.0
    return max(0.0, (time.time() * 1000 - pair_created_ms) / 60_000)


def age_band(pair_created_ms: int) -> str:
    mins = age_mins(pair_created_ms)
    if mins < 5:
        return "0-5m"
    if mins < 15:
        return "5-15m"
    if mins < 30:
        return "15-30m"
    if mins < 60:
        return "30-60m"
    return "60m+"


def _buy_ratio(txn_buys: int, txn_sells: int) -> float:
    total = max(0, int(txn_buys or 0)) + max(0, int(txn_sells or 0))
    if total <= 0:
        return 0.5
    return max(0.0, min(1.0, (txn_buys or 0) / total))


def _max_holder_pct(rc: dict) -> float:
    top_holders = rc.get("topHolders") or []
    if not top_holders:
        return 0.0
    first = top_holders[0] or {}
    if "pct" in first and first.get("pct") is not None:
        return float(first.get("pct") or 0)
    supply = float(rc.get("supply") or 0)
    balance = float(first.get("balance") or 0)
    if supply > 0:
        return balance / supply * 100.0
    return 0.0


def _prune_quality_state(now_ts: float | None = None):
    now_ts = now_ts or time.time()
    cutoff = now_ts - QUALITY_HISTORY_SECS
    stale = []
    for mint, rows in _quality_history.items():
        kept = [row for row in rows if row.get("ts", 0) >= cutoff]
        if kept:
            _quality_history[mint] = kept[-6:]
        else:
            stale.append(mint)
    for mint in stale:
        _quality_history.pop(mint, None)

    narr_cutoff = now_ts - NARRATIVE_CLUSTER_WINDOW_SECS
    stale_narr = []
    for narrative, rows in _narrative_alert_history.items():
        kept = [ts for ts in rows if ts >= narr_cutoff]
        if kept:
            _narrative_alert_history[narrative] = kept
        else:
            stale_narr.append(narrative)
    for narrative in stale_narr:
        _narrative_alert_history.pop(narrative, None)


def record_narrative_alert(narrative: str, now_ts: float | None = None):
    now_ts = now_ts or time.time()
    _prune_quality_state(now_ts)
    _narrative_alert_history[str(narrative or "Other")].append(now_ts)


def _recent_narrative_alert_count(narrative: str, now_ts: float | None = None) -> int:
    now_ts = now_ts or time.time()
    _prune_quality_state(now_ts)
    return len(_narrative_alert_history.get(str(narrative or "Other"), []))


def build_entry_quality(token: dict, rc: dict, result: dict, narrative: str) -> dict:
    now_ts = time.time()
    _prune_quality_state(now_ts)

    mint = result.get("mint") or token.get("mint") or ""
    source_name = token.get("_source_name") or result.get("_source_name") or ""
    source_rank = token.get("_source_rank", result.get("_source_rank", 0)) or 0
    price_h1 = float(result.get("price_h1", token.get("price_h1", 0)) or 0)
    volume_m5 = float(result.get("volume_m5", token.get("volume_m5", 0)) or 0)
    volume_h1 = float(result.get("volume_h1", token.get("volume_h1", 0)) or 0)
    liquidity = float(result.get("liquidity", token.get("liquidity", 0)) or 0)
    mcap = float(result.get("mcap", token.get("mcap", 0)) or 0)
    tx_buys = int(token.get("txns_m5_buys", 0) or 0)
    tx_sells = int(token.get("txns_m5_sells", 0) or 0)
    txns_5m = int(result.get("txns_5m", tx_buys + tx_sells) or 0)
    buy_ratio_5m = _buy_ratio(tx_buys, tx_sells)
    age_now = age_mins(result.get("pair_created", token.get("pair_created", 0) or 0))
    holder_pct = _max_holder_pct(rc)
    wallet_signal = float(result.get("wallet_signal", result.get("wallet_boost", 0)) or 0)
    effective_score = float(result.get("effective_score", result.get("total", 0)) or 0)

    prior_rows = _quality_history.get(mint, [])
    prev = prior_rows[-1] if prior_rows else None
    score_peak = max([effective_score] + [float(row.get("effective_score", 0) or 0) for row in prior_rows])
    score_drop_from_peak = max(0.0, score_peak - effective_score)
    score_slope = 0.0
    liquidity_drop_pct = 0.0
    buy_ratio_delta = 0.0
    holder_concentration_delta = 0.0
    if prev:
        elapsed_mins = max(0.001, (now_ts - float(prev.get("ts", now_ts))) / 60.0)
        score_slope = (effective_score - float(prev.get("effective_score", effective_score) or effective_score)) / elapsed_mins
        prev_liq = float(prev.get("liquidity", 0) or 0)
        if prev_liq > 0:
            liquidity_drop_pct = (liquidity - prev_liq) / prev_liq * 100.0
        buy_ratio_delta = buy_ratio_5m - float(prev.get("buy_ratio_5m", buy_ratio_5m) or buy_ratio_5m)
        holder_concentration_delta = holder_pct - float(prev.get("holder_concentration_pct", holder_pct) or holder_pct)

    liquidity_to_mcap_ratio = (liquidity / mcap) if mcap > 0 else 0.0
    txns_per_10k_liq = txns_5m / max(liquidity / 10_000.0, 1.0) if liquidity > 0 else 0.0
    narrative_cluster_count = _recent_narrative_alert_count(narrative, now_ts)

    quality = {
        "age_mins": age_now,
        "age_band": age_band(result.get("pair_created", token.get("pair_created", 0) or 0)),
        "source_name": source_name,
        "source_rank": source_rank,
        "buy_ratio_5m": buy_ratio_5m,
        "liquidity_to_mcap_ratio": liquidity_to_mcap_ratio,
        "txns_per_10k_liq": txns_per_10k_liq,
        "holder_concentration_pct": holder_pct,
        "holder_concentration_delta": holder_concentration_delta,
        "score_peak": score_peak,
        "score_drop_from_peak": score_drop_from_peak,
        "score_slope": score_slope,
        "liquidity_drop_pct": liquidity_drop_pct,
        "buy_ratio_delta": buy_ratio_delta,
        "wallet_signal": wallet_signal,
        "narrative_cluster_count": narrative_cluster_count,
        "price_h1": price_h1,
        "volume_m5": volume_m5,
        "volume_h1": volume_h1,
        "mcap": mcap,
        "liquidity": liquidity,
        "txns_5m": txns_5m,
        "narrative": narrative,
    }

    _quality_history[mint].append({
        "ts": now_ts,
        "effective_score": effective_score,
        "liquidity": liquidity,
        "buy_ratio_5m": buy_ratio_5m,
        "holder_concentration_pct": holder_pct,
    })
    _quality_history[mint] = _quality_history[mint][-6:]
    return quality


def apply_entry_quality_rules(quality: dict, *, effective_score: float, momentum_alive: bool) -> dict:
    reasons: list[str] = []
    force_scouted: list[str] = []
    autobuy_only: list[str] = []

    source_name = quality.get("source_name", "")
    age_now = float(quality.get("age_mins", 0) or 0)
    wallet_signal = float(quality.get("wallet_signal", 0) or 0)
    txns_5m = int(quality.get("txns_5m", 0) or 0)
    buy_ratio_5m = float(quality.get("buy_ratio_5m", 0.5) or 0.5)
    buy_ratio_delta = float(quality.get("buy_ratio_delta", 0) or 0)
    liquidity_drop_pct = float(quality.get("liquidity_drop_pct", 0) or 0)
    holder_pct = float(quality.get("holder_concentration_pct", 0) or 0)
    holder_delta = float(quality.get("holder_concentration_delta", 0) or 0)
    score_drop = float(quality.get("score_drop_from_peak", 0) or 0)
    score_slope = float(quality.get("score_slope", 0) or 0)
    liq_to_mcap = float(quality.get("liquidity_to_mcap_ratio", 0) or 0)
    txns_per_10k_liq = float(quality.get("txns_per_10k_liq", 0) or 0)
    mcap = float(quality.get("mcap", 0) or 0)
    narrative_cluster_count = int(quality.get("narrative_cluster_count", 0) or 0)

    if source_name in WEAK_DISCOVERY_SOURCES and effective_score < 85:
        force_scouted.append("weak discovery source")
    if source_name not in PRIMARY_DISCOVERY_SOURCES and wallet_signal < 5:
        autobuy_only.append("source not trusted enough for auto-buy")

    if age_now > 60 and wallet_signal < 5:
        reasons.append("outside fresh launch window")
    elif age_now > STRICT_AUTOBUY_MAX_AGE_MINS and wallet_signal < 5:
        autobuy_only.append("outside first-20m auto-buy window")

    if txns_5m >= 8 and buy_ratio_5m < 0.52:
        reasons.append("buy ratio fading")
    if txns_5m >= 8 and buy_ratio_delta <= -0.12:
        reasons.append("buy pressure collapsing")
    if liquidity_drop_pct <= -12:
        reasons.append("liquidity dropping fast")
    if score_drop >= 8 and score_slope < -3:
        reasons.append("score decaying from peak")
    if holder_pct >= 15 and holder_delta >= 2:
        reasons.append("holder concentration rising")
    if mcap >= 150_000 and liq_to_mcap < 0.025:
        reasons.append("mcap outrunning liquidity")
    if mcap >= 150_000 and txns_per_10k_liq < 2:
        reasons.append("too few txns for pool depth")
    if narrative_cluster_count >= NARRATIVE_CLUSTER_LIMIT and effective_score < 85:
        reasons.append("narrative cluster overcrowded")
    if not momentum_alive and effective_score < 85:
        force_scouted.append("momentum no longer alive")

    return {
        "quality_reasons": reasons,
        "force_scouted_reasons": force_scouted,
        "autobuy_only_reasons": autobuy_only,
        "alert_blocked": bool(reasons),
        "autobuy_blocked": bool(reasons or autobuy_only or force_scouted),
        "force_scouted": bool(force_scouted),
        "primary_source": source_name in PRIMARY_DISCOVERY_SOURCES,
    }


def classify_alert_tier(
    effective_score: int,
    momentum_alive: bool,
    quality_alert_ok: bool = True,
    force_scouted: bool = False,
    watch_threshold: int = 35,
    warm_threshold: int = 55,
    hot_threshold: int = 70,
    ultra_hot_threshold: int = 85,
) -> str | None:
    """Resolve the alert tier for one token using recency-independent score rules."""
    if not quality_alert_ok:
        return None
    if force_scouted and effective_score >= watch_threshold:
        return "SCOUTED"
    if effective_score >= ultra_hot_threshold and momentum_alive:
        return "ULTRA_HOT"
    if effective_score >= hot_threshold and momentum_alive:
        return "HOT"
    if effective_score >= warm_threshold and momentum_alive:
        return "WARM"
    if effective_score >= watch_threshold:
        return "SCOUTED"
    return None


def select_newest_alerts(
    scored_tokens: list[dict],
    chat_ids: list[int],
    user_settings_map: dict[int, dict],
    *,
    channel_enabled: bool = False,
    channel_scouted_threshold: int = 35,
    channel_hot_threshold: int = 70,
) -> tuple[dict[int, tuple[str, dict]], tuple[str, dict] | None]:
    """
    Pick the newest qualifying token per user, plus the newest qualifying channel alert.
    scored_tokens must already be sorted newest first.
    """
    selected_users: dict[int, tuple[str, dict]] = {}
    selected_channel: tuple[str, dict] | None = None

    for result in scored_tokens:
        mcap = result.get("mcap", 0) or 0
        effective_score = result.get("effective_score", result.get("total", 0))
        momentum_alive = bool(result.get("momentum_alive", True))
        quality_alert_ok = not bool(result.get("entry_quality_alert_blocked"))
        force_scouted = bool(result.get("entry_quality_force_scouted"))

        for uid in chat_ids:
            if uid in selected_users:
                continue
            user_cfg = user_settings_map.get(uid, {})
            user_mcap_min = user_cfg.get("scanner_mcap_min", 15_000)
            user_mcap_max = user_cfg.get("scanner_mcap_max", 10_000_000)
            if not (user_mcap_min <= mcap <= user_mcap_max):
                continue

            tier = classify_alert_tier(
                effective_score,
                momentum_alive,
                quality_alert_ok,
                force_scouted,
                user_cfg.get("alert_scouted_threshold", 35),
                user_cfg.get("alert_warm_threshold", 55),
                user_cfg.get("alert_hot_threshold", 70),
                user_cfg.get("alert_ultra_hot_threshold", 85),
            )
            if tier:
                selected_users[uid] = (tier, result)

        if channel_enabled and selected_channel is None:
            tier = None
            if not quality_alert_ok:
                tier = None
            elif force_scouted and effective_score >= channel_scouted_threshold:
                tier = "SCOUTED"
            elif effective_score >= channel_hot_threshold and momentum_alive:
                tier = "HOT"
            elif effective_score >= channel_scouted_threshold:
                tier = "SCOUTED"
            if tier:
                selected_channel = (tier, result)

        if len(selected_users) == len(chat_ids) and (not channel_enabled or selected_channel is not None):
            break

    return selected_users, selected_channel


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

    # Slippage estimate: 0.1 SOL (~$18) into pool depth = price impact
    liq_usd = r.get("liquidity", 0) or 0
    h1_price_chg = r.get("price_h1", 0)
    if liq_usd > 0:
        _trade_usd = 18.0  # approx 0.1 SOL
        _pool_depth = liq_usd / 2  # one side of AMM pool
        _slip_pct = (_trade_usd / _pool_depth) * 100 if _pool_depth > 0 else 999
        if _slip_pct >= 5:
            slippage_line = f"⚠️ Slippage: `~{_slip_pct:.0f}%` on 0.1◎ buy (pool ${liq_usd:,.0f}) — size down!\n"
        elif _slip_pct >= 1:
            slippage_line = f"💧 Slippage: `~{_slip_pct:.1f}%` on 0.1◎ buy (pool ${liq_usd:,.0f})\n"
        else:
            slippage_line = f"💧 Liquidity: `${liq_usd:,.0f}` (slippage <1% on 0.1◎)\n"
    else:
        slippage_line = f"⚠️ Liquidity: `unknown/zero` — check before buying\n"

    momentum_dir = f"+{h1_price_chg:.0f}%" if h1_price_chg >= 0 else f"{h1_price_chg:.0f}%"
    momentum_emoji = "📈" if h1_price_chg >= 5 else ("📉" if h1_price_chg < -5 else "➡️")

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
        + f"💵 Price: `{price_str}`  {momentum_emoji} 1h: `{momentum_dir}`\n"
        f"🏦 MCap: `${r['mcap']:,.0f}`\n"
        f"📊 Vol (1h): `${r['volume_h1']:,.0f}`\n"
        f"👛 Holders: `{r['total_holders']}`\n"
        f"⏰ Age: `{age_str(r['pair_created'])}`\n"
        f"🏪 DEX: `{r.get('dex', 'N/A')}`\n"
        + slippage_line
        + f"━━━━━━━━━━━━━━━━━━━\n"
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
    loop = asyncio.get_running_loop()
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

    scored_candidates: list[dict] = []

    for token, rc_raw in zip(to_process, rugcheck_results):
        mint = token["mint"]
        mcap = token.get("mcap", 0)
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
        _narr_lower = narr_reason.lower()
        matched_narrative = next((n for n in NARRATIVES if n.lower() in _narr_lower
                                  or any(k in _narr_lower for k in NARRATIVES[n])), "Other")
        result["matched_narrative"] = matched_narrative
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

        # Mark as seen right after scoring so we don't re-score on every 15s cycle.
        # TTL matches alert cooldown (1h) so tokens become eligible again once cooldown expires.
        mark_seen_token(mint)

        if result["disqualified"]:
            continue

        # ── Momentum gate: detect post-peak tokens before alerting ──────────────
        # A high score with declining price + dead volume = already peaked.
        # Winners (SIRI, OLIVEOIL, CLAWFIRST) all had positive h1 price OR
        # strong m5 volume when bought. Force losers to SCOUTED tier only.
        _h1_price  = token.get("price_h1", 0)
        _vol_m5    = token.get("volume_m5", 0)
        _vol_h1    = token.get("volume_h1", 1) or 1
        _vol_m5_pace = _vol_m5 * 12  # annualize m5 to hourly rate
        # Momentum is "alive" if price isn't declining OR volume is still spiking
        _momentum_alive = _h1_price >= -5 or _vol_m5_pace >= _vol_h1 * 0.3
        if not _momentum_alive:
            print(
                f"[SCANNER] {result.get('symbol', mint[:6])} momentum DEAD "
                f"(h1_price={_h1_price:+.1f}%, m5_pace=${_vol_m5_pace:,.0f} vs h1=${_vol_h1:,.0f}) "
                f"— capping to SCOUTED",
                flush=True,
            )
        result["momentum_alive"] = _momentum_alive

        quality = build_entry_quality(token, rc, result, matched_narrative)
        quality_flags = apply_entry_quality_rules(
            quality,
            effective_score=effective_score,
            momentum_alive=_momentum_alive,
        )
        result.update(quality)
        result["entry_quality_reasons"] = quality_flags["quality_reasons"]
        result["entry_quality_force_scouted"] = quality_flags["force_scouted"]
        result["entry_quality_alert_blocked"] = quality_flags["alert_blocked"]
        result["entry_quality_autobuy_blocked"] = quality_flags["autobuy_blocked"]
        result["entry_quality_force_scouted_reasons"] = quality_flags["force_scouted_reasons"]
        result["entry_quality_autobuy_only_reasons"] = quality_flags["autobuy_only_reasons"]
        result["entry_quality_primary_source"] = quality_flags["primary_source"]
        if quality_flags["alert_blocked"] or quality_flags["force_scouted"] or quality_flags["autobuy_only_reasons"]:
            print(
                f"[SCANNER] {result.get('symbol', mint[:6])} quality "
                f"alert_blocked={quality_flags['alert_blocked']} "
                f"force_scouted={quality_flags['force_scouted']} "
                f"autobuy_only={bool(quality_flags['autobuy_only_reasons'])} "
                f"reasons={quality_flags['quality_reasons'] + quality_flags['force_scouted_reasons'] + quality_flags['autobuy_only_reasons']}",
                flush=True,
            )
        scored_candidates.append(result)

    if not scored_candidates:
        return

    user_settings_map = {}
    for uid in chat_ids:
        try:
            user_settings_map[uid] = settings_manager.get_user_settings(uid)
        except Exception:
            user_settings_map[uid] = {}

    selected_user_alerts, selected_channel_alert = select_newest_alerts(
        scored_candidates,
        chat_ids,
        user_settings_map,
        channel_enabled=bool(alert_channel),
        channel_scouted_threshold=CH_SCOUTED_THR,
        channel_hot_threshold=CH_HOT_THR,
    )

    alert_groups: dict[tuple[str, str], dict] = {}
    autobuy_targets: dict[str, dict] = {}
    for uid, (tier, result) in selected_user_alerts.items():
        key = (result["mint"], tier)
        group = alert_groups.setdefault(key, {"tier": tier, "result": result, "uids": []})
        group["uids"].append(uid)
        bucket = autobuy_targets.setdefault(result["mint"], {"result": result, "uids": set()})
        bucket["uids"].add(uid)

    ordered_alert_groups = sorted(
        alert_groups.values(),
        key=lambda g: g["result"].get("pair_created", 0),
        reverse=True,
    )

    from telegram import InlineKeyboardMarkup, InlineKeyboardButton

    for group in ordered_alert_groups:
        tier = group["tier"]
        result = group["result"]
        mint = result["mint"]
        mcap = result.get("mcap", 0)

        if tier == "SCOUTED":
            record_narrative_alert(result.get("matched_narrative", "Other"))
            add_to_watchlist(mint, {
                "name": result["name"], "symbol": result["symbol"],
                "score": result["total"], "mcap": mcap, "mint": mint,
                "ts": time.time(),
            })
            _db.mark_scan_log_alerted(mint)
            scout_msg = format_scouted_alert(result)[:4000]
            scout_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🟢 Buy",      callback_data=f"quick:buy:{mint}"),
                 InlineKeyboardButton("🤖 Analyze",  callback_data=f"quick:analyze:{mint}"),
                 InlineKeyboardButton("🔔 Alert",    callback_data=f"quick:alert:{mint}")],
                [InlineKeyboardButton("📊 Chart",    url=f"https://dexscreener.com/solana/{mint}"),
                 InlineKeyboardButton("🔫 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
                 InlineKeyboardButton("🪙 Pump",     url=f"https://pump.fun/{mint}")],
            ])
            for uid in group["uids"]:
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
            continue

        _db.mark_scan_log_alerted(mint)
        record_narrative_alert(result.get("matched_narrative", "Other"))
        try:
            msg = format_alert(result)[:4000]
        except Exception as _fe:
            print(f"[SCANNER] format_alert error for {result.get('symbol', mint[:8])}: {_fe}", flush=True)
            continue
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Buy",      callback_data=f"quick:buy:{mint}"),
             InlineKeyboardButton("🤖 Analyze",  callback_data=f"quick:analyze:{mint}"),
             InlineKeyboardButton("🔔 Alert",    callback_data=f"quick:alert:{mint}")],
            [InlineKeyboardButton("📊 Chart",    url=f"https://dexscreener.com/solana/{mint}"),
             InlineKeyboardButton("🔫 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
             InlineKeyboardButton("🪙 Pump",     url=f"https://pump.fun/{mint}")],
        ])
        for uid in group["uids"]:
            try:
                await bot.send_message(
                    chat_id=uid, text=msg,
                    parse_mode="Markdown", reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception as _e1:
                _e1s = str(_e1)
                if "Flood control" in _e1s or "Too Many Requests" in _e1s:
                    import re as _re
                    _wait = int((_re.search(r"Retry in (\d+)", _e1s) or [None, 5])[1])
                    print(f"[SCANNER] flood control — waiting {_wait}s", flush=True)
                    await asyncio.sleep(_wait + 1)
                    try:
                        await bot.send_message(chat_id=uid, text=msg, reply_markup=kb, disable_web_page_preview=True)
                    except Exception as e:
                        print(f"[SCANNER] DM send error uid={uid}: {e}", flush=True)
                else:
                    try:
                        await bot.send_message(chat_id=uid, text=msg, reply_markup=kb, disable_web_page_preview=True)
                    except Exception as e:
                        print(f"[SCANNER] DM send error uid={uid}: {e}", flush=True)

    if selected_channel_alert and alert_channel:
        channel_tier, channel_result = selected_channel_alert
        mint = channel_result["mint"]
        if channel_tier == "SCOUTED":
            record_narrative_alert(channel_result.get("matched_narrative", "Other"))
            add_to_watchlist(mint, {
                "name": channel_result["name"], "symbol": channel_result["symbol"],
                "score": channel_result["total"], "mcap": channel_result.get("mcap", 0), "mint": mint,
                "ts": time.time(),
            })
            _db.mark_scan_log_alerted(mint)
            channel_msg = format_scouted_alert(channel_result)[:4000]
            channel_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Chart",    url=f"https://dexscreener.com/solana/{mint}"),
                 InlineKeyboardButton("🔫 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
                 InlineKeyboardButton("🪙 Pump",     url=f"https://pump.fun/{mint}")],
            ])
            try:
                await bot.send_message(
                    chat_id=alert_channel, text=channel_msg,
                    parse_mode="Markdown", reply_markup=channel_kb,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                print(f"[SCANNER] scouted channel error ch={alert_channel}: {e}", flush=True)
        else:
            _db.mark_scan_log_alerted(mint)
            record_narrative_alert(channel_result.get("matched_narrative", "Other"))
            try:
                channel_msg = format_alert(channel_result)[:4000]
            except Exception as e:
                print(f"[SCANNER] channel format error ch={alert_channel}: {e}", flush=True)
                channel_msg = None
            if channel_msg:
                channel_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📊 Chart",    url=f"https://dexscreener.com/solana/{mint}"),
                     InlineKeyboardButton("🔫 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
                     InlineKeyboardButton("🪙 Pump",     url=f"https://pump.fun/{mint}")],
                ])
                try:
                    await bot.send_message(
                        chat_id=alert_channel, text=channel_msg,
                        parse_mode="Markdown", reply_markup=channel_kb,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    try:
                        await bot.send_message(
                            chat_id=alert_channel, text=channel_msg,
                            reply_markup=channel_kb, disable_web_page_preview=True,
                        )
                    except Exception as e:
                        print(f"[SCANNER] channel send error ch={alert_channel}: {e}", flush=True)

    if on_alert:
        ordered_autobuy = sorted(
            autobuy_targets.values(),
            key=lambda g: g["result"].get("pair_created", 0),
            reverse=True,
        )
        for group in ordered_autobuy:
            try:
                await on_alert(bot, group["result"], target_uids=sorted(group["uids"]))
            except TypeError:
                await on_alert(bot, group["result"])
            except Exception:
                pass

    # Log writes are now committed to SQLite directly in append_log; no flush needed.



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
    result = calculate_heat_score_with_settings(token, rc)
    return result
