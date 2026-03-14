"""
Heat Score v2: Simplified 8-Factor Model
───────────────────────────────────────────

A complete redesign of the heat scoring system for better accuracy and transparency.

8 Core Factors (125 pts total, normalized to 0-100):
1. Momentum (0-20 pts) — Volume momentum, growth trajectory
2. Liquidity (0-20 pts) — Pool liquidity, trading depth, reserves
3. Risk Safety (0-25 pts) — Dev wallet, top holders, rugcheck, bundle risk
4. Social/Narrative (0-15 pts) — Twitter activity, trending narratives
5. Wallets (0-15 pts) — Tracked wallet entries, cluster signals
6. Migration Status (0-10 pts) — Token lifecycle (new, graduated, migrated)
7. Buy Directional Bias (0-10 pts) — Buy/sell pressure from Birdeye
8. Volume Trend (0-5 pts) — Volume acceleration from GeckoTerminal

Each factor returns:
- points: 0 to MAX (see above)
- reason: Human-readable explanation
- details: Optional dict with sub-metrics
"""

import json
from datetime import datetime, timedelta
from typing import Optional

try:
    import birdeye as _birdeye
except ImportError:
    _birdeye = None

try:
    import geckoterminal as _gecko
except ImportError:
    _gecko = None

try:
    import wallet_tracker as _wallet_tracker
except ImportError:
    _wallet_tracker = None

try:
    import wallet_cluster as _wallet_cluster
except ImportError:
    _wallet_cluster = None


def _token_mint(token_or_mint) -> str:
    if isinstance(token_or_mint, dict):
        return str(token_or_mint.get("mint", "") or "")
    return str(token_or_mint or "")


def score_momentum(token: dict, cfg: dict = None) -> tuple[int, str, dict]:
    """
    Score momentum (0-20 pts) based on price movement, volume growth, and bid/ask spreads.
    
    Tunable via:
    - momentum_weight_usd_vol: 1-100 (default 50) — USD volume importance
    - momentum_weight_creation_momentum: 1-100 (default 50) — launch timing importance
    - momentum_min_vol: float (default 5000.0) — minimum volume for full points
    """
    if cfg is None:
        cfg = {}
    
    details = {}
    
    # Hours since creation
    created_ts = token.get("created_timestamp", 0)
    if created_ts:
        age_hours = (datetime.now().timestamp() - created_ts) / 3600
        if age_hours < 0.5:  # Less than 30 minutes
            details["age_category"] = "NEWBORN"
            age_boost = 10
        elif age_hours < 2:  # Less than 2 hours
            details["age_category"] = "FRESH"
            age_boost = 8
        elif age_hours < 4:  # Less than 4 hours
            details["age_category"] = "WARM"
            age_boost = 5
        else:
            details["age_category"] = "AGING"
            age_boost = 2
    else:
        age_boost = 0
    
    # USD volume growth in last 5 minutes
    vol_5m = token.get("volume_5m_usd", 0) or 0
    vol_1h = token.get("volume_1h_usd", 0) or 0
    vol_24h = token.get("volume_24h_usd", 0) or 0
    
    vol_weight = cfg.get("momentum_weight_usd_vol", 50) / 100.0
    min_vol = cfg.get("momentum_min_vol", 5000.0)
    
    if vol_24h >= min_vol:
        vol_pts = 10
    elif vol_1h >= min_vol * 0.2:
        vol_pts = 8
    elif vol_5m >= min_vol * 0.05:
        vol_pts = 5
    else:
        vol_pts = 0
    
    details["volume_24h_usd"] = vol_24h
    details["volume_1h_usd"] = vol_1h
    details["volume_5m_usd"] = vol_5m
    details["age_hours"] = age_hours if created_ts else None
    
    # Bid/ask spread quality (lower is better)
    bid_ask_spread = token.get("bid_ask_spread_bps", 100) or 100  # in basis points
    if bid_ask_spread > 0:
        spread_pts = max(0, 10 - (bid_ask_spread / 100))  # 0-10 pts
    else:
        spread_pts = 5
    
    details["bid_ask_spread_bps"] = bid_ask_spread
    details["spread_pts"] = spread_pts
    
    # Composite momentum score
    final_pts = int(age_boost * 0.5 + vol_pts * (vol_weight) + spread_pts * (1 - vol_weight))
    final_pts = min(20, max(0, final_pts))
    
    reason = (
        f"Age: {details.get('age_category', 'UNKNOWN')}, "
        f"Vol24h: ${vol_24h:,.0f}, "
        f"Spread: {bid_ask_spread:.0f}bps"
    )
    
    return final_pts, reason, details


