"""
Wallet Discovery — Phase 2 auto-discovery of smart wallets.

Scans pump.fun graduation history to find wallets that consistently
buy early into tokens that later reach high market caps.

Discovery pipeline:
  1. Fetch top graduated tokens by MCap from pump.fun API (the "winners")
  2. For each token, fetch its earliest trades
  3. Find wallets who bought early (within EARLY_BUY_WINDOW seconds of launch)
  4. Score wallets by: number of winning tokens entered early + avg MCap reached
  5. Surface top wallets via /discoverwallet command
"""
from __future__ import annotations

import json
import os
import time
import requests

DATA_DIR              = os.path.join(os.path.dirname(__file__), "data")
DISCOVERY_STATE_FILE  = os.path.join(DATA_DIR, "wallet_discovery.json")
os.makedirs(DATA_DIR, exist_ok=True)

PUMPFUN_API = "https://frontend-api-v3.pump.fun"

# ── Discovery thresholds ──────────────────────────────────────────────────────
MIN_TOKEN_MCAP_USD  = 300_000   # Token must have reached $300k+ to be "successful"
MIN_TOKENS_WON      = 2         # Wallet must appear early in N+ winners
EARLY_BUY_WINDOW    = 300       # ≤5 min after launch = "early" buyer
MAX_TOKENS_TO_SCAN  = 50        # How many top tokens to analyse per run
DISCOVERY_CACHE_TTL = 3600 * 6  # Re-scan every 6 hours (avoids hammering API)
MAX_TRADES_PER_TOKEN = 20       # Only look at first 20 trades per token


# ─── State helpers ─────────────────────────────────────────────────────────────

def load_discovery_state() -> dict:
    try:
        with open(DISCOVERY_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"discovered_wallets": {}, "last_scan_ts": 0, "scanned_mints": []}


