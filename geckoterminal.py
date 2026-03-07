"""
GeckoTerminal API integration for volume and OHLCV trend analysis.

Provides:
- 1H volume trend detection (accelerating vs declining)
- OHLCV candle data for chart building
- Multi-timeframe analysis
- Pump/dump pattern detection
"""

import requests
import time
from typing import Optional


# GeckoTerminal API endpoints
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v1"
GECKOTERMINAL_OHLCV = f"{GECKOTERMINAL_BASE}/simple/networks/solana/token_ohlcvs"
GECKOTERMINAL_POOL = f"{GECKOTERMINAL_BASE}/simple/networks/solana/pools"

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


def get_volume_trend(mint: str) -> dict:
    """
    Analyze 1-hour volume trend to detect pumps/dumps.
    
    Returns:
        {
            "vol_1h": float,           # Volume in last 1h
            "vol_4h": float,           # Average volume in 4h window
            "vol_24h": float,          # Average volume in 24h
            "trend": "UP" | "DOWN" | "STEADY",
            "trend_intensity": -3 to 3,  # -3 = collapsing, 0 = steady, 3 = explosive
            "trend_score": 0-5,        # Heat score bonus points
            "candles": list,           # Last 24 1H candles
            "error": str | None
        }
    """
    cache_key = f"trend_{mint}"
    cached = _get_cached(cache_key)
    if cached:
        return cached

    try:
        r = requests.get(
            GECKOTERMINAL_OHLCV,
            params={
                "token_addresses": mint,
                "timeframe": "1h",
            },
            timeout=5
        )
        r.raise_for_status()
        data = r.json()

        if data.get("data") is None or not data["data"].get("attributes", {}).get("ohlcv_list"):
            return {
                "vol_1h": 0, "vol_4h": 0, "vol_24h": 0,
                "trend": "STEADY", "trend_intensity": 0, "trend_score": 0,
                "candles": [], "error": "No OHLCV data"
            }

        # GeckoTerminal returns candles as [timestamp, open, high, low, close, volume]
        ohlcv_list = data["data"]["attributes"]["ohlcv_list"]
        if not ohlcv_list:
            return {
                "vol_1h": 0, "vol_4h": 0, "vol_24h": 0,
                "trend": "STEADY", "trend_intensity": 0, "trend_score": 0,
                "candles": [], "error": "Empty candle list"
            }

        # Sort chronologically (newest last)
        ohlcv_list.sort(key=lambda x: x[0])

        # Build candle list and extract volumes
        candles = []
        volumes = []
        for candle in ohlcv_list[-24:]:  # Last 24 candles = last 24 hours
            ts, o, h, l, c, v = candle
            volumes.append(float(v) if v else 0)
            candles.append({
                "ts": ts,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v) if v else 0
            })

        if not volumes:
            return {
                "vol_1h": 0, "vol_4h": 0, "vol_24h": 0,
                "trend": "STEADY", "trend_intensity": 0, "trend_score": 0,
                "candles": candles, "error": "No volume data"
            }

        # Calculate averages
        vol_1h = volumes[-1] if volumes else 0  # Last 1h
        vol_4h = sum(volumes[-4:]) / 4 if len(volumes) >= 4 else sum(volumes) / len(volumes)
        vol_24h = sum(volumes) / len(volumes) if volumes else 0

        # Trend analysis: is volume increasing?
        if len(volumes) >= 4:
            recent_vol = sum(volumes[-4:]) / 4  # Last 4h average
            older_vol = sum(volumes[:-4]) / (len(volumes) - 4) if len(volumes) > 4 else recent_vol
        else:
            recent_vol = vol_1h
            older_vol = vol_24h

        # Calculate trend intensity (-3 to +3)
        if older_vol > 0:
            vol_ratio = recent_vol / older_vol
            if vol_ratio >= 3.0:
                trend_intensity = 3
                trend = "UP"
            elif vol_ratio >= 2.0:
                trend_intensity = 2
                trend = "UP"
            elif vol_ratio >= 1.5:
                trend_intensity = 1
                trend = "UP"
            elif vol_ratio <= 0.33:
                trend_intensity = -3
                trend = "DOWN"
            elif vol_ratio <= 0.5:
                trend_intensity = -2
                trend = "DOWN"
            elif vol_ratio <= 0.7:
                trend_intensity = -1
                trend = "DOWN"
            else:
                trend_intensity = 0
                trend = "STEADY"
        else:
            trend_intensity = 0
            trend = "STEADY"

        # Score: volume trend bonus for upward momentum
        if trend_intensity == 3:
            trend_score = 5  # Explosive upside
        elif trend_intensity == 2:
            trend_score = 3  # Strong upside
        elif trend_intensity == 1:
            trend_score = 1  # Mild upside
        else:
            trend_score = 0  # No bonus for neutral or down

        result = {
            "vol_1h": vol_1h,
            "vol_4h": vol_4h,
            "vol_24h": vol_24h,
            "trend": trend,
            "trend_intensity": trend_intensity,
            "trend_score": trend_score,
            "candles": candles,
            "error": None
        }
        _set_cache(cache_key, result)
        return result

    except requests.exceptions.Timeout:
        return {
            "vol_1h": 0, "vol_4h": 0, "vol_24h": 0,
            "trend": "STEADY", "trend_intensity": 0, "trend_score": 0,
            "candles": [], "error": "timeout"
        }
    except Exception as e:
        return {
            "vol_1h": 0, "vol_4h": 0, "vol_24h": 0,
            "trend": "STEADY", "trend_intensity": 0, "trend_score": 0,
            "candles": [], "error": str(e)
        }