def score_liquidity(token: dict, rc: dict = None, cfg: dict = None) -> tuple[int, str, dict]:
    """
    Score liquidity (0-20 pts) based on total liquidity, pool depth, and contract supply.
    
    Tunable via:
    - liquidity_min_usd: float (default 50000.0) — USD liquidity for full points
    - liquidity_good_usd: float (default 10000.0) — USD liquidity for solid points
    - liquidity_fair_usd: float (default 2000.0) — USD liquidity for baseline points
    """
    if cfg is None:
        cfg = {}
    if rc is None:
        rc = {}
    
    details = {}
    
    # Total liquidity from token metadata
    total_liquidity = token.get("liquidity_usd", 0) or 0
    
    min_liq = cfg.get("liquidity_min_usd", 50000.0)
    good_liq = cfg.get("liquidity_good_usd", 10000.0)
    fair_liq = cfg.get("liquidity_fair_usd", 2000.0)
    
    if total_liquidity >= min_liq:
        liq_pts = 17
    elif total_liquidity >= good_liq:
        liq_pts = 12
    elif total_liquidity >= fair_liq:
        liq_pts = 7
    else:
        liq_pts = 0
    
    details["total_liquidity_usd"] = total_liquidity
    
    # Bonus for stable liquidity (no massive drains recorded)
    drain_history = rc.get("drain_history", []) or []
    if drain_history and len(drain_history) > 0:
        max_drain_pct = max([d.get("drain_pct", 0) for d in drain_history])
        if max_drain_pct < 10:
            liq_pts = min(20, liq_pts + 3)
        elif max_drain_pct > 50:
            liq_pts = max(0, liq_pts - 7)
        details["max_historical_drain_pct"] = max_drain_pct
    
    # Check contract supply info from RugCheck
    supply = rc.get("supply", 0) or 0
    holders_count = len(rc.get("topHolders", []) or [])
    if holders_count > 50:
        liq_pts = min(20, liq_pts + 2)
    
    details["total_supply"] = supply
    details["unique_holders"] = holders_count
    
    final_pts = min(20, max(0, liq_pts))
    
    reason = f"Liquidity: ${total_liquidity:,.0f}, Holders: {holders_count}, Supply: {supply:,.0f}"
    
    return final_pts, reason, details


