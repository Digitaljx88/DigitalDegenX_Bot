"""
Intelligence Tracker — Phase 6: Auto-wallet tracking + narrative intelligence.

Two systems:
  1. Auto-wallet tracking:
       - Buyers in 70+ scored tokens are auto-tracked
       - Dev/creator of high-score tokens is auto-tracked
       - Wallets appearing in 3+ high-score tokens get a boost flag
     Win tracking: a token is a "win" when price 2x+ from entry (caller updates outcome).

  2. Narrative intelligence:
       - Preset 5 categories (AI, Political, Animal, Gaming, RWA) + auto-detected patterns
       - Win rate, avg ROI, trending score per narrative
       - Trending score = appearances in last 24h × recency decay

Scoring feedback:
  - get_wallet_score_boost(wallets)   → 0.0–25.0 bonus pts for tracked high-rep wallets
  - get_narrative_score_boost(narratives) → 0.0–20.0 bonus pts for trending narratives

Storage: data/intelligence_tracker.json
"""

from __future__ import annotations

import json
import os
import time
import re
from datetime import datetime, timezone
from typing import Optional

DATA_DIR   = os.path.join(os.path.dirname(__file__), "data")
STATE_FILE = os.path.join(DATA_DIR, "intelligence_tracker.json")
os.makedirs(DATA_DIR, exist_ok=True)

# ────── Constants ──────────────────────────────────────────────────────────────

AUTO_TRACK_SCORE_THRESHOLD = 70    # Minimum heat score to trigger auto-tracking
REPEAT_APPEARANCE_THRESHOLD = 3    # Track if wallet appears in N+ high-score tokens
WIN_MULTIPLIER_THRESHOLD = 2.0     # 2x price = "win"
MAX_RECENT_TOKENS_PER_NARRATIVE = 50
MAX_TOKENS_PER_WALLET = 100

# Wallet score boost parameters
HIGH_WIN_RATE_BOOST = 25.0    # max boost for wallets with 80%+ win rate
LOW_WIN_RATE_SKIP   = 0.30    # wallets with <30% win rate give no boost

# Narrative boost parameters
TRENDING_HOURS = 24           # look-back window for trending score
MAX_NARRATIVE_BOOST = 20.0

# Preset narrative keywords (mirrors scanner.py NARRATIVES so auto-detection extends them)
PRESET_NARRATIVES: dict[str, list[str]] = {
    "AI":        ["ai", "agent", "gpt", "robot", "artificial", "neural", "llm", "ml", "agi", "chatbot", "openai"],
    "Political": ["trump", "maga", "biden", "elon", "political", "vote", "election", "congress", "senate", "potus"],
    "Animal":    ["dog", "cat", "pepe", "frog", "shib", "inu", "doge", "floki", "bonk", "bear", "wolf", "ape"],
    "Gaming":    ["game", "play", "nft", "pixel", "arcade", "quest", "rpg", "metaverse", "gamefi", "gamer"],
    "RWA":       ["gold", "oil", "real", "estate", "asset", "commodity", "silver", "land", "rwa", "property"],
}

# ────── State helpers ──────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return _empty_state()


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _empty_state() -> dict:
    return {
        "auto_wallets": {},
        "narratives": {n: _empty_narrative() for n in PRESET_NARRATIVES},
        "session_appearances": {},   # address → [mint1, mint2, ...] (this session)
    }


def _empty_wallet(reason: str, token_mint: str, heat_score: int) -> dict:
    return {
        "reason": reason,
        "auto_added_ts": int(time.time()),
        "tokens_seen": [token_mint],
        "heat_scores_seen": [heat_score],
        "appearances": 1,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "roi_avg": 0.0,
        "reputation": 50,
        "is_repeat": False,
    }


def _empty_narrative() -> dict:
    return {
        "total_tokens": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "avg_roi": 0.0,
        "trending_score": 0.0,
        "last_seen_ts": 0.0,
        "recent_tokens": [],   # [{mint, heat_score, ts}]
        "custom_keywords": [], # auto-detected new keywords for this narrative
    }


# ────── Auto-wallet tracking ──────────────────────────────────────────────────

