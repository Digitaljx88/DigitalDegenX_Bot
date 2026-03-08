"""
Early Launch Hunter - Detects brand new liquidity pools on Solana blockchain
Alerts user instantly when tokens are launched with configurable liquidity filters
"""

import asyncio
import json
import os
import time
import httpx
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta


# ─────────────────────────────────────────────────────────────────────────────
# STATE MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def _load_launch_state() -> Dict:
    """Load or create launch detection state file"""
    state_file = "data/launch_hunter_state.json"
    if os.path.exists(state_file):
        with open(state_file) as f:
            return json.load(f)
    return {"seen_tokens": {}, "last_scan": 0}


def _save_launch_state(state: Dict) -> None:
    """Persist launch state to disk"""
    os.makedirs("data", exist_ok=True)
    with open("data/launch_hunter_state.json", "w") as f:
        json.dump(state, f, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# PUMP.FUN API INTEGRATION
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_new_tokens_pump_fun(limit: int = 50) -> List[Dict]:
    """
    Fetch newest tokens from Pump.fun sorted by creation time
    Returns list of token objects with metadata
    """
    try:
        url = "https://frontend-api-v3.pump.fun/coins"
        params = {
            "limit": limit,
            "sort": "created",
            "order": "desc"
        }

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, params=params)
        if response.status_code == 200:
            data = response.json()
            return data.get("coins", []) if isinstance(data, dict) else data
        return []
    except Exception as e:
        print(f"[LAUNCH] Error fetching tokens: {e}")
        return []


async def fetch_token_liquidity(mint: str) -> Optional[float]:
    """
    Get token liquidity from DexScreener
    Returns liquidity in USD or None if fetch fails
    """
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url)

        if response.status_code == 200:
            data = response.json()
            if data.get("pairs"):
                pair = data["pairs"][0]
                liquidity = pair.get("liquidity", {}).get("usd") or pair.get("liquidity", {}).get("base")
                return float(liquidity) if liquidity else None
        return None
    except Exception as e:
        print(f"[LAUNCH] Error fetching liquidity for {mint}: {e}")
        return None