def score_risk_safety(token: dict, rc: dict = None, cfg: dict = None) -> tuple[int, str, dict]:
    """
    Score risk safety (0-25 pts) based on dev wallet, top holders, rugcheck, bundle risk.
    
    High scoring = low risk. Scoring is inverted: start at 25 and deduct for red flags.
    
    Tunable via:
    - risk_dev_sell_threshold_pct: 1-100 (default 50) — dev >50% auto-disqualify
    - risk_top_holder_threshold_pct: 1-100 (default 20) — single >20% auto-disqualify
    - risk_bundle_severity: 1-100 (default 50) — bundle risk tolerance
    """
    if cfg is None:
        cfg = {}
    if rc is None:
        rc = {}
    
    details = {}
    risk_pts = 25  # Start full

    # Mint authority still enabled = issuer can mint more supply at will
    mint_authority = rc.get("mintAuthority")
    if mint_authority and mint_authority not in ("", "null", None):
        details["mint_authority_disqualified"] = True
        risk_pts = 0
    details["mint_authority"] = mint_authority
    
    # Dev wallet check
    dev_sold_pct = rc.get("dev_wallet", {}).get("sold_pct", 0) or 0
    dev_threshold = cfg.get("risk_dev_sell_threshold_pct", 50) / 100.0
    
    if dev_sold_pct > dev_threshold:
        details["dev_disqualified"] = True
        risk_pts = 0
    else:
        # Penalize dev activity but don't DQ
        if dev_sold_pct > dev_threshold * 0.7:
            risk_pts -= 8
        elif dev_sold_pct > dev_threshold * 0.4:
            risk_pts -= 5
        elif dev_sold_pct > 0:
            risk_pts -= 2
    
    details["dev_sold_pct"] = dev_sold_pct
    
    # Top holder concentration
    top_holders = rc.get("topHolders", []) or []
    holder_threshold = cfg.get("risk_top_holder_threshold_pct", 20) / 100.0
    
    max_holder_pct = 0
    if top_holders:
        max_holder_pct = (top_holders[0].get("balance", 0) / max(rc.get("supply", 1), 1)) * 100
    
    if max_holder_pct > holder_threshold * 100:
        details["top_holder_disqualified"] = True
        risk_pts = 0
    else:
        # Penalize but don't DQ
        if max_holder_pct > holder_threshold * 100 * 0.7:
            risk_pts -= 7
        elif max_holder_pct > holder_threshold * 100 * 0.4:
            risk_pts -= 4
    
    details["max_holder_pct"] = max_holder_pct
    details["unique_holders"] = len(top_holders)
    
    # RugCheck flags
    rugcheck_risk = rc.get("risk_level", "unknown") or "unknown"
    if rugcheck_risk == "high":
        risk_pts -= 12
    elif rugcheck_risk == "medium":
        risk_pts -= 6
    elif rugcheck_risk == "low":
        pass  # Already factored into points
    
    details["rugcheck_risk"] = rugcheck_risk

    danger_risks = [r.get("name", "unknown") for r in (rc.get("risks") or []) if r.get("level") == "danger"]
    if danger_risks:
        details["danger_risks"] = danger_risks
        details["danger_disqualified"] = True
        risk_pts = 0
    
    # Bundle risk from RugCheck
    is_bundled = rc.get("is_bundled", False) or False
    if is_bundled:
        bundle_severity = cfg.get("risk_bundle_severity", 50) / 100.0
        risk_pts -= int(10 * bundle_severity)
    
    details["is_bundled"] = is_bundled
    
    final_pts = min(25, max(0, risk_pts))
    
    reason = (
        f"Risk: {rugcheck_risk.upper()}, Dev: {dev_sold_pct:.1f}%, "
        f"Top Holder: {max_holder_pct:.1f}%"
    )
    
    return final_pts, reason, details