def auto_track_wallet(
    address: str,
    reason: str,
    token_mint: str,
    heat_score: int,
) -> bool:
    """
    Record that `address` appeared in a high-score token.
    reason: 'high_score_buyer' | 'dev_wallet' | 'repeat_appearance'
    Returns True if newly added, False if already tracked.
    """
    if not address or not token_mint:
        return False

    state = _load_state()
    wallets = state.setdefault("auto_wallets", {})
    session = state.setdefault("session_appearances", {})

    # Track session appearances per wallet
    appearances_list = session.setdefault(address, [])
    if token_mint not in appearances_list:
        appearances_list.append(token_mint)

    if address in wallets:
        w = wallets[address]
        # Update existing record
        if token_mint not in w["tokens_seen"]:
            w["tokens_seen"] = (w["tokens_seen"] + [token_mint])[-MAX_TOKENS_PER_WALLET:]
            w["heat_scores_seen"] = (w["heat_scores_seen"] + [heat_score])[-MAX_TOKENS_PER_WALLET:]
            w["appearances"] += 1
        # Upgrade reason if repeat
        if w["appearances"] >= REPEAT_APPEARANCE_THRESHOLD and not w.get("is_repeat"):
            w["is_repeat"] = True
            w["reason"] = "repeat_appearance"
        _save_state(state)
        return False

    # New wallet — add it
    wallets[address] = _empty_wallet(reason, token_mint, heat_score)
    # Check if already repeat from session history
    if len(appearances_list) >= REPEAT_APPEARANCE_THRESHOLD:
        wallets[address]["is_repeat"] = True
        wallets[address]["reason"] = "repeat_appearance"

    _save_state(state)
    return True


def get_auto_tracked_wallets() -> dict:
    """Return full auto-tracked wallet dict."""
    return _load_state().get("auto_wallets", {})


def is_auto_tracked(address: str) -> bool:
    return address in _load_state().get("auto_wallets", {})


def get_wallet_record(address: str) -> Optional[dict]:
    return _load_state().get("auto_wallets", {}).get(address)


def record_token_outcome(
    token_mint: str,
    outcome: str,            # 'win' | 'loss'
    roi_multiplier: float,   # e.g. 3.5 means 3.5x
) -> int:
    """
    Mark a token outcome for all auto-tracked wallets that held it.
    Also updates narrative stats for the token.
    Returns count of wallets updated.
    """
    state = _load_state()
    wallets = state.get("auto_wallets", {})
    updated = 0

    for addr, w in wallets.items():
        if token_mint in w.get("tokens_seen", []):
            if outcome == "win":
                w["wins"] = w.get("wins", 0) + 1
            else:
                w["losses"] = w.get("losses", 0) + 1

            total = w.get("wins", 0) + w.get("losses", 0)
            w["win_rate"] = round(w["wins"] / total, 3) if total else 0.0

            # Update avg ROI with incremental average
            prev_avg = w.get("roi_avg", 0.0)
            w["roi_avg"] = round(prev_avg + (roi_multiplier - prev_avg) / total, 2) if total else roi_multiplier

            # Recalculate reputation (50 base + win_rate contribution + roi bonus)
            roi_bonus  = min(20, max(0, (w["roi_avg"] - 1.0) * 5))
            w["reputation"] = min(100, int(50 + w["win_rate"] * 30 + roi_bonus))
            updated += 1

    _save_state(state)
    return updated


# ────── Narrative intelligence ────────────────────────────────────────────────

def detect_narratives(name: str, symbol: str, description: str) -> list[str]:
    """
    Match token text against all known narrative categories.
    Returns list of matched narrative names (may be empty).
    """
    text = f"{name} {symbol} {description}".lower()
    matched = []

    state = _load_state()
    narratives = state.get("narratives", {})

    for narrative_name, kws in {**PRESET_NARRATIVES, **_custom_keyword_map(narratives)}.items():
        hits = sum(1 for kw in kws if kw in text)
        if hits >= 1:
            matched.append(narrative_name)

    return matched


def _custom_keyword_map(narratives: dict) -> dict[str, list[str]]:
    """Build keyword map including any auto-detected custom keywords."""
    result = {}
    for name, data in narratives.items():
        custom = data.get("custom_keywords", [])
        if custom:
            result[name] = custom
    return result


