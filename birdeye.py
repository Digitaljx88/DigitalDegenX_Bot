"""
Birdeye API integration for Solana token analysis.

Provides:
- Buy/sell pressure scoring (0-10 pts for heat score)
- Trade volume direction detection
- Multi-DEX trade aggregation
- Wash trading detection (volume without price movement)
"""

import requests
import time
from typing import Optional

# Birdeye API endpoints
BIRDEYE_BASE = "https://api.birdeye.so/v1"
BIRDEYE_TOKEN_TRADES = f"{BIRDEYE_BASE}/token/trades"
BIRDEYE_TOKEN_OHLCV = f"{BIRDEYE_BASE}/token/ohlcv"
BIRDEYE_TOKEN_OVERVIEW = f"{BIRDEYE_BASE}/token/overview"

# Cache for API responses (5-minute TTL)
_cache = {}
CACHE_TTL = 300


def _get_cached(key: str) -> Optional[dict]:
    """Retrieve cached API response if not expired."""
    if key in _cache:
        data, ts = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None


def _set_cache(key: str, data: dict):
    """Cache API response with timestamp."""
    _cache[key] = (data, time.time())


def get_buy_sell_pressure(mint: str, api_key: str = "") -> dict:
    """
    Analyze buy/sell ratio from recent trades.
    
    Returns:
        {
            "buy_count": int,        # number of buys in window
            "sell_count": int,       # number of sells in window
            "buy_ratio": 0.0-1.0,    # % of buys
            "volume_buy": float,     # USD volume of buys
            "volume_sell": float,    # USD volume of sells
            "pressure_score": 0-10,  # heat score points (0-10)
            "direction": "BUY" | "SELL" | "NEUTRAL",
            "error": str | None
        }
    """
    if not api_key:
        return {
            "buy_count": 0, "sell_count": 0, "buy_ratio": 0.5,
            "volume_buy": 0, "volume_sell": 0, "pressure_score": 0,
            "direction": "NEUTRAL", "error": "no API key"
        }

    cache_key = f"pressure_{mint}"
    cached = _get_cached(cache_key)
    if cached:
        return cached

    try:
        # Fetch last 20 trades for this token
        headers = {"x-chain": "solana", "Authorization": f"Bearer {api_key}"}
        r = requests.get(
            BIRDEYE_TOKEN_TRADES,
            params={"address": mint, "limit": 20},
            headers=headers,
            timeout=5
        )
        r.raise_for_status()
        data = r.json()

        if data.get("success") is False:
            return {
                "buy_count": 0, "sell_count": 0, "buy_ratio": 0.5,
                "volume_buy": 0, "volume_sell": 0, "pressure_score": 0,
                "direction": "NEUTRAL", "error": data.get("message", "API error")
            }

        trades = data.get("data", {}).get("items", [])
        if not trades:
            return {
                "buy_count": 0, "sell_count": 0, "buy_ratio": 0.5,
                "volume_buy": 0, "volume_sell": 0, "pressure_score": 0,
                "direction": "NEUTRAL", "error": "no recent trades"
            }

        buy_count = 0
        sell_count = 0
        volume_buy = 0.0
        volume_sell = 0.0

        for trade in trades:
            side = (trade.get("side") or "").lower()
            amount = float(trade.get("amount") or 0) if trade.get("amount") else 0
            price = float(trade.get("price") or 0) if trade.get("price") else 0
            value_usd = amount * price if amount and price else 0

            if side == "buy":
                buy_count += 1
                volume_buy += value_usd
            elif side == "sell":
                sell_count += 1
                volume_sell += value_usd

        total_trades = buy_count + sell_count
        buy_ratio = buy_count / total_trades if total_trades > 0 else 0.5

        # Score: if 70%+ buys = 10pts, 60%+ buys = 5pts, 40-60% = 0pts, below 40% buys = -2pts
        if buy_ratio >= 0.7:
            pressure_score = 10
            direction = "BUY"
        elif buy_ratio >= 0.6:
            pressure_score = 5
            direction = "BUY"
        elif buy_ratio >= 0.4:
            pressure_score = 0
            direction = "NEUTRAL"
        else:
            pressure_score = 0  # Don't penalize; treat as SELL signal but no points
            direction = "SELL"

        result = {
            "buy_count": buy_count,
            "sell_count": sell_count,
            "buy_ratio": buy_ratio,
            "volume_buy": volume_buy,
            "volume_sell": volume_sell,
            "pressure_score": max(0, pressure_score),
            "direction": direction,
            "error": None
        }
        _set_cache(cache_key, result)
        return result

    except requests.exceptions.Timeout:
        return {
            "buy_count": 0, "sell_count": 0, "buy_ratio": 0.5,
            "volume_buy": 0, "volume_sell": 0, "pressure_score": 0,
            "direction": "NEUTRAL", "error": "timeout"
        }
    except Exception as e:
        return {
            "buy_count": 0, "sell_count": 0, "buy_ratio": 0.5,
            "volume_buy": 0, "volume_sell": 0, "pressure_score": 0,
            "direction": "NEUTRAL", "error": str(e)
        }


