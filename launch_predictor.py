"""
Launch Predictor — Phase 5 predictive launch detection.

Matches incoming token fingerprints against historical high-performing
launch archetypes and returns a confidence-weighted prediction.

Architecture:
  1. ARCHETYPES — 6 predefined pattern templates (narrative, volume shape,
     wallet signal, holder profile, DEX, age window).
  2. Feature extraction — pulls numeric features from the scanner token dict
     + RugCheck dict + heat score breakdown.
  3. Archetype matching — cosine-style weighted feature comparison returns
     a "playbook match" score 0–100 per archetype.
  4. Historical calibration — reads scanner_log.json to compute win rates per
     archetype from past alerted tokens, updating ARCHETYPE_STATS cache.
  5. Prediction boost — +0/+5/+10 added to heat score when high confidence.

Persistence:
  - data/launch_patterns.json  — learned archetype win stats (refreshed daily)
  - data/prediction_log.json   — last 300 predictions for /playbook review
"""

import json
import os
import time

DATA_DIR             = os.path.join(os.path.dirname(__file__), "data")
PATTERNS_FILE        = os.path.join(DATA_DIR, "launch_patterns.json")
PREDICTION_LOG_FILE  = os.path.join(DATA_DIR, "prediction_log.json")
SCANNER_LOG_FILE     = os.path.join(DATA_DIR, "scanner_log.json")
os.makedirs(DATA_DIR, exist_ok=True)

STATS_REFRESH_SECS  = 7200   # rebuild archetype stats every 2h
MAX_PRED_LOG        = 300


# ─── Archetype definitions ────────────────────────────────────────────────────
#
# Each archetype is a feature template with:
#   - required: hard-filter (must match for archetype to fire at all)
#   - features: {feature_name: (ideal_value, weight)}
#     Feature values are normalised to 0.0–1.0 before comparison.
#   - description: human-readable label
#   - emoji: display emoji

ARCHETYPES = {
    "AI_CLUSTER": {
        "emoji":       "🤖",
        "description": "AI narrative + smart wallet co-investment cluster",
        "required":    {"narrative": "AI"},
        "features": {
            "momentum_pts":  (20, 2.5),   # ideal=20pts  weight=2.5
            "cluster_pts":   (15, 3.0),   # heavy weight on cluster signal
            "wallet_rep_pts":(10, 2.0),
            "bundle_clean":  (1,  2.0),   # 1=clean, 0=bundled
            "twitter":       (1,  1.0),
            "holders_norm":  (0.5, 1.0),  # 0–1 normalised holder count
        },
    },
    "MEME_VIRAL": {
        "emoji":       "🐸",
        "description": "Animal/meme narrative with extreme volume velocity",
        "required":    {"narrative_any": ["Animal", "Gaming"]},
        "features": {
            "momentum_pts":  (20, 3.0),
            "twitter":       (1,  2.0),
            "age_mins_inv":  (0.9, 1.5),  # younger is better (inverse age score)
            "mcap_norm":     (0.15, 1.0), # sweet spot: ~$150k
            "bundle_clean":  (1,   1.5),
        },
    },
    "POLITICAL_SPIKE": {
        "emoji":       "🇺🇸",
        "description": "Political narrative riding a trending news cycle",
        "required":    {"narrative": "Political"},
        "features": {
            "momentum_pts":  (15, 2.5),
            "twitter":       (1,  3.0),   # Twitter is critical for political
            "narrative_pts": (15, 2.0),
            "age_mins_inv":  (0.8, 2.0),
            "holders_norm":  (0.4, 1.0),
        },
    },
    "STEALTH_RAYDIUM": {
        "emoji":       "🥷",
        "description": "Quiet Raydium migration before the crowd notices",
        "required":    {"dex": "raydium"},
        "features": {
            "age_mins_inv":  (0.7, 2.5),  # <60 min since migration
            "bundle_clean":  (1,   2.5),
            "holders_norm":  (0.3, 1.5),  # still low-holder phase
            "momentum_pts":  (10,  2.0),
            "dev_pts":       (10,  1.5),
        },
    },
    "CLEAN_WHALE_ENTRY": {
        "emoji":       "🐋",
        "description": "Smart wallet cluster entry on a clean, holder-distributed token",
        "required":    {"min_wallet_signal": 5},
        "features": {
            "wallet_rep_pts": (15, 3.5),
            "cluster_pts":    (15, 3.0),
            "bundle_clean":   (1,  3.0),
            "holders_norm":   (0.6, 1.5),
            "dev_pts":        (10,  2.0),
        },
    },
    "MICRO_ROCKETSHIP": {
        "emoji":       "🚀",
        "description": "Ultra-low mcap with extreme volume spike — pre-discovery gem",
        "required":    {"max_mcap": 100_000},
        "features": {
            "momentum_pts":  (20, 3.0),
            "mcap_norm":     (0.05, 2.0),  # ideal = very small mcap
            "age_mins_inv":  (0.95, 2.5),
            "bundle_clean":  (1,    2.0),
            "liquidity_pts": (10,   1.5),
        },
    },
}