def update_narrative_on_token(
    token: dict,
    heat_score: int,
    outcome: Optional[str] = None,    # 'win' | 'loss' | None (not yet resolved)
    roi_multiplier: float = 0.0,
) -> list[str]:
    """
    Record a token's narrative hit. Called after calculate_heat_score().
    Returns list of matched narrative names.
    """
    name   = token.get("name", "")
    symbol = token.get("symbol", "")
    desc   = token.get("description", "")
    mint   = token.get("mint", "")

    matched = detect_narratives(name, symbol, desc)
    if not matched:
        return []

    state = _load_state()
    narratives = state.setdefault("narratives", {})
    now = time.time()

    for narr_name in matched:
        narr = narratives.setdefault(narr_name, _empty_narrative())
        narr["total_tokens"] = narr.get("total_tokens", 0) + 1
        narr["last_seen_ts"] = now

        # Record recent token
        recent = narr.setdefault("recent_tokens", [])
        recent.append({"mint": mint, "heat_score": heat_score, "ts": now})
        narr["recent_tokens"] = recent[-MAX_RECENT_TOKENS_PER_NARRATIVE:]

        # Update outcome stats if provided
        if outcome == "win":
            narr["wins"] = narr.get("wins", 0) + 1
        elif outcome == "loss":
            narr["losses"] = narr.get("losses", 0) + 1

        total = narr.get("wins", 0) + narr.get("losses", 0)
        if total:
            narr["win_rate"] = round(narr["wins"] / total, 3)

        if roi_multiplier and total:
            prev = narr.get("avg_roi", 0.0)
            narr["avg_roi"] = round(prev + (roi_multiplier - prev) / total, 2)

        # Recalculate trending score: appearances in last 24h weighted by recency
        cutoff = now - TRENDING_HOURS * 3600
        recent_entries = [e for e in narr["recent_tokens"] if e["ts"] >= cutoff]
        # Recency weight: 1.0 for now → 0.1 at cutoff
        trending = sum(
            0.1 + 0.9 * ((e["ts"] - cutoff) / (TRENDING_HOURS * 3600))
            for e in recent_entries
        )
        narr["trending_score"] = round(trending, 2)

    # Auto-detect new narrative patterns from description words
    _auto_detect_narratives(desc, matched, state)

    _save_state(state)
    return matched


def _auto_detect_narratives(description: str, already_matched: list[str], state: dict) -> None:
    """
    If a description contains repeated meaningful tokens not in any preset,
    add them as custom keywords to the closest matching narrative or a new one.
    This is a simple frequency-based detector — only fires for descriptions with
    repeated meaningful words (3+ chars, not stop words).
    """
    STOP_WORDS = {
        "the", "and", "for", "are", "but", "not", "you", "all", "can", "her",
        "was", "one", "our", "out", "day", "get", "has", "him", "his", "how",
        "its", "now", "did", "she", "via", "win", "new", "big", "top", "hot",
        "buy", "sell", "hold", "token", "coin", "pump", "moon", "100x", "gem",
    }
    words = re.findall(r'\b[a-z]{3,}\b', description.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w not in STOP_WORDS:
            freq[w] = freq.get(w, 0) + 1

    # Words appearing 2+ times in description — potential narrative signal
    candidates = [w for w, c in freq.items() if c >= 2]
    if not candidates:
        return

    narratives = state.setdefault("narratives", {})
    for candidate in candidates[:5]:  # cap at 5 per token
        # Check if already in any preset
        already_known = any(
            candidate in kws for kws in PRESET_NARRATIVES.values()
        ) or any(
            candidate in n.get("custom_keywords", []) for n in narratives.values()
        )
        if already_known:
            continue

        # Add to the best-matched existing narrative or create "Trending" bucket
        target_narr = already_matched[0] if already_matched else "Trending"
        narr = narratives.setdefault(target_narr, _empty_narrative())
        custom = narr.setdefault("custom_keywords", [])
        if candidate not in custom:
            custom.append(candidate)
            if len(custom) > 30:
                custom.pop(0)  # keep max 30


def get_narrative_stats() -> dict:
    """Return full narrative stats dict."""
    return _load_state().get("narratives", {})


def get_trending_narratives(limit: int = 5) -> list[tuple[str, float]]:
    """Return list of (name, trending_score) sorted by trending_score desc."""
    narratives = get_narrative_stats()
    scored = [(name, data.get("trending_score", 0.0)) for name, data in narratives.items()]
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]


# ────── Scoring feedback (boost points) ──────────────────────────────────────

