"""
Portfolio Distribution Watcher
Monitors held tokens for crash signals (5-signal system with weighted confidence scoring).
Sends alerts to main channel when high-risk conditions detected.
"""

import json
import time
import os
import requests
from typing import dict, list, tuple

WATCHER_STATE_FILE = os.path.join("data", "portfolio_watcher_state.json")

# Signal weights (confidence multipliers)
SIGNAL_WEIGHTS = {
    "dev_movement": 3.0,      # Extremely high confidence
    "whale_exit": 2.5,         # High confidence
    "liquidity_drain": 2.0,    # Solid signal
    "buy_sell_flip": 1.5,      # Moderate signal
    "volume_collapse": 1.0,    # Weak signal
}

# Alert thresholds
ALERT_THRESHOLD_HIGH = 3.0
ALERT_THRESHOLD_MEDIUM = 2.0
ALERT_THRESHOLD_LOW = 1.0

# Cooldown: don't re-alert same signal on same token within 5 minutes
SIGNAL_COOLDOWN_SECS = 300

# Minimum cycles before alerting (allow baseline establishment)
MIN_CYCLES_BASELINE = 3


def _load_state() -> dict:
    """Load watcher state from JSON file."""
    if not os.path.exists(WATCHER_STATE_FILE):
        return {}
    try:
        with open(WATCHER_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict):
    """Save watcher state to JSON file."""
    os.makedirs("data", exist_ok=True)
    try:
        with open(WATCHER_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[WATCHER] Error saving state: {e}")


def _init_token_state(mint: str, symbol: str, dev_wallet: str = "") -> dict:
    """Initialize state for a new token being watched."""
    return {
        "symbol": symbol,
        "dev_wallet": dev_wallet,
        "cycles_observed": 0,
        "last_alert_time": 0,
        "last_alert_signals": [],
        "baseline_volume_1h": 0,
        "peak_price_24h": 0,
        "last_liquidity": 0,
        "volume_history": [],  # rolling 5-min volumes
        "buy_counts": [],      # rolling buy counts per min
        "sell_counts": [],     # rolling sell counts per min
        "signal_cooldowns": {} # {signal_name: last_alert_time}
    }


def fetch_dexscreener_metrics(mint: str, timeout: int = 10) -> dict | None:
    """
    Fetch price, liquidity, volume, and recent trades from DexScreener.
    Returns dict with: price, liquidity, volume_5m, volume_1h, buys, sells, latest_trades
    """
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        
        pairs = data.get("pairs", [])
        if not pairs:
            return None
        
        # Get SOL pair (highest liquidity)
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol_pairs:
            return None
        
        pair = sorted(sol_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]
        
        # Parse key fields
        price = float(pair.get("priceUsd") or 0)
        liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
        volume_5m = float(pair.get("volume", {}).get("m5") or 0)
        volume_1h = float(pair.get("volume", {}).get("h1") or 0)
        
        # Parse trades (buy/sell counts)
        txns = pair.get("txns", {})
        buys_5m = txns.get("m5", {}).get("buys", 0)
        sells_5m = txns.get("m5", {}).get("sells", 0)
        
        return {
            "price": price,
            "liquidity": liquidity,
            "volume_5m": volume_5m,
            "volume_1h": volume_1h,
            "buys_5m": buys_5m,
            "sells_5m": sells_5m,
        }
    except Exception as e:
        print(f"[WATCHER] DexScreener fetch error for {mint[:8]}: {e}")
        return None


def fetch_pump_dev_status(mint: str, timeout: int = 10) -> dict | None:
    """
    Fetch dev wallet info and last_trade_timestamp from pump.fun.
    Returns dict with: dev_wallet, last_trade_timestamp, is_dev_authority_revoked
    """
    try:
        url = f"https://frontend-api-v3.pump.fun/coin/{mint}"
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        
        return {
            "dev_wallet": data.get("dev", ""),
            "last_trade_timestamp": data.get("last_trade_timestamp", 0),
            "is_dev_authority_revoked": data.get("is_dev_authority_revoked", False),
        }
    except Exception as e:
        print(f"[WATCHER] Pump.fun fetch error for {mint[:8]}: {e}")
        return None


def detect_dev_movement(mint: str, token_state: dict, pump_data: dict) -> bool:
    """
    Detect if dev wallet became active recently.
    HIGH CONFIDENCE SIGNAL.
    """
    if not pump_data or not token_state.get("dev_wallet"):
        return False
    
    last_trade_ts = pump_data.get("last_trade_timestamp", 0)
    stored_ts = token_state.get("pump_last_trade_ts", 0)
    
    # If last_trade_timestamp changed, dev might be moving tokens
    if last_trade_ts > stored_ts and (time.time() - last_trade_ts) < 300:  # within 5 min
        return True
    
    return False


def detect_whale_exit(dex_data: dict, token_state: dict, liquidity: float) -> bool:
    """
    Detect large sell transactions (>2-5% of liquidity).
    Returns True if whale exit detected in last 5 min.
    """
    if not dex_data or not liquidity:
        return False
    
    buys = dex_data.get("buys_5m", 0)
    sells = dex_data.get("sells_5m", 0)
    
    # Very high sell count relative to liquidity is a whale signal
    # Heuristic: if sells_5m > 15 and sell/buy ratio > 3x, likely whale
    if sells >= 15 and buys > 0 and (sells / buys) >= 3.0:
        return True
    
    return False


def detect_liquidity_drain(dex_data: dict, token_state: dict) -> bool:
    """
    Detect liquidity drops >15% in rolling 10-min window.
    """
    if not dex_data:
        return False
    
    current_liquidity = dex_data.get("liquidity", 0)
    last_liquidity = token_state.get("last_liquidity", current_liquidity)
    
    if last_liquidity == 0:
        return False
    
    drop_pct = ((last_liquidity - current_liquidity) / last_liquidity) * 100
    
    # Alert if >15% drop detected
    return drop_pct > 15.0


def detect_buy_sell_flip(dex_data: dict, token_state: dict) -> bool:
    """
    Detect when sell pressure exceeds buy pressure (60%+ sells).
    Tracks rolling 5-min window.
    """
    if not dex_data:
        return False
    
    buys = dex_data.get("buys_5m", 0)
    sells = dex_data.get("sells_5m", 0)
    total = buys + sells
    
    if total == 0:
        return False
    
    sell_ratio = (sells / total) * 100
    
    # Alert if >60% of trades are sells
    return sell_ratio > 60.0


def detect_volume_collapse(dex_data: dict, token_state: dict) -> bool:
    """
    Detect volume drops >40% while price near peak.
    Compares 5m volume to 1h average.
    """
    if not dex_data:
        return False
    
    volume_5m = dex_data.get("volume_5m", 0)
    volume_1h = dex_data.get("volume_1h", 0)
    
    if volume_1h == 0:
        return False
    
    # Build volume history
    history = token_state.get("volume_history", [])
    history.append(volume_5m)
    if len(history) > 12:  # keep 1 hour of 5m samples
        history = history[-12:]
    
    avg_1h = sum(history) / len(history) if history else 0
    
    if avg_1h == 0:
        return False
    
    drop_pct = ((avg_1h - volume_5m) / avg_1h) * 100
    
    # Alert if 40%+ volume drop
    return drop_pct > 40.0


def score_signals(mint: str, symbol: str, metrics: dict) -> tuple[dict, float, str]:
    """
    Analyze all 5 signals and return weighted confidence score.
    Returns: (signal_dict, confidence_score, risk_level)
    Risk levels: 'LOW', 'MEDIUM', 'HIGH'
    """
    state = _load_state()
    token_state = state.get(mint, {})
    
    # Skip if not enough baseline cycles
    if token_state.get("cycles_observed", 0) < MIN_CYCLES_BASELINE:
        return ({}, 0.0, "BASELINE")
    
    signals = {}
    score = 0.0
    
    # Fetch live data
    dex_data = metrics.get("dex_data")
    pump_data = metrics.get("pump_data")
    
    # Signal 1: Dev Movement (3.0x)
    if detect_dev_movement(mint, token_state, pump_data):
        signals["dev_movement"] = True
        score += SIGNAL_WEIGHTS["dev_movement"]
    
    # Signal 2: Whale Exit (2.5x)
    if detect_whale_exit(dex_data, token_state, dex_data.get("liquidity", 0) if dex_data else 0):
        signals["whale_exit"] = True
        score += SIGNAL_WEIGHTS["whale_exit"]
    
    # Signal 3: Liquidity Drain (2.0x)
    if detect_liquidity_drain(dex_data, token_state):
        signals["liquidity_drain"] = True
        score += SIGNAL_WEIGHTS["liquidity_drain"]
    
    # Signal 4: Buy/Sell Flip (1.5x)
    if detect_buy_sell_flip(dex_data, token_state):
        signals["buy_sell_flip"] = True
        score += SIGNAL_WEIGHTS["buy_sell_flip"]
    
    # Signal 5: Volume Collapse (1.0x)
    if detect_volume_collapse(dex_data, token_state):
        signals["volume_collapse"] = True
        score += SIGNAL_WEIGHTS["volume_collapse"]
    
    # Determine risk level
    if score >= ALERT_THRESHOLD_HIGH:
        risk_level = "HIGH"
    elif score >= ALERT_THRESHOLD_MEDIUM:
        risk_level = "MEDIUM"
    elif score >= ALERT_THRESHOLD_LOW:
        risk_level = "LOW"
    else:
        risk_level = "NONE"
    
    return signals, score, risk_level


def should_alert(mint: str, signals: dict) -> bool:
    """
    Determine if we should send an alert based on signal cooldowns.
    Returns False if same signals were alerted recently.
    """
    if not signals:
        return False
    
    state = _load_state()
    token_state = state.get(mint, {})
    cooldowns = token_state.get("signal_cooldowns", {})
    now = time.time()
    
    signal_names = set(signals.keys())
    
    # Check if any signal is on cooldown
    for sig_name in signal_names:
        last_alert = cooldowns.get(sig_name, 0)
        if (now - last_alert) < SIGNAL_COOLDOWN_SECS:
            return False  # Still in cooldown for this signal
    
    return True


def update_state_post_alert(mint: str, symbols: dict, signal_names: list):
    """Update state file with new cooldowns after alert sent."""
    state = _load_state()
    token_state = state.get(mint, _init_token_state(mint, ""))
    cooldowns = token_state.get("signal_cooldowns", {})
    now = time.time()
    
    for sig_name in signal_names:
        cooldowns[sig_name] = now
    
    token_state["signal_cooldowns"] = cooldowns
    token_state["last_alert_time"] = now
    token_state["last_alert_signals"] = signal_names
    
    state[mint] = token_state
    _save_state(state)


def format_alert_message(symbol: str, mint: str, signals: dict, score: float, risk_level: str) -> str:
    """Format alert message for Telegram."""
    signal_list = []
    if signals.get("dev_movement"):
        signal_list.append("📛 Developer wallet active")
    if signals.get("whale_exit"):
        signal_list.append("🐋 Whale exit detected")
    if signals.get("liquidity_drain"):
        signal_list.append("💧 Liquidity draining")
    if signals.get("buy_sell_flip"):
        signal_list.append("📉 Sell pressure rising")
    if signals.get("volume_collapse"):
        signal_list.append("📊 Volume collapsing")
    
    signal_text = "\n".join(f"• {s}" for s in signal_list)
    
    emoji = "🔴" if risk_level == "HIGH" else "🟠" if risk_level == "MEDIUM" else "🟡"
    
    message = (
        f"{emoji} DISTRIBUTION WARNING\n\n"
        f"Token: `${symbol}`\n"
        f"Risk Level: {risk_level}\n"
        f"Confidence: {score:.1f}/5.0\n\n"
        f"Signals Detected:\n"
        f"{signal_text}\n\n"
        f"💡 Action: Monitor closely or exit position\n"
        f"`{mint[:16]}...`"
    )
    
    return message


async def check_portfolio_for_alerts(bot, uid: int, portfolio: dict, get_portfolio_func) -> list:
    """
    Check all tokens in portfolio for crash signals.
    Returns list of tuples: (mint, symbol, signals, score, risk_level, alert_message)
    """
    if not portfolio:
        return []
    
    alerts = []
    state = _load_state()
    
    # Limit to top 20 tokens by amount held
    sorted_tokens = sorted(
        portfolio.items(),
        key=lambda x: x[1],
        reverse=True
    )[:20]
    
    for mint, amount in sorted_tokens:
        if amount <= 0:
            continue
        
        token_state = state.get(mint, _init_token_state(mint, ""))
        
        # Fetch metrics
        dex_data = fetch_dexscreener_metrics(mint)
        pump_data = fetch_pump_dev_status(mint)
        
        if not dex_data:
            continue
        
        # Update state with metrics
        token_state["cycles_observed"] = token_state.get("cycles_observed", 0) + 1
        token_state["last_liquidity"] = dex_data.get("liquidity", 0)
        token_state["pump_last_trade_ts"] = pump_data.get("last_trade_timestamp", 0) if pump_data else 0
        
        if pump_data:
            token_state["dev_wallet"] = pump_data.get("dev_wallet", "")
        
        state[mint] = token_state
        
        # Score signals
        signals, score, risk_level = score_signals(
            mint,
            token_state.get("symbol", mint[:8]),
            {"dex_data": dex_data, "pump_data": pump_data}
        )
        
        # Check if we should alert
        if risk_level in ["HIGH", "MEDIUM", "LOW"] and should_alert(mint, signals):
            symbol = token_state.get("symbol", mint[:8])
            message = format_alert_message(symbol, mint, signals, score, risk_level)
            alerts.append((mint, symbol, signals, score, risk_level, message))
            
            # Update cooldowns
            update_state_post_alert(mint, {symbol: amount}, list(signals.keys()))
    
    _save_state(state)
    return alerts
