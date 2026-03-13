from __future__ import annotations

from copy import deepcopy


STRATEGY_PROFILES = {
    "launch_snipe": {
        "label": "Ultra-fresh pump.fun",
        "allowed_sources": {"pumpfun_newest", "pumpfun_hot"},
        "soft_max_age_mins": 20,
        "hard_max_age_mins": 45,
        "min_liquidity_usd": 2_500,
        "min_txns_5m": 6,
        "min_buy_ratio_5m": 0.56,
        "max_mcap_usd": 350_000,
        "size_bias": 1.15,
        "exit_preset": "scalp",
    },
    "migration_continuation": {
        "label": "Graduated Raydium momentum",
        "preferred_dex": "raydium",
        "soft_max_age_mins": 120,
        "hard_max_age_mins": 240,
        "min_liquidity_usd": 8_000,
        "min_txns_5m": 10,
        "min_buy_ratio_5m": 0.54,
        "size_bias": 1.00,
        "exit_preset": "standard",
    },
    "wallet_follow": {
        "label": "Wallet-follow continuation",
        "soft_max_age_mins": 180,
        "hard_max_age_mins": 360,
        "min_liquidity_usd": 4_000,
        "min_txns_5m": 5,
        "min_buy_ratio_5m": 0.52,
        "min_wallet_signal": 5,
        "size_bias": 1.20,
        "exit_preset": "diamond",
    },
    "narrative_breakout": {
        "label": "Narrative breakout",
        "soft_max_age_mins": 45,
        "hard_max_age_mins": 120,
        "min_liquidity_usd": 3_500,
        "min_txns_5m": 8,
        "min_buy_ratio_5m": 0.58,
        "require_narrative": True,
        "size_bias": 0.95,
        "exit_preset": "standard",
    },
}


ARCHETYPE_TO_PROFILE = {
    "MICRO_ROCKETSHIP": "launch_snipe",
    "STEALTH_RAYDIUM": "migration_continuation",
    "CLEAN_WHALE_ENTRY": "wallet_follow",
    "AI_CLUSTER": "narrative_breakout",
    "MEME_VIRAL": "narrative_breakout",
    "POLITICAL_SPIKE": "narrative_breakout",
}


def _source_name(result: dict) -> str:
    return str(result.get("_source_name") or result.get("source_name") or result.get("source") or "")


def _narrative(result: dict) -> str:
    return str(
        result.get("matched_narrative")
        or result.get("entry_narrative")
        or result.get("narrative")
        or "Other"
    )


def get_profile(name: str | None) -> dict:
    return deepcopy(STRATEGY_PROFILES.get(str(name or ""), STRATEGY_PROFILES["narrative_breakout"]))


def resolve_strategy_profile(result: dict) -> str:
    explicit = str(result.get("strategy_profile") or "")
    if explicit in STRATEGY_PROFILES:
        return explicit

    archetype = str(result.get("archetype") or result.get("entry_archetype") or "")
    if archetype in ARCHETYPE_TO_PROFILE:
        return ARCHETYPE_TO_PROFILE[archetype]

    source_name = _source_name(result)
    age_mins = float(result.get("age_mins", 9_999) or 9_999)
    wallet_signal = float(result.get("wallet_signal", result.get("wallet_boost", 0)) or 0)
    dex = str(result.get("dex") or "").lower()
    mcap = float(result.get("mcap", 0) or 0)
    narrative = _narrative(result)

    if source_name in {"pumpfun_newest", "pumpfun_hot"} and age_mins <= 15 and (mcap <= 350_000 or mcap <= 0):
        return "launch_snipe"
    if wallet_signal >= 5:
        return "wallet_follow"
    if dex == "raydium" or source_name in {"dex_pairs", "dex_lookup"}:
        return "migration_continuation"
    if narrative != "Other":
        return "narrative_breakout"
    return "launch_snipe" if age_mins <= 10 else "narrative_breakout"


def annotate_result(result: dict) -> dict:
    profile_name = resolve_strategy_profile(result)
    profile = get_profile(profile_name)
    archetype_conf = float(result.get("archetype_conf", result.get("strategy_confidence", 0)) or 0)
    wallet_signal = float(result.get("wallet_signal", result.get("wallet_boost", 0)) or 0)
    source_name = _source_name(result)
    age_mins = float(result.get("age_mins", 9_999) or 9_999)
    narrative = _narrative(result)

    confidence = archetype_conf
    if confidence <= 0:
        confidence = 55.0
        if source_name in {"pumpfun_newest", "pumpfun_hot"}:
            confidence += 12
        if wallet_signal >= 5:
            confidence += 14
        if narrative != "Other":
            confidence += 8
        if age_mins <= 15:
            confidence += 8
    confidence = max(0.0, min(100.0, confidence))

    return {
        "strategy_profile": profile_name,
        "strategy_label": profile["label"],
        "strategy_confidence": round(confidence, 1),
        "strategy_size_bias": profile["size_bias"],
        "strategy_exit_preset": profile["exit_preset"],
    }