def score_social_narrative(token: dict, cfg: dict = None) -> tuple[int, str, dict]:
    """
    Score social/narrative (0-15 pts) based on Twitter presence and trending narratives.
    
    Tunable via:
    - social_twitter_follower_min: int (default 1000) — minimum followers for full points
    - social_narrative_trending_boost: 1-100 (default 50) — trending narrative weight
    """
    if cfg is None:
        cfg = {}
    
    details = {}
    social_pts = 0
    
    # Twitter presence
    twitter_handle = token.get("twitter", "") or ""
    twitter_followers = token.get("twitter_followers", 0) or 0
    
    twitter_min = cfg.get("social_twitter_follower_min", 1000)
    
    if twitter_handle:
        if twitter_followers >= twitter_min:
            social_pts += 10
        elif twitter_followers >= twitter_min * 0.3:
            social_pts += 6
        elif twitter_followers > 0:
            social_pts += 3
    
    details["twitter_handle"] = twitter_handle
    details["twitter_followers"] = twitter_followers
    
    # Narrative alignment (check token name/symbol/description for hot narratives)
    # This is a placeholder - would integrate with intelligence_tracker in production
    narratives = []
    name = (token.get("name", "") or "").lower()
    symbol = (token.get("symbol", "") or "").lower()
    description = (token.get("description", "") or "").lower()
    lifecycle_narrative = str(token.get("lifecycle_narrative") or token.get("matched_narrative") or "").strip()
    
    hot_narratives = ["ai", "agent", "trump", "maga", "solana", "defi", "nft", "gaming"]
    for narrative in hot_narratives:
        if narrative in name or narrative in symbol or narrative in description:
            narratives.append(narrative)
            social_pts += 2

    if lifecycle_narrative and lifecycle_narrative.lower() != "other":
        narratives.append(lifecycle_narrative.lower())
        social_pts += 4
    
    details["detected_narratives"] = sorted(set(narratives))
    details["lifecycle_narrative"] = lifecycle_narrative or None
    
    final_pts = min(15, max(0, social_pts))
    
    reason = (
        f"Twitter: {twitter_handle} ({twitter_followers} followers), "
        f"Narratives: {', '.join(narratives) or 'none'}"
    )
    
    return final_pts, reason, details


def score_wallet_behavior(token_or_mint, cfg: dict = None) -> tuple[int, str, dict]:
    """
    Score wallet behavior (0-15 pts) based on tracked wallet entries and cluster signals.
    
    Placeholder that integrates with wallet_tracker module.
    
    Tunable via:
    - wallet_cluster_boost_pts: 1-15 (default 5) — pts for cluster match
    - wallet_known_seed_boost_pts: 1-15 (default 8) — pts for known successful wallet
    """
    if cfg is None:
        cfg = {}
    
    details = {}
    wallet_pts = 0
    token_mint = _token_mint(token_or_mint)
    token = token_or_mint if isinstance(token_or_mint, dict) else {}

    cluster_boost = cfg.get("wallet_cluster_boost_pts", 5)
    seed_boost = cfg.get("wallet_known_seed_boost_pts", 8)
    wallet_signal = float(token.get("wallet_signal", token.get("wallet_boost", 0)) or 0)

    if wallet_signal >= 8:
        wallet_pts += 10
    elif wallet_signal >= 5:
        wallet_pts += 7
    elif wallet_signal >= 2:
        wallet_pts += 3
    details["wallet_signal"] = wallet_signal

    known_seed = False
    if _wallet_tracker and hasattr(_wallet_tracker, "check_seed_wallets"):
        try:
            known_seed = bool(_wallet_tracker.check_seed_wallets(token_mint))
        except Exception:
            known_seed = False

    cluster_match = False
    if _wallet_cluster and hasattr(_wallet_cluster, "get_cluster_signals"):
        try:
            cluster_match = bool(_wallet_cluster.get_cluster_signals(token_mint))
        except Exception:
            cluster_match = False
    
    if known_seed:
        wallet_pts += seed_boost
        details["known_seed_wallet"] = True
    
    if cluster_match:
        wallet_pts += cluster_boost
        details["cluster_match"] = True
    
    final_pts = min(15, max(0, wallet_pts))
    
    reason = (
        f"Known Seed: {known_seed}, Cluster Match: {cluster_match}, "
        f"Overall Wallet Reputation: {'good' if wallet_pts > 5 else 'neutral'}"
    )
    
    return final_pts, reason, details


