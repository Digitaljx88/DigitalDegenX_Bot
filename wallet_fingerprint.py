"""
Wallet Fingerprinting — Phase 3 behavioral analysis.

Detects:
  1. Funding lineage: who funded a wallet's initial SOL?
  2. Coordinated clusters: wallets sharing the same funder
  3. Bundle risk: multiple cluster wallets buying the same new token rapidly

Bundle detection logic:
  - For a new token, collect early buyers (within BUNDLE_WINDOW_SECS of launch)
  - For each buyer, look up their funding source via Solana RPC
  - If 2+ buyers share the same funder → bundle cluster
  - bundle_risk score: 0 (clean) → 10 (high risk, heavily coordinated)

Uses SOLANA_RPC from config. Results are cached in data/fingerprint_cache.json
to minimize expensive RPC calls (cache TTL: 24h per wallet).
"""

import json
import os
import time
import requests

DATA_DIR         = os.path.join(os.path.dirname(__file__), "data")
CACHE_FILE       = os.path.join(DATA_DIR, "fingerprint_cache.json")
BUNDLE_LOG_FILE  = os.path.join(DATA_DIR, "bundle_log.json")
os.makedirs(DATA_DIR, exist_ok=True)

CACHE_TTL           = 86400   # 24h per wallet funding record
BUNDLE_WINDOW_SECS  = 60      # Early buyers = within 60s of token launch
RPC_TIMEOUT         = 8       # seconds per RPC call
MAX_SIGS_TO_CHECK   = 10      # How far back to look for funding tx
MIN_FUNDING_SOL     = 0.01    # Ignore dust transfers below this

try:
    from config import SOLANA_RPC
except ImportError:
    SOLANA_RPC = "https://api.mainnet-beta.solana.com"


# ─── Cache helpers ─────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"wallets": {}}