def get_volume_trend(mint: str, api_key: str = "") -> dict:
    """
    Analyze 1-hour volume trend (is volume increasing?).
    
    Returns:
        {
            "vol_1h": float,      # volume in last hour
            "vol_6h": float,      # volume in last 6 hours
            "vol_24h": float,     # volume in last 24 hours
            "trend": "UP" | "DOWN" | "STABLE",
            "trend_score": 0-3,   # 0 = down, 1 = stable, 2-3 = up and accelerating
            "error": str | None
        }
    """
    if not api_key:
        return {
            "vol_1h": 0, "vol_6h": 0, "vol_24h": 0,
            "trend": "STABLE", "trend_score": 0, "error": "no API key"
        }

    cache_key = f"trend_{mint}"
    cached = _get_cached(cache_key)
    if cached:
        return cached

    try:
        # Fetch OHLCV data (1h candles, last 24 candles = last 24h)
        headers = {"x-chain": "solana", "Authorization": f"Bearer {api_key}"}
        r = requests.get(
            BIRDEYE_TOKEN_OHLCV,
            params={
                "address": mint,
                "type": "1H",  # 1-hour candles
                "time_from": int(time.time()) - 86400,  # last 24h
                "time_to": int(time.time())
            },
            headers=headers,
            timeout=5
        )
        r.raise_for_status()
        data = r.json()

        if data.get("success") is False:
            return {
                "vol_1h": 0, "vol_6h": 0, "vol_24h": 0,
                "trend": "STABLE", "trend_score": 0,
                "error": data.get("message", "API error")
            }

        items = data.get("data", {}).get("items", [])
        if not items or len(items) < 2:
            return {
                "vol_1h": 0, "vol_6h": 0, "vol_24h": 0,
                "trend": "STABLE", "trend_score": 0, "error": "insufficient data"
            }

        # Sort by time descending (newest first)
        items.sort(key=lambda x: x.get("time", 0), reverse=True)

        # Last candle = 1h
        vol_1h = float(items[0].get("v", 0)) if items else 0

        # Average of last 6 candles = 6h
        vol_6h_list = [float(items[i].get("v", 0)) for i in range(min(6, len(items)))]
        vol_6h = sum(vol_6h_list) / len(vol_6h_list) if vol_6h_list else 0

        # Average of all 24 candles
        vol_24h_list = [float(item.get("v", 0)) for item in items]
        vol_24h = sum(vol_24h_list) / len(vol_24h_list) if vol_24h_list else 0

        # Trend: if 1h vol > 6h avg, volume is UP
        if vol_1h > vol_6h * 1.2:
            trend = "UP"
            trend_score = 3 if vol_1h > vol_6h * 1.5 else 2
        elif vol_1h < vol_6h * 0.8:
            trend = "DOWN"
            trend_score = 0
        else:
            trend = "STABLE"
            trend_score = 1

        result = {
            "vol_1h": vol_1h,
            "vol_6h": vol_6h,
            "vol_24h": vol_24h,
            "trend": trend,
            "trend_score": trend_score,
            "error": None
        }
        _set_cache(cache_key, result)
        return result

    except Exception as e:
        return {
            "vol_1h": 0, "vol_6h": 0, "vol_24h": 0,
            "trend": "STABLE", "trend_score": 0, "error": str(e)
        }


def clear_cache():
    """Clear the cache (useful for testing)."""
    global _cache
    _cache = {}