def detect_pump_dump(mint: str) -> dict:
    """
    Detect pump/dump patterns in price actionvs volume.
    
    Returns:
        {
            "pattern": "PUMP" | "DUMP" | "ACCUMULATION" | "NEUTRAL",
            "confidence": 0-100,  # How confident we are about the pattern
            "signal_score": 0-5,  # Bonus pts for scanner if strong pattern
            "description": str,
            "error": str | None
        }
    """
    trend_data = get_volume_trend(mint)
    if trend_data.get("error"):
        return {
            "pattern": "NEUTRAL",
            "confidence": 0,
            "signal_score": 0,
            "description": "Cannot analyze pattern",
            "error": trend_data["error"]
        }

    candles = trend_data.get("candles", [])
    if len(candles) < 4:
        return {
            "pattern": "NEUTRAL",
            "confidence": 0,
            "signal_score": 0,
            "description": "Insufficient candles",
            "error": "Need at least 4 candles"
        }

    # Look at price and volume over last 4 candles
    recent = candles[-4:]
    price_start = recent[0].get("open", 0)
    price_end = recent[-1].get("close", 0)
    price_change = ((price_end - price_start) / price_start * 100) if price_start else 0

    vol_recent = sum(c["volume"] for c in recent) / 4
    vol_older = sum(c["volume"] for c in candles[:-4]) / (len(candles) - 4) if len(candles) > 4 else vol_recent
    vol_ratio = vol_recent / vol_older if vol_older > 0 else 1

    # Pattern detection
    if price_change > 50 and vol_ratio > 1.5:
        pattern = "PUMP"
        confidence = min(100, int(price_change + (vol_ratio * 20)))
        signal_score = 3
        description = f"Price +{price_change:.1f}% with {vol_ratio:.1f}x volume"
    elif price_change < -50 and vol_ratio > 1.2:
        pattern = "DUMP"
        confidence = min(100, int(abs(price_change) + (vol_ratio * 20)))
        signal_score = 0  # No bonus for dumps
        description = f"Price {price_change:.1f}% with {vol_ratio:.1f}x volume (sell pressure)"
    elif price_change > 5 and vol_ratio > 0.8:
        pattern = "ACCUMULATION"
        confidence = int(price_change * 2)
        signal_score = 1
        description = f"Gradual accumulation: +{price_change:.1f}% price, stable volume"
    else:
        pattern = "NEUTRAL"
        confidence = 50
        signal_score = 0
        description = f"Price {price_change:+.1f}%, vol ratio {vol_ratio:.2f}x"

    return {
        "pattern": pattern,
        "confidence": confidence,
        "signal_score": signal_score,
        "description": description,
        "error": None
    }


def get_pool_info(mint: str) -> dict:
    """
    Get pool liquidity info from GeckoTerminal.
    
    Returns:
        {
            "liquidity_usd": float,
            "fdv": float,
            "price": float,
            "error": str | None
        }
    """
    cache_key = f"pool_{mint}"
    cached = _get_cached(cache_key)
    if cached:
        return cached

    try:
        r = requests.get(
            f"{GECKOTERMINAL_BASE}/search/pools",
            params={
                "query": mint,
                "network": "solana"
            },
            timeout=5
        )
        r.raise_for_status()
        data = r.json()

        pools = data.get("data", [])
        if not pools:
            return {
                "liquidity_usd": 0,
                "fdv": 0,
                "price": 0,
                "error": "No pool data"
            }

        pool = pools[0]
        attrs = pool.get("attributes", {})

        result = {
            "liquidity_usd": float(attrs.get("reserve_in_usd", 0) or 0),
            "fdv": float(attrs.get("fully_diluted_valuation_usd", 0) or 0),
            "price": float(attrs.get("token_price_usd", 0) or 0),
            "error": None
        }
        _set_cache(cache_key, result)
        return result

    except Exception as e:
        return {
            "liquidity_usd": 0,
            "fdv": 0,
            "price": 0,
            "error": str(e)
        }


def clear_cache():
    """Clear the cache (useful for testing)."""
    global _cache
    _cache = {}