def save_discovery_state(s: dict):
    with open(DISCOVERY_STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


# ─── pump.fun API helpers ──────────────────────────────────────────────────────

def _fetch_top_graduated(limit: int = MAX_TOKENS_TO_SCAN) -> list:
    """Return graduated pump.fun tokens sorted by market cap descending."""
    try:
        r = requests.get(
            f"{PUMPFUN_API}/coins",
            params={
                "offset": "0", "limit": str(limit),
                "sort": "market_cap", "order": "DESC",
                "includeNsfw": "false", "complete": "true",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _fetch_earliest_trades(mint: str) -> list:
    """Return the earliest MAX_TRADES_PER_TOKEN buys for a token, oldest-first."""
    try:
        r = requests.get(
            f"{PUMPFUN_API}/trades/{mint}",
            params={"offset": "0", "limit": str(MAX_TRADES_PER_TOKEN), "minimumSize": "0"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        trades = data if isinstance(data, list) else []
        # Sorted ascending = earliest first
        return sorted(trades, key=lambda t: t.get("timestamp", 0))
    except Exception:
        return []


# ─── Core discovery logic ──────────────────────────────────────────────────────

def run_discovery(force: bool = False) -> dict:
    """
    Scan pump.fun top tokens and identify smart wallets that consistently
    entered winning tokens early.

    Returns the discovered_wallets dict {address: {...stats...}}.
    Uses a 6-hour cache to avoid redundant API calls.
    """
    s = load_discovery_state()
    now = time.time()

    if not force and (now - s.get("last_scan_ts", 0)) < DISCOVERY_CACHE_TTL:
        return s.get("discovered_wallets", {})

    coins = _fetch_top_graduated()
    if not coins:
        return s.get("discovered_wallets", {})

    # wallet_stats: address → {tokens: {mint: {...}}, total_tokens: int}
    wallet_stats: dict[str, dict] = {}

    for coin in coins:
        mint     = coin.get("mint", "")
        mcap_usd = float(coin.get("usd_market_cap") or coin.get("market_cap") or 0)
        created_ms = coin.get("created_timestamp") or 0
        created_ts = created_ms / 1000 if created_ms > 1e10 else created_ms

        if not mint or mcap_usd < MIN_TOKEN_MCAP_USD:
            continue

        trades = _fetch_earliest_trades(mint)
        if not trades:
            continue

        # Infer launch timestamp from coin metadata or first trade
        launch_ts = created_ts
        if not launch_ts and trades:
            first_ts_raw = trades[0].get("timestamp", 0)
            launch_ts = first_ts_raw / 1000 if first_ts_raw > 1e10 else first_ts_raw

        for trade in trades:
            if not trade.get("is_buy", True):
                continue

            wallet = trade.get("user", "")
            if not wallet:
                continue

            ts_raw = trade.get("timestamp", 0)
            trade_ts = ts_raw / 1000 if ts_raw > 1e10 else ts_raw
            secs_after = trade_ts - launch_ts if launch_ts else 0

            if secs_after > EARLY_BUY_WINDOW or secs_after < 0:
                continue

            if wallet not in wallet_stats:
                wallet_stats[wallet] = {"tokens": {}, "total_tokens": 0}

            # One entry per wallet per token (the earliest one)
            if mint not in wallet_stats[wallet]["tokens"]:
                wallet_stats[wallet]["tokens"][mint] = {
                    "mcap_usd":          mcap_usd,
                    "buy_ts":            trade_ts,
                    "secs_after_launch": max(0, int(secs_after)),
                }
                wallet_stats[wallet]["total_tokens"] += 1

        time.sleep(0.15)  # polite rate limit between token fetches

    # Score and filter
    discovered: dict[str, dict] = {}

    for wallet, stats in wallet_stats.items():
        tokens_won = len(stats["tokens"])
        if tokens_won < MIN_TOKENS_WON:
            continue

        mcaps         = [t["mcap_usd"] for t in stats["tokens"].values()]
        entry_secs    = [t["secs_after_launch"] for t in stats["tokens"].values()]
        avg_mcap      = sum(mcaps) / tokens_won
        avg_entry_sec = sum(entry_secs) / tokens_won

        # Discovery score (0–100):
        #   wins × 20   → up to 40 pts for 2+ wins
        #   avg MCap    → up to 30 pts for $3M+ avg
        #   early entry → up to 20 pts for sub-10s avg entry
        score = int(min(100,
            min(40, tokens_won * 20) +
            min(30, avg_mcap / 100_000) +
            max(0, 20 - avg_entry_sec / 15)
        ))

        top_tokens = sorted(
            [{"mint": k, "mcap_usd": int(v["mcap_usd"])} for k, v in stats["tokens"].items()],
            key=lambda x: x["mcap_usd"], reverse=True,
        )[:5]

        discovered[wallet] = {
            "discovery_score":  score,
            "tokens_won":       tokens_won,
            "avg_mcap_usd":     int(avg_mcap),
            "avg_entry_secs":   int(avg_entry_sec),
            "top_tokens":       top_tokens,
            "last_seen_ts":     max(t["buy_ts"] for t in stats["tokens"].values()),
            "auto_discovered":  True,
        }

    s["discovered_wallets"] = discovered
    s["last_scan_ts"]       = now
    s["scanned_mints"]      = [c.get("mint") for c in coins if c.get("mint")]
    save_discovery_state(s)
    return discovered


def get_top_discovered(limit: int = 10) -> list:
    """Return top N discovered wallets sorted by discovery_score desc."""
    s = load_discovery_state()
    discovered = s.get("discovered_wallets", {})
    ranked = sorted(
        [{"address": addr, **data} for addr, data in discovered.items()],
        key=lambda x: x.get("discovery_score", 0),
        reverse=True,
    )
    return ranked[:limit]


def last_scan_age_secs() -> float:
    """How many seconds ago the last discovery scan ran."""
    return time.time() - load_discovery_state().get("last_scan_ts", 0)


def promote_wallet(address: str) -> dict | None:
    """Return a discovered wallet's data (used when adding it to watchlist)."""
    return load_discovery_state().get("discovered_wallets", {}).get(address)