async def fetch_token_age_seconds(mint: str) -> Optional[float]:
    """
    Estimate token age in seconds based on creation data
    Returns seconds since creation or None
    """
    try:
        url = f"https://frontend-api-v3.pump.fun/coin/{mint}"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url)

        if response.status_code == 200:
            data = response.json()
            created_timestamp = data.get("created_timestamp")

            if created_timestamp:
                age_seconds = time.time() - created_timestamp
                return max(0, age_seconds)
        return None
    except Exception as e:
        print(f"[LAUNCH] Error fetching age for {mint}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# LAUNCH DETECTION LOGIC
# ─────────────────────────────────────────────────────────────────────────────

async def detect_new_launches(
    min_liquidity_usd: float = 1000,
    max_age_minutes: float = 10,
    limit_check: int = 50
) -> List[Tuple[str, str, float, float]]:
    """
    Detect brand new tokens with sufficient liquidity
    
    Returns list of (mint, symbol, liquidity_usd, age_seconds)
    """
    new_launches = []
    
    # Fetch newest tokens from Pump.fun
    tokens = await fetch_new_tokens_pump_fun(limit=limit_check)
    
    for token in tokens[:limit_check]:
        try:
            mint = token.get("mint")
            symbol = token.get("symbol")
            
            if not mint or not symbol:
                continue
            
            # Get token age
            age_seconds = await fetch_token_age_seconds(mint)
            if not age_seconds or age_seconds > (max_age_minutes * 60):
                continue  # Token too old
            
            # Get liquidity
            liquidity = await fetch_token_liquidity(mint)
            if not liquidity or liquidity < min_liquidity_usd:
                continue  # Not enough liquidity
            
            new_launches.append((mint, symbol, liquidity, age_seconds))
            
        except Exception as e:
            print(f"[LAUNCH] Error checking token: {e}")
            continue
    
    return new_launches


# ─────────────────────────────────────────────────────────────────────────────
# STATE TRACKING & DEDUPLICATION
# ─────────────────────────────────────────────────────────────────────────────

def should_alert_launch(mint: str, state: Dict) -> bool:
    """Check if this token should trigger an alert (not seen before)"""
    return mint not in state.get("seen_tokens", {})


def mark_launch_alerted(mint: str, symbol: str, liquidity: float, state: Dict) -> None:
    """Mark token as alerted to prevent duplicate alerts"""
    if "seen_tokens" not in state:
        state["seen_tokens"] = {}
    
    state["seen_tokens"][mint] = {
        "symbol": symbol,
        "liquidity": liquidity,
        "alerted_time": time.time(),
        "first_detected_age": 0
    }
    _save_launch_state(state)


# ─────────────────────────────────────────────────────────────────────────────
# ALERT FORMATTING
# ─────────────────────────────────────────────────────────────────────────────

def format_launch_alert(mint: str, symbol: str, liquidity: float, age_seconds: float) -> str:
    """
    Format a launch detection alert for Telegram
    
    Example:
    🚀 NEW TOKEN LAUNCH
    Token: $SYMBOL
    Mint: ABC123...
    Liquidity: $5,234.50
    Age: 2 seconds ago
    
    🔥 ULTRA FRESH - Catch it before the crowd!
    """
    age_text = f"{int(age_seconds)}s ago" if age_seconds < 60 else f"{int(age_seconds/60)}m ago"
    heat_emoji = "🔥" if age_seconds < 30 else "👀" if age_seconds < 120 else "⚡"
    
    message = (
        f"{heat_emoji} 🚀 NEW TOKEN LAUNCH\n"
        f"Token: <b>${symbol}</b>\n"
        f"Mint: <code>{mint[:16]}...</code>\n"
        f"Liquidity: <b>${liquidity:,.2f}</b>\n"
        f"Age: <b>{age_text}</b>\n"
        f"\n"
    )
    
    if age_seconds < 30:
        message += "🔥 ULTRA FRESH - You're among the first!\n"
    elif age_seconds < 120:
        message += "⚡ VERY FRESH - Early bird opportunity\n"
    else:
        message += "👀 Still Fresh - Window closing\n"
    
    message += (
        f"\n"
        f"<a href='https://dexscreener.com/solana/{mint}'>View on DexScreener</a> | "
        f"<a href='https://pump.fun/coin/{mint}'>Pump.fun</a>\n"
    )
    
    return message


# ─────────────────────────────────────────────────────────────────────────────
# MAIN DETECTION LOOP
# ─────────────────────────────────────────────────────────────────────────────

async def check_for_new_launches(
    bot,
    launch_channel_id: int,
    min_liquidity: float = 1000,
    max_age_minutes: float = 10
) -> List[Tuple[str, str]]:
    """
    Check for new launches and send alerts
    
    Returns list of (mint, symbol) for tokens that triggered alerts
    """
    try:
        state = _load_launch_state()
        alerts_sent = []
        
        # Detect new launches
        launches = await detect_new_launches(
            min_liquidity_usd=min_liquidity,
            max_age_minutes=max_age_minutes,
            limit_check=50
        )
        
        # Process each new launch
        for mint, symbol, liquidity, age_seconds in launches:
            # Check if we've already alerted for this token
            if not should_alert_launch(mint, state):
                continue
            
            # Format and send alert
            try:
                message = format_launch_alert(mint, symbol, liquidity, age_seconds)
                await bot.send_message(
                    chat_id=launch_channel_id,
                    text=message,
                    parse_mode="HTML"
                )
                
                # Mark as alerted
                mark_launch_alerted(mint, symbol, liquidity, state)
                alerts_sent.append((mint, symbol))
                
                print(f"[LAUNCH] 🚀 Alert sent: ${symbol} - ${liquidity:,.0f} liquidity")
                
            except Exception as e:
                print(f"[LAUNCH] Failed to send alert for {symbol}: {e}")
        
        return alerts_sent
        
    except Exception as e:
        print(f"[LAUNCH] Critical error in launch detection: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# STATISTICS & REPORTING
# ─────────────────────────────────────────────────────────────────────────────

def get_launch_stats() -> Dict:
    """Get statistics about detected launches"""
    state = _load_launch_state()
    seen = state.get("seen_tokens", {})
    
    if not seen:
        return {"total_launches_detected": 0, "recent": []}
    
    # Sort by alert time (most recent first)
    recent = sorted(
        seen.items(),
        key=lambda x: x[1].get("alerted_time", 0),
        reverse=True
    )[:20]  # Last 20
    
    return {
        "total_launches_detected": len(seen),
        "recent": [
            {
                "symbol": v.get("symbol"),
                "mint": k[:16] + "...",
                "liquidity": f"${v.get('liquidity', 0):,.0f}",
                "detected_ago_min": int((time.time() - v.get("alerted_time", 0)) / 60)
            }
            for k, v in recent
        ]
    }


def format_launch_stats_message() -> str:
    """Format launch statistics for Telegram"""
    stats = get_launch_stats()
    total = stats["total_launches_detected"]
    
    if total == 0:
        return "🚀 Early Launch Hunter\n\nNo launches detected yet.\n\nWatching for new tokens..."
    
    message = f"🚀 <b>Early Launch Hunter</b>\n\nTotal launches detected: <b>{total}</b>\n\n"
    message += "<b>Recent Launches:</b>\n"
    
    for launch in stats["recent"][:10]:
        message += (
            f"• ${launch['symbol']} - {launch['liquidity']} ({launch['detected_ago_min']}m ago)\n"
        )
    
    return message