def score_migration_status(token: dict, rc: dict = None, cfg: dict = None) -> tuple[int, str, dict]:
    """
    Score migration status (0-10 pts) based on token lifecycle (new, graduated, migrated).
    
    New tokens get boost, graduated tokens get boost, migrated tokens get slight penalty.
    
    Tunable via:
    - migration_new_boost_pts: 1-10 (default 8) — pts for brand new token
    - migration_grad_boost_pts: 1-10 (default 6) — pts for graduated from pump.fun
    - migration_migrated_penalty_pts: 0-10 (default 2) — pts deducted for already migrated
    """
    if cfg is None:
        cfg = {}
    if rc is None:
        rc = {}
    
    details = {}
    migr_pts = 0
    
    # Check token age
    created_ts = token.get("created_timestamp", 0)
    if created_ts:
        age_hours = (datetime.now().timestamp() - created_ts) / 3600
    else:
        age_hours = 999
    
    details["age_hours"] = age_hours
    lifecycle_state = str(token.get("lifecycle_state") or "").lower()
    details["lifecycle_state"] = lifecycle_state or None
    
    # New token boost (under 1 hour)
    new_boost = cfg.get("migration_new_boost_pts", 8)
    if lifecycle_state in {"launched", "pump_active"} or age_hours < 1:
        migr_pts = new_boost
        details["status"] = "NEW"
    else:
        details["status"] = "EXISTING"
    
    # Graduated from pump.fun bonus
    is_graduated = rc.get("is_graduated_pump_fun", False) or False
    if lifecycle_state == "migration_pending":
        is_graduated = True
    grad_boost = cfg.get("migration_grad_boost_pts", 6)
    
    if is_graduated:
        migr_pts = max(migr_pts, grad_boost)
        details["graduated_pump_fun"] = True
    
    # Migrated token penalty (already on Raydium/other DEX)
    is_migrated = rc.get("is_migrated", False) or False
    if lifecycle_state in {"raydium_live", "dex_indexed"}:
        is_migrated = True
    migrated_penalty = cfg.get("migration_migrated_penalty_pts", 2)
    
    if is_migrated and not is_graduated:
        migr_pts = max(0, migr_pts - migrated_penalty)
        details["already_migrated"] = True
    
    final_pts = min(10, max(0, migr_pts))
    
    reason = f"Status: {details.get('status', 'unknown')}, Graduated: {is_graduated}, Migrated: {is_migrated}"
    
    return final_pts, reason, details


def score_directional_bias(token_or_mint, cfg: dict = None) -> tuple[int, str, dict]:
    """
    Score directional buy/sell bias (0-10 pts) using Birdeye buy/sell pressure.
    
    Higher = more buy bias (bullish).
    
    Tunable via:
    - bias_buy_threshold_pct: 1-100 (default 70) — buy % for full points
    - bias_buy_good_threshold_pct: 1-100 (default 60) — good buy %
    """
    if cfg is None:
        cfg = {}
    
    details = {}
    token = token_or_mint if isinstance(token_or_mint, dict) else {}
    token_mint = _token_mint(token_or_mint)

    buy_ratio = 0.5  # Default neutral
    buy_count = 0
    sell_count = 0

    if token:
        buy_count = int(token.get("txns_m5_buys", 0) or 0)
        sell_count = int(token.get("txns_m5_sells", 0) or 0)
        total = buy_count + sell_count
        if total > 0:
            buy_ratio = buy_count / total
        if token.get("buy_ratio_5m") is not None:
            try:
                buy_ratio = float(token.get("buy_ratio_5m") or buy_ratio)
            except Exception:
                pass

    if _birdeye:
        try:
            import config as _cfg
            api_key = getattr(_cfg, "BIRDEYE_API_KEY", "")
            pressure = _birdeye.get_buy_sell_pressure(token_mint, api_key)
            if not pressure.get("error"):
                buy_ratio = pressure.get("buy_ratio", 0.5)
                buy_count = pressure.get("buy_count", 0)
                sell_count = pressure.get("sell_count", 0)
        except Exception:
            pass

    buy_threshold = cfg.get("bias_buy_threshold_pct", 70) / 100.0
    good_threshold = cfg.get("bias_buy_good_threshold_pct", 60) / 100.0

    if buy_ratio >= buy_threshold:
        bias_pts = 10
    elif buy_ratio >= good_threshold:
        bias_pts = 6
    elif buy_ratio > 0.5:
        bias_pts = 3
    else:
        bias_pts = 0

    details["buy_ratio_pct"] = buy_ratio * 100
    details["buy_count"] = buy_count
    details["sell_count"] = sell_count

    final_pts = min(10, max(0, bias_pts))

    direction = "BUY" if buy_ratio > 0.6 else ("SELL" if buy_ratio < 0.4 else "NEUTRAL")
    reason = f"Direction: {direction}, Buy Ratio: {buy_ratio*100:.0f}% ({buy_count}B/{sell_count}S)"

    return final_pts, reason, details


