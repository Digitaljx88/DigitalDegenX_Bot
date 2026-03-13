from __future__ import annotations

from copy import deepcopy
import time


EXIT_PROFILE_DEFAULTS = {
    "ai_runner": {
        "first_risk_off": {
            "enabled": True,
            "activate_mult": 1.6,
            "sell_pct": 30,
            "tighten_trailing": True,
            "tighten_to_pct": 15,
            "triggered": False,
        },
        "velocity_rollover": {
            "enabled": True,
            "activate_mult": 1.5,
            "sell_pct": 40,
            "min_score_drop": 10,
            "min_velocity": -2.0,
            "check_cooldown_secs": 180,
            "peak_score": 0.0,
            "last_score": 0.0,
            "last_velocity": 0.0,
            "last_check_ts": 0.0,
            "triggered": False,
        },
        "post_partial_trail_pct": 15,
    },
    "political_spike": {
        "first_risk_off": {
            "enabled": True,
            "activate_mult": 1.5,
            "sell_pct": 35,
            "tighten_trailing": True,
            "tighten_to_pct": 14,
            "triggered": False,
        },
        "velocity_rollover": {
            "enabled": True,
            "activate_mult": 1.4,
            "sell_pct": 50,
            "min_score_drop": 8,
            "min_velocity": -2.0,
            "check_cooldown_secs": 150,
            "peak_score": 0.0,
            "last_score": 0.0,
            "last_velocity": 0.0,
            "last_check_ts": 0.0,
            "triggered": False,
        },
        "post_partial_trail_pct": 14,
    },
    "meme_runner": {
        "first_risk_off": {
            "enabled": True,
            "activate_mult": 1.75,
            "sell_pct": 30,
            "tighten_trailing": True,
            "tighten_to_pct": 18,
            "triggered": False,
        },
        "velocity_rollover": {
            "enabled": True,
            "activate_mult": 1.6,
            "sell_pct": 35,
            "min_score_drop": 12,
            "min_velocity": -2.5,
            "check_cooldown_secs": 180,
            "peak_score": 0.0,
            "last_score": 0.0,
            "last_velocity": 0.0,
            "last_check_ts": 0.0,
            "triggered": False,
        },
        "post_partial_trail_pct": 18,
    },
    "balanced": {
        "first_risk_off": {
            "enabled": True,
            "activate_mult": 2.0,
            "sell_pct": 25,
            "tighten_trailing": True,
            "tighten_to_pct": 20,
            "triggered": False,
        },
        "velocity_rollover": {
            "enabled": True,
            "activate_mult": 1.75,
            "sell_pct": 35,
            "min_score_drop": 12,
            "min_velocity": -3.0,
            "check_cooldown_secs": 180,
            "peak_score": 0.0,
            "last_score": 0.0,
            "last_velocity": 0.0,
            "last_check_ts": 0.0,
            "triggered": False,
        },
        "post_partial_trail_pct": 20,
    },
}


def narrative_exit_profile(narrative: str | None) -> tuple[str, dict]:
    label = str(narrative or "Other").lower()
    if label == "political":
        profile = "political_spike"
    elif label == "ai":
        profile = "ai_runner"
    elif label in {"animal", "gaming"}:
        profile = "meme_runner"
    else:
        profile = "balanced"
    return profile, deepcopy(EXIT_PROFILE_DEFAULTS[profile])


def ensure_exit_blocks(cfg: dict, *, narrative: str | None = None,
                       entry_score_effective: float | int | None = None) -> bool:
    profile_name, defaults = narrative_exit_profile(narrative or cfg.get("narrative"))
    changed = False

    if not cfg.get("exit_profile"):
        cfg["exit_profile"] = profile_name
        changed = True
    if narrative and not cfg.get("narrative"):
        cfg["narrative"] = narrative
        changed = True

    ts = cfg.setdefault("trailing_stop", {
        "enabled": False,
        "trail_pct": 30,
        "sell_pct": 100,
        "peak_price": 0.0,
        "triggered": False,
    })
    if "post_partial_trail_pct" not in ts:
        ts["post_partial_trail_pct"] = min(
            float(ts.get("trail_pct", 30) or 30),
            float(defaults["post_partial_trail_pct"]),
        )
        changed = True
    if "tightened" not in ts:
        ts["tightened"] = False
        changed = True

    if "first_risk_off" not in cfg:
        cfg["first_risk_off"] = deepcopy(defaults["first_risk_off"])
        changed = True

    if "velocity_rollover" not in cfg:
        cfg["velocity_rollover"] = deepcopy(defaults["velocity_rollover"])
        changed = True

    vro = cfg["velocity_rollover"]
    if entry_score_effective is not None:
        target = float(entry_score_effective or 0)
        if target > float(vro.get("peak_score", 0) or 0):
            vro["peak_score"] = target
            changed = True
        if not vro.get("last_score"):
            vro["last_score"] = target
            changed = True

    return changed


def tighten_trailing_after_partial(cfg: dict, *, current_price: float = 0.0) -> bool:
    changed = False
    ts = cfg.setdefault("trailing_stop", {
        "enabled": False,
        "trail_pct": 30,
        "sell_pct": 100,
        "peak_price": 0.0,
        "triggered": False,
        "post_partial_trail_pct": 20,
        "tightened": False,
    })
    fro = cfg.get("first_risk_off", {})
    target_trail = float(
        fro.get("tighten_to_pct", ts.get("post_partial_trail_pct", ts.get("trail_pct", 30)))
    )
    current_trail = float(ts.get("trail_pct", 30) or 30)

    if not ts.get("enabled"):
        ts["enabled"] = True
        changed = True
    if target_trail < current_trail:
        ts["trail_pct"] = target_trail
        changed = True
    if current_price > float(ts.get("peak_price", 0) or 0):
        ts["peak_price"] = current_price
        changed = True
    if not ts.get("tightened"):
        ts["tightened"] = True
        ts["tightened_at"] = time.time()
        changed = True

    return changed