def _save_cache(c: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(c, f, indent=2)


def _load_bundle_log() -> list:
    try:
        with open(BUNDLE_LOG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _append_bundle_log(entry: dict):
    log = _load_bundle_log()
    log.append(entry)
    if len(log) > 200:
        log = log[-200:]
    with open(BUNDLE_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


# ─── Solana RPC helpers ────────────────────────────────────────────────────────

def _rpc(method: str, params: list) -> dict:
    """Make a single JSON-RPC call to the configured Solana node."""
    try:
        r = requests.post(
            SOLANA_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=RPC_TIMEOUT,
        )
        return r.json().get("result") or {}
    except Exception:
        return {}


def _get_oldest_sigs(address: str, limit: int = MAX_SIGS_TO_CHECK) -> list:
    """Return the N oldest transaction signatures for an address (first-ever txs)."""
    result = _rpc("getSignaturesForAddress", [
        address,
        {"limit": limit, "commitment": "confirmed"},
    ])
    if isinstance(result, list):
        # Reverse so oldest is first
        return list(reversed(result))
    return []


def _get_tx(sig: str) -> dict:
    """Fetch a transaction by signature. Returns the parsed tx or {}."""
    result = _rpc("getTransaction", [
        sig,
        {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0},
    ])
    return result if isinstance(result, dict) else {}


# ─── Funding lineage detection ─────────────────────────────────────────────────

def get_funding_wallet(address: str) -> dict:
    """
    Find the wallet that funded `address` with its initial SOL.

    Returns:
        {
            "funder":         str | None,   # wallet that sent initial SOL
            "fund_amount_sol": float,
            "fund_ts":        float,
            "cached":         bool,
        }
    """
    cache = _load_cache()
    entry = cache["wallets"].get(address)

    if entry and (time.time() - entry.get("cached_ts", 0)) < CACHE_TTL:
        return {**entry, "cached": True}

    # Look at oldest transactions for this wallet
    sigs = _get_oldest_sigs(address)
    funder      = None
    fund_amount = 0.0
    fund_ts     = 0.0

    for sig_info in sigs:
        sig = sig_info.get("signature", "")
        if not sig:
            continue

        tx = _get_tx(sig)
        if not tx:
            continue

        meta     = tx.get("meta") or {}
        message  = (tx.get("transaction") or {}).get("message") or {}
        accounts = message.get("accountKeys") or []

        # Look for a SOL transfer (system program transfer instruction)
        instructions = message.get("instructions") or []
        for ix in instructions:
            parsed = ix.get("parsed") or {}
            if not isinstance(parsed, dict):
                continue
            if parsed.get("type") != "transfer":
                continue
            info = parsed.get("info") or {}
            dest     = info.get("destination", "")
            src      = info.get("source", "")
            lamports = int(info.get("lamports") or 0)
            sol      = lamports / 1e9

            if dest == address and src and sol >= MIN_FUNDING_SOL:
                funder      = src
                fund_amount = sol
                # Use blockTime from signature info
                fund_ts = sig_info.get("blockTime") or tx.get("blockTime") or 0
                break

        if funder:
            break

        # Fallback: check pre/post balances
        if not funder and accounts:
            pre_bals  = meta.get("preBalances")  or []
            post_bals = meta.get("postBalances") or []
            for i, acc in enumerate(accounts):
                acc_key = acc.get("pubkey") if isinstance(acc, dict) else str(acc)
                if acc_key == address and i < len(pre_bals) and i < len(post_bals):
                    delta = (post_bals[i] - pre_bals[i]) / 1e9
                    if delta >= MIN_FUNDING_SOL:
                        # Find sender: largest decrease
                        for j, acc2 in enumerate(accounts):
                            acc2_key = acc2.get("pubkey") if isinstance(acc2, dict) else str(acc2)
                            if j < len(pre_bals) and j < len(post_bals):
                                delta2 = (post_bals[j] - pre_bals[j]) / 1e9
                                if delta2 < -MIN_FUNDING_SOL and acc2_key != address:
                                    funder      = acc2_key
                                    fund_amount = delta
                                    fund_ts     = sig_info.get("blockTime") or 0
                                    break
                    if funder:
                        break

        time.sleep(0.08)  # polite rate limit

    record = {
        "address":         address,
        "funder":          funder,
        "fund_amount_sol": fund_amount,
        "fund_ts":         fund_ts,
        "cached_ts":       time.time(),
        "cached":          False,
    }
    cache["wallets"][address] = record
    _save_cache(cache)
    return record


# ─── Bundle detection ──────────────────────────────────────────────────────────

def score_bundle_risk(token_mint: str, early_buyers: list) -> dict:
    """
    Analyse early_buyers for coordinated bundle patterns.

    early_buyers: list of wallet addresses (strings) that bought early

    Returns:
        {
            "bundle_risk":    int,   # 0–10 (0=clean, 10=high risk)
            "reason":         str,
            "clusters":       list,  # [{funder, wallets: [...], count: N}]
            "bundle_wallets": list,  # flat list of all flagged wallet addresses
        }
    """
    if not early_buyers:
        return {"bundle_risk": 0, "reason": "No early buyers to analyse",
                "clusters": [], "bundle_wallets": []}

    # Resolve funders for each buyer (with cache)
    funder_map: dict[str, list] = {}  # funder → [buyer_wallets]
    for buyer in early_buyers[:12]:   # limit 12 buyers to avoid RPC storm
        rec = get_funding_wallet(buyer)
        funder = rec.get("funder")
        if funder:
            funder_map.setdefault(funder, []).append(buyer)

    # Find clusters: funders with 2+ buyers in this token's early buys
    clusters = []
    bundle_wallets = []
    for funder, wallets in funder_map.items():
        if len(wallets) >= 2:
            clusters.append({
                "funder":  funder,
                "wallets": wallets,
                "count":   len(wallets),
            })
            bundle_wallets.extend(wallets)

    bundle_wallets = list(set(bundle_wallets))

    if not clusters:
        result = {"bundle_risk": 0, "reason": "No coordinated wallets detected",
                  "clusters": [], "bundle_wallets": []}
    else:
        total_bundled = len(bundle_wallets)
        max_cluster   = max(c["count"] for c in clusters)

        # Score: larger cluster = higher risk
        if max_cluster >= 5 or total_bundled >= 6:
            bundle_risk = 10
            reason = f"High bundle risk: {total_bundled} coordinated wallets in {len(clusters)} cluster(s)"
        elif max_cluster >= 3 or total_bundled >= 4:
            bundle_risk = 7
            reason = f"Moderate bundle risk: {total_bundled} wallets, largest cluster={max_cluster}"
        else:
            bundle_risk = 4
            reason = f"Mild bundle risk: {len(clusters)} cluster(s), {total_bundled} shared-funder wallets"

        result = {
            "bundle_risk":    bundle_risk,
            "reason":        reason,
            "clusters":      sorted(clusters, key=lambda c: c["count"], reverse=True),
            "bundle_wallets": bundle_wallets,
        }

        # Log the bundle detection
        _append_bundle_log({
            "mint":        token_mint,
            "ts":          time.time(),
            "bundle_risk": bundle_risk,
            "reason":      reason,
            "clusters":    result["clusters"],
        })

    return result


def get_bundle_log(limit: int = 20) -> list:
    """Return the most recently detected bundle events."""
    log = _load_bundle_log()
    return sorted(log, key=lambda x: x.get("ts", 0), reverse=True)[:limit]


def get_cached_funder(address: str) -> str | None:
    """Quick lookup: return funder from cache only (no RPC call)."""
    entry = _load_cache()["wallets"].get(address)
    return entry.get("funder") if entry else None