def score_volume_trend(token_mint: str, cfg: dict = None) -> tuple[int, str, dict]:
    """
    Score volume trend (0-5 pts) using GeckoTerminal OHLCV volume acceleration.
    
    Higher = stronger volume growth (bullish).
    
    Tunable via:
    - trend_explosive_threshold: 1-5 (default 5) — max points for explosive
    - trend_strong_threshold: 1-5 (default 3) — pts for strong growth
    """
    if cfg is None:
        cfg = {}
    
    details = {}

    trend_intensity = 0  # -3 to +3
    vol_1h = 0
    vol_4h = 0
    vol_24h = 0

    if _gecko:
        try:
            trend_data = _gecko.get_volume_trend(token_mint)
            if not trend_data.get("error"):
                trend_intensity = trend_data.get("trend_intensity", 0)
                vol_1h = trend_data.get("vol_1h", 0)
                vol_4h = trend_data.get("vol_4h", 0)
                vol_24h = trend_data.get("vol_24h", 0)
        except Exception:
            pass

    explosive_pts = cfg.get("trend_explosive_threshold", 5)
    strong_pts = cfg.get("trend_strong_threshold", 3)

    if trend_intensity >= 2:  # Explosive
        trend_pts = explosive_pts
    elif trend_intensity >= 1:  # Strong
        trend_pts = strong_pts
    elif trend_intensity >= 0:  # Mild
        trend_pts = 1
    else:
        trend_pts = 0

    details["trend_intensity"] = trend_intensity
    details["vol_1h"] = vol_1h
    details["vol_4h"] = vol_4h
    details["vol_24h"] = vol_24h

    final_pts = min(5, max(0, trend_pts))

    trend_type = "EXPLOSIVE" if trend_intensity >= 2 else ("STRONG" if trend_intensity >= 1 else "MILD/NEUTRAL")
    reason = f"Trend: {trend_type} (intensity: {trend_intensity:+d}), 1h: ${vol_1h:,.0f}, 24h: ${vol_24h:,.0f}"

    return final_pts, reason, details


