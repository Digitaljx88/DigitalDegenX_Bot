"""
autobuy.py — Auto-buy gate pipeline.

Separates the decision logic (evaluate) from the execution (execute) so that
each gate can be unit-tested independently of the Telegram bot and Solana RPC.

Usage:
    decision = await evaluate(uid, scanner_result)
    if decision.gate_passed:
        await execute(bot, decision, scanner_result)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import requests

import db as _db
import position_sizing
from services.trading import build_trading_snapshot


# ── BuyDecision ───────────────────────────────────────────────────────────────

@dataclass
class BuyDecision:
    uid:          int
    mint:         str
    symbol:       str
    name:         str
    score:        int
    mcap:         float
    sol_amount:   float
    confidence:   float       = 0.0
    size_multiplier: float    = 1.0
    strategy_profile: str     = ""
    gate_passed:  bool        = False
    block_reason: str         = ""
    # resolved by evaluate — forwarded to execute
    mode:         str         = "paper"  # "paper" | "live"
    # fresh market data (filled by gate_freshness)
    fresh_vol_m5: float       = 0.0
    fresh_price_h1: float     = 0.0


# ── Individual gate functions ─────────────────────────────────────────────────
# Each returns (passed: bool, reason: str).
# Gates are designed to be pure / easily mockable in tests.

def gate_enabled(uid: int, cfg: dict) -> tuple[bool, str]:
    if not cfg.get("enabled"):
        return False, "auto-buy not enabled"
    return True, ""


def gate_score(score: int, cfg: dict, user_cfg: dict) -> tuple[bool, str]:
    """Score must meet tier threshold unless it is a graduation buy."""
    min_score = cfg.get("min_score", 55)
    tier = cfg.get("buy_tier", "")
    if tier:
        tier_map = {
            "scouted":   user_cfg.get("alert_scouted_threshold", 35),
            "warm":      user_cfg.get("alert_warm_threshold", 55),
            "hot":       user_cfg.get("alert_hot_threshold", 70),
            "ultra_hot": user_cfg.get("alert_ultra_hot_threshold", 85),
        }
        min_score = tier_map.get(tier, min_score)
    if score < min_score:
        return False, f"score {score} < min {min_score} (tier={tier or 'manual'})"
    return True, ""


def gate_mcap(mcap: float, cfg: dict) -> tuple[bool, str]:
    max_mcap = cfg.get("max_mcap", 500_000)
    if mcap and mcap > max_mcap:
        return False, f"mcap ${mcap:,.0f} > max ${max_mcap:,.0f}"
    return True, ""


def gate_already_bought(uid: int, mint: str) -> tuple[bool, str]:
    if _db.has_bought(uid, mint):
        return False, "already bought this token"
    return True, ""


def gate_daily_limit(uid: int, cfg: dict, sol_amount: float) -> tuple[bool, str]:
    daily_limit = cfg.get("daily_limit_sol", 1.0)
    if daily_limit <= 0:
        return True, ""
    today_spent = _db.get_spent_today(uid)
    if today_spent + sol_amount > daily_limit:
        return False, (
            f"daily limit reached — spent {today_spent:.3f} SOL, "
            f"limit {daily_limit:.1f} SOL"
        )
    return True, ""


def gate_position_limit(uid: int, cfg: dict) -> tuple[bool, str]:
    max_pos = cfg.get("max_positions", 0)
    if max_pos <= 0:
        return True, ""
    open_pos = _db.get_open_position_count(uid)
    if open_pos >= max_pos:
        return False, f"positions {open_pos} >= max {max_pos}"
    return True, ""


def gate_momentum(result: dict) -> tuple[bool, str]:
    """Block post-peak / dying tokens based on cached scanner data."""
    h1_price = float(result.get("price_h1", 0) or 0)
    vol_m5   = float(result.get("volume_m5", 0) or 0)
    vol_h1   = float(result.get("volume_h1", 1) or 1)
    m5_pace  = vol_m5 * 12  # extrapolate m5 to hourly rate
    if not (h1_price >= -5 or m5_pace >= vol_h1 * 0.3):
        return False, (
            f"momentum dead — h1_price={h1_price:+.1f}%, "
            f"m5_pace=${m5_pace:,.0f} vs h1=${vol_h1:,.0f}"
        )
    return True, ""


def gate_entry_quality(result: dict) -> tuple[bool, str]:
    """
    Block trades the scanner already flagged as weak-quality or out of the
    strict fresh-launch autobuy window.
    """
    reasons: list[str] = []
    reasons.extend(result.get("entry_quality_reasons") or [])
    if result.get("entry_quality_autobuy_only_reasons"):
        reasons.extend(result.get("entry_quality_autobuy_only_reasons") or [])
    if result.get("entry_quality_force_scouted"):
        reasons.extend(result.get("entry_quality_force_scouted_reasons") or ["force scouted"])
    if result.get("entry_quality_autobuy_blocked"):
        if reasons:
            return False, "entry quality blocked — " + ", ".join(dict.fromkeys(str(r) for r in reasons if r))
        return False, "entry quality blocked"
    return True, ""


def gate_freshness(mint: str) -> tuple[bool, str, float, float]:
    """
    Re-fetch DexScreener pair data to verify token is still active.
    Returns (passed, reason, fresh_vol_m5, fresh_price_h1).
    Fails open on network error (returns True) — we'd rather miss a block than
    silently skip due to transient API unavailability.
    """
    try:
        resp = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=8,
        )
        pairs = resp.json().get("pairs") or []
        # Pick the Solana pair with the highest liquidity
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol_pairs:
            return True, "", 0.0, 0.0  # no pair data — fail open
        pair     = max(sol_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0)))
        vol_m5   = float((pair.get("volume") or {}).get("m5", 0) or 0)
        price_h1 = float((pair.get("priceChange") or {}).get("h1", 0) or 0)
        vol_h1   = float((pair.get("volume") or {}).get("h1", 1) or 1)
        m5_pace  = vol_m5 * 12
        tx_buys  = int(((pair.get("txns") or {}).get("m5") or {}).get("buys", 0) or 0)
        tx_sells = int(((pair.get("txns") or {}).get("m5") or {}).get("sells", 0) or 0)
        total_tx = tx_buys + tx_sells
        buy_ratio = (tx_buys / total_tx) if total_tx > 0 else 0.5
        if vol_m5 < 50:
            return False, f"fresh data: zero activity (vol_m5=${vol_m5:.0f} < $50 floor)", vol_m5, price_h1
        if total_tx >= 8 and buy_ratio < 0.52:
            return False, f"fresh data: buy ratio fading ({buy_ratio:.0%})", vol_m5, price_h1
        if not (price_h1 >= -5 or m5_pace >= vol_h1 * 0.3):
            return False, (
                f"fresh data: momentum dead — "
                f"h1_price={price_h1:+.1f}%, m5_pace=${m5_pace:,.0f} vs h1=${vol_h1:,.0f}"
            ), vol_m5, price_h1
        return True, "", vol_m5, price_h1
    except Exception as e:
        # Network error — fail open, log and proceed
        print(f"[AUTOBUY] gate_freshness: DexScreener fetch failed for {mint}: {e} — failing open", flush=True)
        return True, "", 0.0, 0.0


# ── evaluate() ────────────────────────────────────────────────────────────────

async def evaluate(uid: int, result: dict) -> BuyDecision:
    """
    Run all 8 gates and return a BuyDecision.
    Async because gate_freshness runs in a thread executor to avoid blocking
    the event loop during the HTTP call.
    """
    mint   = result.get("mint", "")
    symbol = result.get("symbol", mint[:6])
    name   = result.get("name", symbol)
    score  = result.get("effective_score", result.get("total", 0))
    mcap   = result.get("mcap", 0)

    # Reset daily spend counter if it's a new UTC day
    _db.reset_day_if_needed(uid)
    cfg = _db.get_auto_buy_config(uid)

    # Lazy import settings_manager to avoid heavy circular-import at module level
    try:
        import settings_manager as _sm
        user_cfg = _sm.get_user_settings(uid)
    except Exception as e:
        print(f"[AUTOBUY] evaluate: settings_manager unavailable for uid={uid}: {e} — using defaults", flush=True)
        user_cfg = {}

    # Run all fast (synchronous) gates in order
    gates = [
        lambda: gate_enabled(uid, cfg),
        lambda: gate_score(score, cfg, user_cfg),
        lambda: gate_mcap(mcap, cfg),
        lambda: gate_already_bought(uid, mint),
        lambda: gate_position_limit(uid, cfg),
        lambda: gate_momentum(result),
        lambda: gate_entry_quality(result),
    ]

    for g in gates:
        passed, reason = g()
        if not passed:
            print(f"[AUTOBUY] uid={uid} BLOCKED {symbol}: {reason}", flush=True)
            return BuyDecision(
                uid=uid, mint=mint, symbol=symbol, name=name,
                score=score, mcap=mcap, sol_amount=0.0,
                gate_passed=False, block_reason=reason,
            )

    sizing = position_sizing.resolve_position_size(
        cfg,
        result,
        exposure=_db.get_open_position_exposure(uid),
    )
    if sizing.block_reason:
        print(f"[AUTOBUY] uid={uid} BLOCKED {symbol}: {sizing.block_reason}", flush=True)
        return BuyDecision(
            uid=uid, mint=mint, symbol=symbol, name=name,
            score=score, mcap=mcap, sol_amount=0.0,
            confidence=sizing.confidence,
            size_multiplier=sizing.size_multiplier,
            strategy_profile=sizing.strategy_profile,
            gate_passed=False, block_reason=sizing.block_reason,
        )

    sol_amount = sizing.sol_amount
    passed, reason = gate_daily_limit(uid, cfg, sol_amount)
    if not passed:
        print(f"[AUTOBUY] uid={uid} BLOCKED {symbol}: {reason}", flush=True)
        return BuyDecision(
            uid=uid, mint=mint, symbol=symbol, name=name,
            score=score, mcap=mcap, sol_amount=sol_amount,
            confidence=sizing.confidence,
            size_multiplier=sizing.size_multiplier,
            strategy_profile=sizing.strategy_profile,
            gate_passed=False, block_reason=reason,
        )

    # gate_freshness runs a blocking HTTP call — offload to thread
    loop = asyncio.get_running_loop()
    fresh_passed, fresh_reason, fresh_vol_m5, fresh_price_h1 = await loop.run_in_executor(
        None, gate_freshness, mint
    )
    if not fresh_passed:
        print(f"[AUTOBUY] uid={uid} BLOCKED {symbol}: {fresh_reason}", flush=True)
        return BuyDecision(
            uid=uid, mint=mint, symbol=symbol, name=name,
            score=score, mcap=mcap, sol_amount=sol_amount,
            confidence=sizing.confidence,
            size_multiplier=sizing.size_multiplier,
            strategy_profile=sizing.strategy_profile,
            gate_passed=False, block_reason=fresh_reason,
        )

    # All gates passed
    try:
        import bot as _bot
        mode = _bot.get_mode(uid)
    except Exception as e:
        print(f"[AUTOBUY] evaluate: get_mode failed for uid={uid}: {e} — defaulting to paper", flush=True)
        mode = "paper"

    return BuyDecision(
        uid=uid, mint=mint, symbol=symbol, name=name,
        score=score, mcap=mcap, sol_amount=sol_amount,
        confidence=sizing.confidence,
        size_multiplier=sizing.size_multiplier,
        strategy_profile=sizing.strategy_profile,
        gate_passed=True, block_reason="",
        mode=mode,
        fresh_vol_m5=fresh_vol_m5,
        fresh_price_h1=fresh_price_h1,
    )


async def evaluate_lifecycle_snapshot(uid: int, snapshot, *, user_id: int | None = None) -> BuyDecision:
    """
    Evaluate autobuy eligibility directly from a lifecycle snapshot by converting it
    into the same normalized scanner token/result shape used by the scanner.
    """
    token = build_trading_snapshot(snapshot, max_age_hours=4)
    if not token:
        return BuyDecision(
            uid=uid,
            mint=getattr(snapshot, "mint", ""),
            symbol=getattr(getattr(snapshot, "lifecycle", None), "symbol", "") or "",
            name=getattr(getattr(snapshot, "lifecycle", None), "name", "") or "",
            score=0,
            mcap=0.0,
            sol_amount=0.0,
            gate_passed=False,
            block_reason="snapshot outside fresh scan window",
        )

    import scanner as _scanner

    result = _scanner.calculate_heat_score_with_settings(token, snapshot.enrichment.rugcheck or {}, user_id=user_id)
    result["effective_score"] = result.get("total", 0)
    result["velocity"] = 0.0
    result["velocity_label"] = "flat"
    narrative = result.get("matched_narrative") or token.get("lifecycle_narrative") or "Other"
    result["matched_narrative"] = narrative
    result.update(_scanner.build_entry_quality(token, snapshot.enrichment.rugcheck or {}, result, narrative))
    quality_flags = _scanner.apply_entry_quality_rules(
        result,
        effective_score=result["effective_score"],
        momentum_alive=True,
    )
    result["entry_quality_reasons"] = quality_flags["quality_reasons"]
    result["entry_quality_force_scouted"] = quality_flags["force_scouted"]
    result["entry_quality_alert_blocked"] = quality_flags["alert_blocked"]
    result["entry_quality_autobuy_blocked"] = quality_flags["autobuy_blocked"]
    result["entry_quality_force_scouted_reasons"] = quality_flags["force_scouted_reasons"]
    result["entry_quality_autobuy_only_reasons"] = quality_flags["autobuy_only_reasons"]
    result["entry_quality_primary_source"] = quality_flags["primary_source"]
    result["strategy_profile"] = quality_flags["strategy_profile"]
    result["strategy_exit_preset"] = quality_flags["strategy_exit_preset"]
    result["strategy_size_bias"] = quality_flags["strategy_size_bias"]
    return await evaluate(uid, result)


# ── execute() ─────────────────────────────────────────────────────────────────

async def execute(bot, decision: BuyDecision, result: dict):
    """
    Thin wrapper: delegates to bot.execute_auto_buy so that existing execution
    logic (portfolio lock, on-chain TX, DM formatting) stays in one place.
    Task 8 will move that logic here and make bot.py import autobuy instead.
    """
    if not decision.gate_passed:
        return
    import bot as _bot
    await _bot.execute_auto_buy(bot, decision.uid, result, decision=decision)