def evaluate_strategy_rules(data: dict) -> dict:
    profile_name = resolve_strategy_profile(data)
    profile = get_profile(profile_name)
    reasons: list[str] = []
    autobuy_only: list[str] = []

    source_name = _source_name(data)
    age_mins = float(data.get("age_mins", 9_999) or 9_999)
    liquidity = float(data.get("liquidity", 0) or 0)
    txns_5m = int(data.get("txns_5m", 0) or 0)
    buy_ratio = float(data.get("buy_ratio_5m", 0.5) or 0.5)
    wallet_signal = float(data.get("wallet_signal", data.get("wallet_boost", 0)) or 0)
    dex = str(data.get("dex") or "").lower()
    mcap = float(data.get("mcap", 0) or 0)
    narrative = _narrative(data)

    if profile_name == "launch_snipe":
        allowed = profile.get("allowed_sources", set())
        if source_name not in allowed and wallet_signal < 5:
            autobuy_only.append("launch_snipe wants direct pump.fun discovery")
        if age_mins > float(profile["soft_max_age_mins"]):
            autobuy_only.append("launch_snipe missed early entry window")
        if mcap and mcap > float(profile["max_mcap_usd"]):
            autobuy_only.append("launch_snipe prefers smaller caps")
    elif profile_name == "migration_continuation":
        if dex != str(profile.get("preferred_dex", "")).lower() and wallet_signal < 5:
            autobuy_only.append("migration continuation wants Raydium flow")
        if age_mins > float(profile["soft_max_age_mins"]) and wallet_signal < 5:
            autobuy_only.append("migration continuation past ideal timing")
    elif profile_name == "wallet_follow":
        if wallet_signal < float(profile["min_wallet_signal"]):
            reasons.append("wallet_follow needs stronger wallet signal")
        if age_mins > float(profile["soft_max_age_mins"]):
            autobuy_only.append("wallet_follow entry too old")
    elif profile_name == "narrative_breakout":
        if profile.get("require_narrative") and narrative == "Other":
            reasons.append("narrative breakout needs a real narrative")
        if age_mins > float(profile["soft_max_age_mins"]) and wallet_signal < 5:
            autobuy_only.append("narrative breakout losing freshness")

    if age_mins > float(profile["hard_max_age_mins"]) and wallet_signal < 5:
        reasons.append(f"{profile_name} outside hard age window")
    if liquidity and liquidity < float(profile["min_liquidity_usd"]):
        reasons.append(f"{profile_name} needs more liquidity")
    if txns_5m > 0 and txns_5m < int(profile["min_txns_5m"]):
        reasons.append(f"{profile_name} needs more recent txns")
    if txns_5m >= 6 and buy_ratio < float(profile["min_buy_ratio_5m"]):
        reasons.append(f"{profile_name} buy ratio too weak")

    return {
        "strategy_profile": profile_name,
        "strategy_reasons": reasons,
        "strategy_autobuy_only_reasons": autobuy_only,
        "strategy_size_bias": profile["size_bias"],
        "strategy_exit_preset": profile["exit_preset"],
    }


def apply_auto_sell_profile(cfg: dict, strategy_profile: str | None) -> bool:
    profile_name = resolve_strategy_profile({"strategy_profile": strategy_profile})
    changed = False
    cfg["strategy_profile"] = profile_name

    ts = cfg.setdefault("trailing_stop", {})
    ttp = cfg.setdefault("trailing_tp", {})
    te = cfg.setdefault("time_exit", {})
    fro = cfg.setdefault("first_risk_off", {})
    vro = cfg.setdefault("velocity_rollover", {})

    if profile_name == "launch_snipe":
        updates = {
            "trail_pct": 16,
            "post_partial_trail_pct": 14,
            "enabled": True,
        }
        for key, value in updates.items():
            if ts.get(key) != value:
                ts[key] = value
                changed = True
        for key, value in {"enabled": True, "hours": 3, "target_mult": 1.4}.items():
            if te.get(key) != value:
                te[key] = value
                changed = True
        for key, value in {"enabled": True, "activate_mult": 1.5, "sell_pct": 35}.items():
            if fro.get(key) != value:
                fro[key] = value
                changed = True
    elif profile_name == "migration_continuation":
        for key, value in {"enabled": True, "trail_pct": 22, "post_partial_trail_pct": 18}.items():
            if ts.get(key) != value:
                ts[key] = value
                changed = True
        for key, value in {"enabled": True, "activate_mult": 1.8, "sell_pct": 25}.items():
            if fro.get(key) != value:
                fro[key] = value
                changed = True
        for key, value in {"enabled": False}.items():
            if te.get(key) != value:
                te[key] = value
                changed = True
    elif profile_name == "wallet_follow":
        for key, value in {"enabled": True, "trail_pct": 24, "post_partial_trail_pct": 18}.items():
            if ts.get(key) != value:
                ts[key] = value
                changed = True
        for key, value in {"enabled": True, "activate_mult": 2.5, "trail_pct": 18, "sell_pct": 60}.items():
            if ttp.get(key) != value:
                ttp[key] = value
                changed = True
        for key, value in {"enabled": True, "activate_mult": 2.2, "sell_pct": 20}.items():
            if fro.get(key) != value:
                fro[key] = value
                changed = True
    else:  # narrative_breakout
        for key, value in {"enabled": True, "trail_pct": 20, "post_partial_trail_pct": 17}.items():
            if ts.get(key) != value:
                ts[key] = value
                changed = True
        for key, value in {"enabled": True, "hours": 8, "target_mult": 1.6}.items():
            if te.get(key) != value:
                te[key] = value
                changed = True
        for key, value in {"enabled": True, "activate_mult": 1.75, "sell_pct": 30}.items():
            if fro.get(key) != value:
                fro[key] = value
                changed = True

    if not vro.get("enabled"):
        vro["enabled"] = True
        changed = True

    return changed
