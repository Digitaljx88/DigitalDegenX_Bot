"""
Heat Momentum — velocity / derivative tracking for heat scores.

Records per-token score snapshots and computes the rate of change
(score units per minute) over a rolling window. Fast-rising tokens
get a velocity label that is surfaced in alerts.

Storage: data/heat_momentum.json  (dict of mint → [(timestamp, score), ...])
Window:  WINDOW_SECS (default 300s = 5 min) — older entries pruned automatically
"""
from __future__ import annotations

import json
import os
import time

DATA_DIR   = os.path.join(os.path.dirname(__file__), "data")
STATE_FILE = os.path.join(DATA_DIR, "heat_momentum.json")
os.makedirs(DATA_DIR, exist_ok=True)

WINDOW_SECS = 300   # keep 5 minutes of history per token
PRUNE_EVERY = 60    # prune stale entries every N seconds

_cache: dict[str, list] = {}   # mint → [(ts, score), ...]
_last_save = 0.0
_last_prune = 0.0


def _load() -> None:
    global _cache
    try:
        with open(STATE_FILE) as f:
            _cache = json.load(f)
    except Exception:
        _cache = {}


def _save() -> None:
    global _last_save
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(_cache, f)
        _last_save = time.time()
    except Exception:
        pass


def _prune() -> None:
    """Remove entries older than WINDOW_SECS and tokens with no recent data."""
    global _last_prune
    cutoff = time.time() - WINDOW_SECS
    stale_mints = []
    for mint, entries in _cache.items():
        fresh = [(ts, sc) for ts, sc in entries if ts >= cutoff]
        if fresh:
            _cache[mint] = fresh
        else:
            stale_mints.append(mint)
    for m in stale_mints:
        del _cache[m]
    _last_prune = time.time()


# Load on import
_load()


def record(mint: str, score: float) -> None:
    """Record a new score snapshot for mint."""
    global _last_save, _last_prune
    now = time.time()

    if now - _last_prune > PRUNE_EVERY:
        _prune()

    entries = _cache.setdefault(mint, [])
    entries.append((now, score))

    # Auto-save every 30s
    if now - _last_save > 30:
        _save()


def get_velocity(mint: str) -> tuple[float, str]:
    """
    Compute score velocity (units/minute) using oldest vs newest snapshot
    within the rolling window.

    Returns:
        (velocity, label)
        velocity: positive = rising, negative = falling, 0 = flat/unknown
        label:    human-readable string for alerts
    """
    entries = _cache.get(mint, [])
    if len(entries) < 2:
        return 0.0, "flat (no history)"

    cutoff = time.time() - WINDOW_SECS
    recent = [(ts, sc) for ts, sc in entries if ts >= cutoff]
    if len(recent) < 2:
        return 0.0, "flat (single snapshot)"

    oldest_ts, oldest_sc = recent[0]
    newest_ts, newest_sc = recent[-1]
    elapsed_mins = (newest_ts - oldest_ts) / 60.0
    if elapsed_mins < 0.05:   # < 3 seconds apart — too close to be meaningful
        return 0.0, "flat (too recent)"

    velocity = (newest_sc - oldest_sc) / elapsed_mins   # score pts/min

    if velocity >= 15:
        label = f"🚀 SURGING +{velocity:.1f}pt/min"
    elif velocity >= 8:
        label = f"⬆️  rising +{velocity:.1f}pt/min"
    elif velocity >= 3:
        label = f"↗️  climbing +{velocity:.1f}pt/min"
    elif velocity <= -8:
        label = f"⬇️  falling {velocity:.1f}pt/min"
    elif velocity <= -3:
        label = f"↘️  cooling {velocity:.1f}pt/min"
    else:
        label = f"➡️  stable ({velocity:+.1f}pt/min)"

    return velocity, label


def velocity_score_boost(mint: str) -> int:
    """
    Return a bonus score (0-10) based on velocity.
    Adds urgency for fast-rising tokens even if their absolute score is moderate.
    Callers should cap total at 100.
    """
    velocity, _ = get_velocity(mint)
    if velocity >= 15:
        return 10
    if velocity >= 8:
        return 7
    if velocity >= 3:
        return 4
    if velocity >= 1:
        return 2
    return 0