# ─── Pattern stats persistence ────────────────────────────────────────────────

def _load_patterns() -> dict:
    try:
        with open(PATTERNS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"stats": {}, "last_built": 0}


def _save_patterns(p: dict):
    with open(PATTERNS_FILE, "w") as f:
        json.dump(p, f, indent=2)


def _load_pred_log() -> list:
    try:
        with open(PREDICTION_LOG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _append_pred_log(entry: dict):
    log = _load_pred_log()
    log.append(entry)
    if len(log) > MAX_PRED_LOG:
        log = log[-MAX_PRED_LOG:]
    with open(PREDICTION_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


# ─── Historical stats builder ─────────────────────────────────────────────────

def _build_archetype_stats() -> dict:
    """
    Read scanner_log.json and build win-rate stats per archetype.

    An "alerted" token (score >= 70, alerted=True) is classified into an
    archetype based on its logged narrative + rough mcap/score features.
    A "win" = alerted + not disqualified.

    Returns {archetype_name: {total: N, wins: N, win_rate: 0.0-1.0, avg_score: F}}
    """
    stats: dict[str, dict] = {k: {"total": 0, "wins": 0, "avg_score": 0.0} for k in ARCHETYPES}

    try:
        with open(SCANNER_LOG_FILE) as f:
            log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return stats

    for entry in log:
        score     = entry.get("score", 0)
        narrative = entry.get("narrative", "Other")
        mcap      = entry.get("mcap", 0)
        alerted   = entry.get("alerted", False)
        dq        = entry.get("dq")

        if score < 40:
            continue

        # Classify into archetype using simple heuristic
        archetype = _classify_log_entry(narrative, mcap, score, alerted, dq)
        if archetype not in stats:
            continue

        stats[archetype]["total"] += 1
        if alerted and not dq:
            stats[archetype]["wins"] += 1
            prev_avg = stats[archetype]["avg_score"]
            n        = stats[archetype]["wins"]
            stats[archetype]["avg_score"] = round(((prev_avg * (n - 1)) + score) / n, 1)

    # Compute win rates
    for k, v in stats.items():
        t = v["total"]
        v["win_rate"] = round(v["wins"] / t, 3) if t else 0.0

    return stats


def _classify_log_entry(narrative: str, mcap: float, score: int, alerted: bool, dq) -> str:
    """Map a historical log entry to its most likely archetype."""
    if mcap and mcap <= 100_000:
        return "MICRO_ROCKETSHIP"
    if narrative == "AI":
        return "AI_CLUSTER"
    if narrative == "Political":
        return "POLITICAL_SPIKE"
    if narrative in ("Animal", "Gaming"):
        return "MEME_VIRAL"
    if score >= 70 and alerted and not dq:
        return "CLEAN_WHALE_ENTRY"
    return "STEALTH_RAYDIUM"


def get_archetype_stats(force: bool = False) -> dict:
    """Return archetype stats, rebuilding if stale (2h TTL)."""
    p = _load_patterns()
    if force or (time.time() - p.get("last_built", 0)) > STATS_REFRESH_SECS:
        p["stats"]      = _build_archetype_stats()
        p["last_built"] = time.time()
        _save_patterns(p)
    return p["stats"]


# ─── Feature extraction ───────────────────────────────────────────────────────

def _extract_features(token: dict, rc: dict, breakdown: dict) -> dict:
    """
    Extract normalised feature vector from a token+rc+breakdown for archetype matching.

    All values are in 0.0–1.0 (or raw pts where noted, normalised inside scoring).
    """
    # From breakdown (raw pts, 0–20)
    mom_pts  = breakdown.get("momentum",   (0,))[0]
    liq_pts  = breakdown.get("liquidity",  (0,))[0]
    wal_pts  = breakdown.get("wallet_rep", (0,))[0]
    clu_pts  = breakdown.get("cluster",    (0,))[0]
    twit_pts = breakdown.get("twitter",    (0,))[0]
    narr_pts = breakdown.get("narrative",  (0,))[0]
    migr_pts = breakdown.get("migration",  (0,))[0]
    dev_pts  = breakdown.get("dev",        (0,))[0]
    bund_pts = breakdown.get("bundle",     (0,))[0]
    narr_rsn = breakdown.get("narrative",  (0, ""))[1].lower()

    # Narrative label
    narrative = "Other"
    for n in ("AI", "Political", "Animal", "Gaming", "RWA"):
        if n.lower() in narr_rsn:
            narrative = n
            break

    # DEX
    dex = (token.get("dex") or "").lower()

    # Mcap normalised to 0–1 over $0–$10M range
    mcap = float(token.get("mcap", 0) or 0)
    mcap_norm = min(1.0, mcap / 10_000_000)

    # Age normalised (younger → higher inv score)
    pair_created = token.get("pair_created", 0) or 0
    now_ms       = time.time() * 1000
    age_mins     = max(0, (now_ms - pair_created) / 60_000) if pair_created else 9999
    age_mins_inv = max(0.0, 1.0 - min(1.0, age_mins / 240))  # 0 → 1.0, 240min → 0.0

    # Holder count normalised 0–1 over 0–500 range
    holders      = int(rc.get("totalHolders", 0) or 0)
    holders_norm = min(1.0, holders / 500)

    # Bundle clean flag (1 = clean/no penalty)
    bundle_clean = 1.0 if bund_pts >= 0 else 0.0

    # Twitter presence
    twitter = 1.0 if twit_pts > 0 else 0.0

    # Combined wallet signal for required check
    wallet_signal = wal_pts + clu_pts

    return {
        "narrative":      narrative,
        "dex":            dex,
        "mcap":           mcap,
        "mcap_norm":      mcap_norm,
        "age_mins":       age_mins,
        "age_mins_inv":   age_mins_inv,
        "holders":        holders,
        "holders_norm":   holders_norm,
        "momentum_pts":   mom_pts,
        "liquidity_pts":  liq_pts,
        "wallet_rep_pts": wal_pts,
        "cluster_pts":    clu_pts,
        "twitter":        twitter,
        "narrative_pts":  narr_pts,
        "migration_pts":  migr_pts,
        "dev_pts":        dev_pts,
        "bundle_clean":   bundle_clean,
        "wallet_signal":  wallet_signal,
    }


# ─── Archetype matching ───────────────────────────────────────────────────────

def _check_required(features: dict, archetype_def: dict) -> bool:
    """Check if hard required conditions are met for an archetype."""
    req = archetype_def.get("required", {})
    for key, val in req.items():
        if key == "narrative":
            if features.get("narrative") != val:
                return False
        elif key == "narrative_any":
            if features.get("narrative") not in val:
                return False
        elif key == "dex":
            if val not in features.get("dex", ""):
                return False
        elif key == "max_mcap":
            if features.get("mcap", 0) > val:
                return False
        elif key == "min_wallet_signal":
            if features.get("wallet_signal", 0) < val:
                return False
    return True


def _score_archetype(features: dict, archetype_def: dict) -> float:
    """
    Score how well features match an archetype (0.0–1.0).

    Each feature (f_name) has an ideal value and a weight.
    Feature values are normalised to 0–1 before comparison.
    Score = weighted sum of per-feature matches / total weight.
    """
    feat_defs  = archetype_def.get("features", {})
    total_wt   = 0.0
    weighted   = 0.0

    # Normalisation ranges for raw-pt features (max pts each category can score)
    pt_maxes = {
        "momentum_pts":   20.0,
        "liquidity_pts":  10.0,
        "wallet_rep_pts": 15.0,
        "cluster_pts":    15.0,
        "narrative_pts":  15.0,
        "migration_pts":  10.0,
        "dev_pts":        10.0,
    }

    for f_name, (ideal, weight) in feat_defs.items():
        raw = features.get(f_name, 0)

        # Normalise raw points
        if f_name in pt_maxes:
            norm_val = raw / pt_maxes[f_name]
            norm_ideal = ideal / pt_maxes[f_name]
        else:
            norm_val   = float(raw)
            norm_ideal = float(ideal)

        # Similarity: 1.0 = perfect match, penalise deviation
        diff      = abs(norm_val - norm_ideal)
        similarity = max(0.0, 1.0 - diff)
        weighted  += similarity * weight
        total_wt  += weight

    return (weighted / total_wt) if total_wt else 0.0


# ─── Main prediction function ─────────────────────────────────────────────────

def predict_launch(token: dict, rc: dict, breakdown: dict) -> dict:
    """
    Run predictive launch detection for a token.

    Returns:
    {
      "archetype":       str,         # best matching archetype key
      "archetype_label": str,         # human-readable label
      "emoji":           str,
      "confidence":      int,         # 0–100
      "boost":           int,         # +0/+5/+10 heat score modifier
      "reason":          str,
      "win_rate":        float,       # historical win rate for this archetype
      "avg_score":       float,       # historical avg alert score
      "all_scores":      dict,        # {archetype: 0–100} for all archetypes
    }
    """
    features = _extract_features(token, rc, breakdown)
    stats    = get_archetype_stats()

    best_archetype = None
    best_score     = 0.0
    all_scores     = {}

    for arch_key, arch_def in ARCHETYPES.items():
        if not _check_required(features, arch_def):
            all_scores[arch_key] = 0
            continue
        match = _score_archetype(features, arch_def)
        match_pct = round(match * 100)
        all_scores[arch_key] = match_pct
        if match_pct > best_score:
            best_score     = match_pct
            best_archetype = arch_key

    if best_archetype is None or best_score < 30:
        result = {
            "archetype":       "NONE",
            "archetype_label": "No pattern match",
            "emoji":           "⚪",
            "confidence":      0,
            "boost":           0,
            "reason":          "Token doesn't match any known launch archetype",
            "win_rate":        0.0,
            "avg_score":       0.0,
            "all_scores":      all_scores,
        }
        _append_pred_log({
            "ts":        time.time(),
            "mint":      token.get("mint", ""),
            "archetype": "NONE",
            "confidence": 0,
            "boost":      0,
        })
        return result

    arch_def   = ARCHETYPES[best_archetype]
    arch_stats = stats.get(best_archetype, {})
    win_rate   = arch_stats.get("win_rate", 0.0)
    avg_score  = arch_stats.get("avg_score", 0.0)

    # Confidence = archetype match × historical win rate boost
    # If no historical data yet, use raw match score only
    if arch_stats.get("total", 0) >= 5:
        confidence = round(best_score * 0.6 + win_rate * 100 * 0.4)
    else:
        confidence = round(best_score * 0.8)

    confidence = min(100, confidence)

    # Boost tier
    if confidence >= 70:
        boost  = 10
        reason = f"{arch_def['emoji']} *{arch_def['description']}* (conf {confidence}%, hist win {win_rate:.0%})"
    elif confidence >= 50:
        boost  = 5
        reason = f"{arch_def['emoji']} Partial match: {arch_def['description']} (conf {confidence}%)"
    else:
        boost  = 0
        reason = f"{arch_def['emoji']} Weak {arch_def['description']} pattern (conf {confidence}%)"

    _append_pred_log({
        "ts":         time.time(),
        "mint":       token.get("mint", ""),
        "name":       token.get("name", ""),
        "archetype":  best_archetype,
        "confidence": confidence,
        "boost":      boost,
        "win_rate":   win_rate,
    })

    return {
        "archetype":       best_archetype,
        "archetype_label": arch_def["description"],
        "emoji":           arch_def["emoji"],
        "confidence":      confidence,
        "boost":           boost,
        "reason":          reason,
        "win_rate":        win_rate,
        "avg_score":       avg_score,
        "all_scores":      all_scores,
    }


# ─── Playbook summary ─────────────────────────────────────────────────────────

def get_playbook_summary() -> dict:
    """
    Return a summary of current playbook state for the /playbook command:
      - Archetype win rates (sorted best first)
      - Recent predictions (last 10)
      - Current best-bet archetype
    """
    stats          = get_archetype_stats()
    pred_log       = _load_pred_log()
    recent_preds   = sorted(pred_log, key=lambda x: -x.get("ts", 0))[:10]

    # Rank archetypes by: win_rate * sqrt(total) (confidence-weighted)
    ranked = []
    import math
    for arch_key, s in stats.items():
        total    = s.get("total", 0)
        win_rate = s.get("win_rate", 0.0)
        if total == 0:
            ranked.append({"key": arch_key, "rank_score": 0.0, **s,
                           "emoji": ARCHETYPES[arch_key]["emoji"],
                           "description": ARCHETYPES[arch_key]["description"]})
        else:
            rank_score = win_rate * math.sqrt(total)
            ranked.append({"key": arch_key, "rank_score": rank_score,
                           "emoji": ARCHETYPES[arch_key]["emoji"],
                           "description": ARCHETYPES[arch_key]["description"],
                           **s})
    ranked.sort(key=lambda x: -x["rank_score"])

    best_bet = ranked[0] if ranked and ranked[0]["rank_score"] > 0 else None

    return {
        "ranked_archetypes": ranked,
        "best_bet":          best_bet,
        "recent_predictions": recent_preds,
        "stats_last_built":  _load_patterns().get("last_built", 0),
    }


def force_rebuild_stats():
    """Force rebuild archetype stats from log. Use after large scan batches."""
    return get_archetype_stats(force=True)
