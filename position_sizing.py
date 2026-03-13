"""
position_sizing.py — adaptive auto-buy confidence and sizing helpers.

Shared between autobuy.py and bot.py so sizing decisions stay consistent from
gate evaluation through execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import strategy_profiles


SOURCE_CONFIDENCE = {
    "pumpfun_newest": 1.00,
    "pumpfun_hot": 0.90,
    "dex_pairs": 0.75,
    "dex_lookup": 0.65,
    "dex_profiles": 0.45,
    "dex_boosts": 0.35,
    "dex_search": 0.20,
}


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _boolish(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def resolve_source_name(result: dict) -> str:
    return str(
        result.get("_source_name")
        or result.get("source_name")
        or result.get("source")
        or ""
    )


def resolve_narrative(result: dict) -> str:
    return str(
        result.get("matched_narrative")
        or result.get("entry_narrative")
        or result.get("narrative")
        or "Other"
    )


def resolve_archetype(result: dict) -> str:
    return str(result.get("archetype") or result.get("entry_archetype") or "")


def _age_confidence(age_mins: float) -> float:
    if age_mins <= 5:
        return 1.00
    if age_mins <= 15:
        return 0.92
    if age_mins <= 30:
        return 0.75
    if age_mins <= 60:
        return 0.50
    return 0.25


def compute_entry_confidence(result: dict) -> tuple[float, dict]:
    effective_score = float(result.get("effective_score", result.get("total", 0)) or 0)
    source_name = resolve_source_name(result)
    source_rank = float(result.get("_source_rank", result.get("source_rank", 0)) or 0)
    age_now = float(result.get("age_mins", 20) or 20)
    wallet_signal = float(result.get("wallet_signal", result.get("wallet_boost", 0)) or 0)
    liq_to_mcap = float(result.get("liquidity_to_mcap_ratio", 0.05) or 0.05)
    txns_per_10k_liq = float(result.get("txns_per_10k_liq", 5) or 5)
    buy_ratio = float(result.get("buy_ratio_5m", 0.58) or 0.58)
    score_slope = float(result.get("score_slope", 0) or 0)
    score_drop = float(result.get("score_drop_from_peak", 0) or 0)
    liq_drop = float(result.get("liquidity_drop_pct", 0) or 0)
    holder_delta = float(result.get("holder_concentration_delta", 0) or 0)
    narrative_cluster_count = int(result.get("narrative_cluster_count", 0) or 0)
    archetype_conf = float(result.get("archetype_conf", 60) or 60)

    score_component = _clamp((effective_score - 55.0) / 35.0)
    source_component = SOURCE_CONFIDENCE.get(
        source_name,
        _clamp(source_rank / 100.0, 0.20, 1.0) if source_rank else 0.55,
    )
    freshness_component = _age_confidence(age_now)
    wallet_component = _clamp(wallet_signal / 8.0)
    liquidity_component = (_clamp(liq_to_mcap / 0.10) + _clamp(txns_per_10k_liq / 10.0)) / 2.0
    momentum_component = (_clamp((buy_ratio - 0.50) / 0.25) + _clamp((score_slope + 2.5) / 7.5)) / 2.0
    archetype_component = _clamp(archetype_conf / 100.0)

    confidence = (
        score_component * 0.28
        + source_component * 0.14
        + freshness_component * 0.16
        + wallet_component * 0.12
        + liquidity_component * 0.14
        + momentum_component * 0.16
        + archetype_component * 0.10
    )

    confidence -= _clamp(score_drop / 20.0) * 0.12
    confidence -= _clamp(max(-liq_drop, 0.0) / 20.0) * 0.10
    confidence -= _clamp(max(holder_delta, 0.0) / 5.0) * 0.06
    confidence -= _clamp(max(narrative_cluster_count - 1, 0) / 3.0) * 0.06

    breakdown = {
        "score": round(score_component, 3),
        "source": round(source_component, 3),
        "freshness": round(freshness_component, 3),
        "wallet": round(wallet_component, 3),
        "liquidity": round(liquidity_component, 3),
        "momentum": round(momentum_component, 3),
        "archetype": round(archetype_component, 3),
    }
    return round(_clamp(confidence), 4), breakdown


@dataclass
class SizingDecision:
    confidence: float
    sol_amount: float
    size_multiplier: float
    narrative: str
    archetype: str
    strategy_profile: str
    block_reason: str = ""
    breakdown: dict = field(default_factory=dict)


def resolve_position_size(cfg: dict, result: dict, exposure: dict | None = None) -> SizingDecision:
    exposure = exposure or {"narrative": {}, "archetype": {}}
    narrative = resolve_narrative(result)
    archetype = resolve_archetype(result)
    strategy_profile = strategy_profiles.resolve_strategy_profile(result)
    confidence, breakdown = compute_entry_confidence(result)

    min_confidence = float(cfg.get("min_confidence", 0.35) or 0.35)
    if confidence < min_confidence:
        return SizingDecision(
            confidence=confidence,
            sol_amount=0.0,
            size_multiplier=0.0,
            narrative=narrative,
            archetype=archetype,
            strategy_profile=strategy_profile,
            block_reason=f"confidence {confidence:.2f} < min {min_confidence:.2f}",
            breakdown=breakdown,
        )

    max_narrative = int(cfg.get("max_narrative_exposure", 2) or 0)
    if max_narrative > 0 and narrative and narrative != "Other":
        current = int((exposure.get("narrative") or {}).get(narrative, 0) or 0)
        if current >= max_narrative:
            return SizingDecision(
                confidence=confidence,
                sol_amount=0.0,
                size_multiplier=0.0,
                narrative=narrative,
                archetype=archetype,
                strategy_profile=strategy_profile,
                block_reason=f"narrative exposure {current} >= max {max_narrative} ({narrative})",
                breakdown=breakdown,
            )

    max_archetype = int(cfg.get("max_archetype_exposure", 0) or 0)
    if max_archetype > 0 and archetype:
        current = int((exposure.get("archetype") or {}).get(archetype, 0) or 0)
        if current >= max_archetype:
            return SizingDecision(
                confidence=confidence,
                sol_amount=0.0,
                size_multiplier=0.0,
                narrative=narrative,
                archetype=archetype,
                strategy_profile=strategy_profile,
                block_reason=f"archetype exposure {current} >= max {max_archetype} ({archetype})",
                breakdown=breakdown,
            )

    base_sol = float(cfg.get("sol_amount", 0.03) or 0.03)
    max_sol = float(cfg.get("max_sol_amount", max(base_sol, 0.10)) or max(base_sol, 0.10))
    max_sol = max(base_sol, max_sol)
    scaling_enabled = _boolish(cfg.get("confidence_scale_enabled", True), default=True)

    if not scaling_enabled:
        multiplier = 1.0
    elif confidence < 0.55:
        multiplier = 0.50
    elif confidence < 0.70:
        multiplier = 1.00
    elif confidence < 0.85:
        multiplier = 1.50
    else:
        multiplier = 2.00

    strategy_size_bias = float(result.get("strategy_size_bias", 1.0) or 1.0)
    sol_amount = round(min(max_sol, base_sol * multiplier * strategy_size_bias), 4)
    return SizingDecision(
        confidence=confidence,
        sol_amount=sol_amount,
        size_multiplier=round(multiplier * strategy_size_bias, 2),
        narrative=narrative,
        archetype=archetype,
        strategy_profile=strategy_profile,
        breakdown=breakdown,
    )