def calculate_heat_score_v2(token: dict, rc: dict = None, cfg: dict = None) -> dict:
    """
    Complete v2 heat score calculation using 8-factor simplified model.
    
    Args:
        token: Token data dict (from pumpfun, dexscreener, etc)
        rc: RugCheck result dict
        cfg: Configuration dict (overrides from config.HEAT_SCORE_V2_DEFAULTS)
    
    Returns:
        dict with:
        - score: 0-100 normalized score
        - raw_score: 0-125 raw points before normalization
        - factors: dict {name: (points, max_points, reason)}
        - top_3: list of top 3 factors by points
        - breakdown: detailed per-factor breakdown
        - risk_level: HIGH / MEDIUM / LOW
        - scout_tier: BREWING / WARM / HOT / ULTRA_HOT
        - disqualified: reason if instantly disqualified, else None
    """
    if rc is None:
        rc = {}
    if cfg is None:
        cfg = {}
    
    # Run all 8 scoring functions
    mom_pts,  mom_reason,  mom_details  = score_momentum(token, cfg)
    liq_pts,  liq_reason,  liq_details  = score_liquidity(token, rc, cfg)
    risk_pts, risk_reason, risk_details = score_risk_safety(token, rc, cfg)
    social_pts, social_reason, social_details = score_social_narrative(token, cfg)
    wallet_pts, wallet_reason, wallet_details = score_wallet_behavior(token, cfg)
    migr_pts, migr_reason, migr_details = score_migration_status(token, rc, cfg)
    bias_pts, bias_reason, bias_details = score_directional_bias(token, cfg)
    trend_pts, trend_reason, trend_details = score_volume_trend(token.get("mint", ""), cfg)
    
    # Check for instant disqualification
    disqualified = None
    if risk_details.get("dev_disqualified"):
        disqualified = "Dev wallet >threshold"
        raw_score = 0
    elif risk_details.get("top_holder_disqualified"):
        disqualified = "Top holder >threshold"
        raw_score = 0
    elif risk_details.get("mint_authority_disqualified"):
        disqualified = "Mint authority active"
        raw_score = 0
    elif risk_details.get("danger_disqualified"):
        disqualified = "RugCheck DANGER"
        raw_score = 0
    else:
        # Calculate raw score (sum of all factors)
        raw_score = mom_pts + liq_pts + risk_pts + social_pts + wallet_pts + migr_pts + bias_pts + trend_pts
    
    # Normalize to 0-100.
    # Effective max without optional APIs (Birdeye/GeckoTerminal/Wallet bootstrap) is 90 pts:
    # momentum(20) + liquidity(20) + risk_safety(25) + social(15) + migration(10) = 90.
    # Wallets(15), directional_bias(10), volume_trend(5) contribute 0 when unconfigured.
    # Using 90 spreads scores into a meaningful range instead of compressing everything to 20-48.
    _SCORE_DENOMINATOR = 90 + wallet_pts + bias_pts + trend_pts  # expands when APIs are live
    score = int((raw_score / _SCORE_DENOMINATOR) * 100)
    score = min(100, max(0, score))
    
    # Build factors dict and top 3
    factors = {
        "momentum": {"pts": mom_pts, "max": 20, "reason": mom_reason, "details": mom_details},
        "liquidity": {"pts": liq_pts, "max": 20, "reason": liq_reason, "details": liq_details},
        "risk_safety": {"pts": risk_pts, "max": 25, "reason": risk_reason, "details": risk_details},
        "social_narrative": {"pts": social_pts, "max": 15, "reason": social_reason, "details": social_details},
        "wallets": {"pts": wallet_pts, "max": 15, "reason": wallet_reason, "details": wallet_details},
        "migration": {"pts": migr_pts, "max": 10, "reason": migr_reason, "details": migr_details},
        "directional_bias": {"pts": bias_pts, "max": 10, "reason": bias_reason, "details": bias_details},
        "volume_trend": {"pts": trend_pts, "max": 5, "reason": trend_reason, "details": trend_details},
    }
    
    # Top 3 factors
    top_3 = sorted(
        [(name, f["pts"]) for name, f in factors.items()],
        key=lambda x: x[1],
        reverse=True
    )[:3]
    
    # Determine risk level (inverse: lower score = higher risk)
    if score >= 75:
        risk_level = "LOW"
    elif score >= 50:
        risk_level = "MEDIUM"
    else:
        risk_level = "HIGH"
    
    # Scout tier (based on score thresholds, fully tunable)
    scout_brewing = cfg.get("scout_tier_brewing_threshold", 50)
    scout_warm = cfg.get("scout_tier_warm_threshold", 60)
    scout_hot = cfg.get("scout_tier_hot_threshold", 80)
    
    if score >= scout_hot:
        scout_tier = "ULTRA_HOT"
    elif score >= scout_warm:
        scout_tier = "HOT"
    elif score >= scout_brewing:
        scout_tier = "WARM"
    else:
        scout_tier = "BREWING"
    
    return {
        "score": score,
        "raw_score": raw_score,
        "max_raw": 125,
        "factors": factors,
        "top_3": top_3,
        "risk_level": risk_level,
        "scout_tier": scout_tier,
        "disqualified": disqualified,
        "timestamp": datetime.now().isoformat(),
    }