def get_wallet_score_boost(wallet_addresses: list[str]) -> float:
    """
    Return bonus score points (0–25) based on tracked wallets with good win rates.
    Called from calculate_heat_score() with the top-holder addresses.
    """
    if not wallet_addresses:
        return 0.0

    state = _load_state()
    wallets = state.get("auto_wallets", {})

    best_boost = 0.0
    for addr in wallet_addresses:
        w = wallets.get(addr)
        if not w:
            continue
        win_rate = w.get("win_rate", 0.0)
        appearances = w.get("appearances", 0)
        if win_rate < LOW_WIN_RATE_SKIP or appearances < 2:
            continue
        # Scale: 30% win rate → 0pts, 80% win rate → 25pts with diminishing returns
        raw = (win_rate - LOW_WIN_RATE_SKIP) / (0.80 - LOW_WIN_RATE_SKIP)
        boost = min(HIGH_WIN_RATE_BOOST, raw * HIGH_WIN_RATE_BOOST)
        # Weight by repeat appearances (more sightings = more confidence)
        confidence = min(1.0, appearances / 5)
        boost = round(boost * confidence, 1)
        best_boost = max(best_boost, boost)

    return best_boost


def get_narrative_score_boost(matched_narratives: list[str]) -> float:
    """
    Return bonus score points (0–20) if matched narratives are trending.
    """
    if not matched_narratives:
        return 0.0

    narratives = get_narrative_stats()
    best_boost = 0.0

    for narr_name in matched_narratives:
        narr = narratives.get(narr_name, {})
        trending = narr.get("trending_score", 0.0)
        win_rate = narr.get("win_rate", 0.0)
        total = narr.get("total_tokens", 0)

        if total < 3:
            # Not enough data — give small trending bonus only
            boost = min(5.0, trending * 1.0)
        else:
            # trending_score * win_rate factor capped at MAX_NARRATIVE_BOOST
            boost = min(MAX_NARRATIVE_BOOST, trending * (1.0 + win_rate))

        best_boost = max(best_boost, round(boost, 1))

    return best_boost


# ────── Token scoring integration ────────────────────────────────────────────

def process_scored_token(
    token: dict,
    rc: dict,
    heat_score: int,
) -> dict:
    """
    Called after calculate_heat_score() for every token that passes filters.
    Does three things:
      1. Updates narrative stats for this token
      2. Auto-tracks dev wallet if score >= threshold
      3. Auto-tracks top buyers (top holders from RugCheck) if score >= threshold
    Returns dict with keys: 'narratives', 'new_wallets', 'boost_data'
    """
    mint   = token.get("mint", "")
    name   = token.get("name", "")
    symbol = token.get("symbol", "")
    desc   = token.get("description", "")

    # 1. Update narrative stats
    matched_narratives = update_narrative_on_token(token, heat_score)

    new_wallets: list[dict] = []

    if heat_score >= AUTO_TRACK_SCORE_THRESHOLD:
        # 2. Auto-track dev wallet
        creator = rc.get("creator", "")
        if creator:
            added = auto_track_wallet(creator, "dev_wallet", mint, heat_score)
            if added:
                new_wallets.append({"address": creator, "reason": "dev_wallet"})

        # 3. Auto-track top buyers (top holders from RugCheck)
        top_holders = rc.get("topHolders") or []
        for holder in top_holders[:10]:
            addr = holder.get("address") or holder.get("owner", "")
            if not addr or addr == creator:
                continue
            pct = float(holder.get("pct", 0))
            # Skip if same as dev (already tracked), skip if very large holder (likely program)
            if pct > 30:
                continue
            added = auto_track_wallet(addr, "high_score_buyer", mint, heat_score)
            if added:
                new_wallets.append({"address": addr, "reason": "high_score_buyer"})

    # 4. Compute boosts for reporter
    wallet_addrs = [h.get("address") or h.get("owner", "") for h in (rc.get("topHolders") or [])]
    wallet_boost    = get_wallet_score_boost(wallet_addrs)
    narrative_boost = get_narrative_score_boost(matched_narratives)

    return {
        "narratives":      matched_narratives,
        "new_wallets":     new_wallets,
        "wallet_boost":    wallet_boost,
        "narrative_boost": narrative_boost,
    }


# ────── Display helpers ───────────────────────────────────────────────────────

