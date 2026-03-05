"""
Wallet tracking and reputation system.

Tracks smart trader wallet entries for early signal detection.
Score reputation based on historical win rate.
"""

import json
import os
import time

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
WALLET_STATE_FILE = os.path.join(DATA_DIR, "wallet_tracker_state.json")
os.makedirs(DATA_DIR, exist_ok=True)


# ─── State helpers ────────────────────────────────────────────────────────────

def load_wallet_state() -> dict:
    """Load wallet tracking state from disk."""
    try:
        with open(WALLET_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"watched_wallets": {}, "recent_entries": {}}


def save_wallet_state(s: dict):
    """Save wallet tracking state to disk."""
    with open(WALLET_STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


# ─── Watched wallet management ────────────────────────────────────────────────

def get_watched_wallets() -> dict:
    """Get all tracked wallets: {address: {name, score, wins, entries, ...}}."""
    return load_wallet_state().get("watched_wallets", {})


def add_watched_wallet(address: str, name: str = ""):
    """Add a wallet to tracking list."""
    if not address or len(address) < 32:
        return False
    
    s = load_wallet_state()
    s.setdefault("watched_wallets", {})[address] = {
        "name": name or address[:8],
        "added_ts": time.time(),
        "reputation_score": 50,  # Neutral starting score
        "entries_total": 0,
        "wins": 0,
        "losses": 0,
        "avg_roi": 0.0,
        "last_entry_ts": 0,
        "entries": [],  # Last 50 entries
    }
    save_wallet_state(s)
    return True


def remove_watched_wallet(address: str):
    """Remove a wallet from tracking."""
    s = load_wallet_state()
    if address in s.get("watched_wallets", {}):
        del s["watched_wallets"][address]
        save_wallet_state(s)
        return True
    return False


def is_wallet_tracked(address: str) -> bool:
    """Check if a wallet is in the tracked list."""
    return address in get_watched_wallets()


def get_wallet_performance(address: str) -> dict:
    """Get reputation and performance metrics for a wallet."""
    wallets = get_watched_wallets()
    if address not in wallets:
        return {}
    
    w = wallets[address]
    return {
        "address": address,
        "name": w.get("name", address[:8]),
        "reputation_score": w.get("reputation_score", 50),
        "entries_total": w.get("entries_total", 0),
        "wins": w.get("wins", 0),
        "losses": w.get("losses", 0),
        "win_rate": (w.get("wins", 0) / max(1, w.get("entries_total", 1))) * 100,
        "avg_roi": w.get("avg_roi", 0.0),
        "last_entry_ts": w.get("last_entry_ts", 0),
    }


# ─── Activity detection ───────────────────────────────────────────────────────

def detect_wallet_entry(token_mint: str, time_window_secs: int = 60) -> list:
    """
    Detect if any tracked wallets entered a token recently.
    
    Returns list of dicts: [{wallet_addr, wallet_name, entry_ts, buy_usd, reputation}]
    """
    s = load_wallet_state()
    recent_entries = s.get("recent_entries", {}).get(token_mint, [])
    
    # Filter to time window
    now = time.time()
    cutoff = now - time_window_secs
    
    tracked_entries = []
    for entry in recent_entries:
        if entry.get("ts", 0) >= cutoff:
            wallet = entry.get("wallet", "")
            if wallet in s.get("watched_wallets", {}):
                w = s["watched_wallets"][wallet]
                tracked_entries.append({
                    "wallet": wallet,
                    "name": w.get("name", wallet[:8]),
                    "entry_ts": entry.get("ts", 0),
                    "buy_usd": entry.get("buy_usd", 0),
                    "reputation": w.get("reputation_score", 50),
                })
    
    return sorted(tracked_entries, key=lambda x: x["entry_ts"], reverse=True)


def record_wallet_activity(wallet_addr: str, token_mint: str, buy_usd: float, timestamp: float):
    """Record a wallet's swap/buy activity on a token."""
    s = load_wallet_state()
    
    # Track whale entries for token
    s.setdefault("recent_entries", {})[token_mint] = s.get("recent_entries", {}).get(token_mint, [])
    s["recent_entries"][token_mint].append({
        "wallet": wallet_addr,
        "ts": timestamp,
        "buy_usd": buy_usd,
    })
    
    # Keep only last 100 entries per token
    if len(s["recent_entries"][token_mint]) > 100:
        s["recent_entries"][token_mint] = s["recent_entries"][token_mint][-100:]
    
    # Update wallet's last entry time
    if wallet_addr in s.get("watched_wallets", {}):
        s["watched_wallets"][wallet_addr]["last_entry_ts"] = timestamp
        s["watched_wallets"][wallet_addr]["entries_total"] = s["watched_wallets"][wallet_addr].get("entries_total", 0) + 1
        
        # Keep last 50 entries per wallet
        s["watched_wallets"][wallet_addr].setdefault("entries", []).append({
            "mint": token_mint,
            "ts": timestamp,
            "usd": buy_usd,
            "outcome": None,  # Updated later when token pumps/dumps
        })
        if len(s["watched_wallets"][wallet_addr]["entries"]) > 50:
            s["watched_wallets"][wallet_addr]["entries"] = s["watched_wallets"][wallet_addr]["entries"][-50:]
    
    save_wallet_state(s)


def update_wallet_reputation(wallet_addr: str, token_result: dict):
    """
    Update wallet reputation based on token outcome.
    
    token_result: {mint, profit_usd, profit_pct, is_win}
    """
    s = load_wallet_state()
    
    if wallet_addr not in s.get("watched_wallets", {}):
        return
    
    w = s["watched_wallets"][wallet_addr]
    is_win = token_result.get("is_win", False)
    roi = token_result.get("profit_pct", 0)
    
    # Update win/loss tracking
    if is_win:
        w["wins"] = w.get("wins", 0) + 1
    else:
        w["losses"] = w.get("losses", 0) + 1
    
    # Update avg ROI
    total = w.get("entries_total", 1)
    prev_avg = w.get("avg_roi", 0.0)
    w["avg_roi"] = ((prev_avg * (total - 1)) + roi) / total
    
    # Update reputation score (0–100, starts at 50)
    # +5 per win, -3 per loss, bonus for high ROI
    win_rate = (w.get("wins", 0) / max(1, total)) * 100
    roi_bonus = min(10, roi / 10)  # Capped at +10
    
    base_score = 50 + (win_rate * 0.3) + roi_bonus
    w["reputation_score"] = max(0, min(100, base_score))
    
    # Mark entry as completed
    mint = token_result.get("mint")
    for entry in w.get("entries", []):
        if entry.get("mint") == mint and entry.get("outcome") is None:
            entry["outcome"] = "win" if is_win else "loss"
            entry["roi"] = roi
            break
    
    save_wallet_state(s)


def get_recent_entries(age_mins: int = 15) -> list:
    """
    Get all recent wallet entries across all tokens.
    
    Returns: [{mint, wallet, wallet_name, reputation, entry_ts, buy_usd}]
    """
    s = load_wallet_state()
    now = time.time()
    cutoff = now - (age_mins * 60)
    
    result = []
    for token_mint, entries in s.get("recent_entries", {}).items():
        for entry in entries:
            if entry.get("ts", 0) >= cutoff:
                wallet = entry.get("wallet", "")
                if wallet in s.get("watched_wallets", {}):
                    w = s["watched_wallets"][wallet]
                    result.append({
                        "mint": token_mint,
                        "wallet": wallet,
                        "wallet_name": w.get("name", wallet[:8]),
                        "reputation": w.get("reputation_score", 50),
                        "entry_ts": entry.get("ts", 0),
                        "buy_usd": entry.get("buy_usd", 0),
                    })
    
    return sorted(result, key=lambda x: x["entry_ts"], reverse=True)