def format_wallet_intelligence(page: int = 0, page_size: int = 10) -> str:
    """Format auto-tracked wallet stats for Telegram display."""
    wallets = get_auto_tracked_wallets()
    if not wallets:
        return "🤖 *Auto-Wallet Intelligence*\n\nNo wallets auto-tracked yet.\nThey appear automatically when tokens score 70+."

    # Sort by reputation desc, then appearances
    sorted_wallets = sorted(
        wallets.items(),
        key=lambda x: (x[1].get("reputation", 50), x[1].get("appearances", 0)),
        reverse=True,
    )

    total = len(sorted_wallets)
    start = page * page_size
    end   = min(start + page_size, total)
    page_wallets = sorted_wallets[start:end]

    lines = [f"🤖 *Auto-Wallet Intelligence* ({total} tracked)\n"]
    for addr, w in page_wallets:
        short   = f"{addr[:4]}...{addr[-4:]}"
        reason  = {"dev_wallet": "👨‍💻 Dev", "high_score_buyer": "💰 Buyer", "repeat_appearance": "🔁 Repeat"}.get(w.get("reason", ""), "❓")
        rep     = w.get("reputation", 50)
        wr      = w.get("win_rate", 0.0)
        apps    = w.get("appearances", 0)
        roi     = w.get("roi_avg", 0.0)
        star    = "⭐" if w.get("is_repeat") else ""
        rep_bar = "🟢" if rep >= 70 else "🟡" if rep >= 50 else "🔴"
        lines.append(
            f"{rep_bar} `{short}` {reason} {star}\n"
            f"   Rep: *{rep}* | WR: *{wr:.0%}* | ROI: *{roi:.1f}x* | Seen: *{apps}x*"
        )

    if total > page_size:
        lines.append(f"\n_Page {page+1}/{(total-1)//page_size+1}_")

    return "\n".join(lines)


def format_narrative_intelligence() -> str:
    """Format narrative stats for Telegram display."""
    narratives = get_narrative_stats()
    if not narratives:
        return "📊 *Narrative Intelligence*\n\nNo narrative data yet. Start scanning to collect stats."

    # Sort by trending score desc
    sorted_narr = sorted(
        narratives.items(),
        key=lambda x: x[1].get("trending_score", 0.0),
        reverse=True,
    )

    trending = get_trending_narratives(3)
    top_names = [n for n, _ in trending if _ > 0]

    lines = ["📊 *Narrative Intelligence*\n"]

    for name, data in sorted_narr:
        total   = data.get("total_tokens", 0)
        wr      = data.get("win_rate", 0.0)
        roi     = data.get("avg_roi", 0.0)
        trend   = data.get("trending_score", 0.0)
        custom  = data.get("custom_keywords", [])

        # Skip empty narratives
        if total == 0 and not custom:
            continue

        fire = "🔥" if name in top_names else ("📈" if trend > 1 else "📉")
        wr_str  = f"{wr:.0%}" if total >= 3 else "N/A"
        roi_str = f"{roi:.1f}x" if roi else "N/A"

        lines.append(f"{fire} *{name}*  _(trend: {trend:.1f})_")
        lines.append(f"   Tokens: *{total}* | WR: *{wr_str}* | ROI: *{roi_str}*")
        if custom:
            kw_preview = ", ".join(custom[:5])
            lines.append(f"   🆕 New keywords: `{kw_preview}`")

    if not any(d.get("total_tokens", 0) for _, d in sorted_narr):
        lines.append("No token data yet — keep scanning to build stats.")

    return "\n".join(lines)


def format_top_wallet_performers(limit: int = 5) -> str:
    """Short summary of top performing auto-tracked wallets."""
    wallets = get_auto_tracked_wallets()
    if not wallets:
        return "_No auto-tracked wallets yet._"

    # Filter to wallets with resolved outcomes
    resolved = {a: w for a, w in wallets.items() if w.get("wins", 0) + w.get("losses", 0) >= 2}
    if not resolved:
        return "_Insufficient outcome data. Wallets are tracked — outcomes update as tokens resolve._"

    sorted_w = sorted(resolved.items(), key=lambda x: -x[1].get("reputation", 50))
    lines = []
    for addr, w in sorted_w[:limit]:
        short = f"{addr[:4]}...{addr[-4:]}"
        lines.append(
            f"  `{short}` Rep *{w['reputation']}* | WR *{w['win_rate']:.0%}* | ROI *{w['roi_avg']:.1f}x*"
        )
    return "\n".join(lines)
