"""
Portfolio Alert System: Automatic mcap milestone tracking
Alerts on every 2x gain (up) or 2x loss (50% down) for all purchased tokens
"""

import json
import os
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
AUTO_SELL_FILE = os.path.join(DATA_DIR, "auto_sell.json")


def _load_json(path):
    """Load JSON file safely."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"[PORTFOLIO_ALERTS] Error loading {path}: {e}")
        return {}


def _save_json(path, data):
    """Save JSON file safely."""
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[PORTFOLIO_ALERTS] Error saving {path}: {e}")


def load_auto_sell():
    """Load auto-sell configs (each token has mcap history)."""
    return _load_json(AUTO_SELL_FILE)


def save_auto_sell(data):
    """Save auto-sell configs."""
    _save_json(AUTO_SELL_FILE, data)


def should_alert_on_mcap_milestone(token_mint: str, current_mcap: float, cfg: dict) -> tuple[bool, str, float]:
    """
    Check if current mcap crosses a 2x milestone from purchase.
    
    Returns:
        (should_alert, direction, mcap_multiplier)
    
    Example:
        Purchase mcap: $10,000
        Current mcap: $20,000 → Should alert: +2x UP
        Current mcap: $5,000 → Should alert: -50% DOWN (2x drop)
    """
    try:
        purchase_mcap = float(cfg.get("purchase_mcap", 0))
        if purchase_mcap <= 0 or current_mcap <= 0:
            return False, "", 1.0
        
        # Get the last alerted mcap level
        milestones = cfg.get("alert_milestones", {})
        last_alerted_mcap = float(milestones.get("last_alerted_mcap", purchase_mcap))
        
        # Calculate multipliers
        current_multiplier = current_mcap / purchase_mcap
        last_multiplier = last_alerted_mcap / purchase_mcap
        
        # UP Movement: Every 2x increase
        # Example: buy at $10k, alert at $20k (+2x), then $40k (+4x), then $80k (+8x)
        if current_mcap > last_alerted_mcap:
            # Find next 2x threshold above current level
            next_threshold = last_alerted_mcap * 2
            if current_mcap >= next_threshold:
                # Calculate exact multiplier
                multiplier = current_mcap / purchase_mcap
                return True, "UP", multiplier
        
        # DOWN Movement: Every 50% drop (2x decrease)
        # Example: $10k, alert at $5k (-50%), then $2.5k (-75%), then $1.25k (-87.5%)
        elif current_mcap < last_alerted_mcap:
            # Find next 50% drop threshold below current level
            next_threshold = last_alerted_mcap * 0.5
            if current_mcap <= next_threshold:
                # Calculate exact multiplier
                multiplier = current_mcap / purchase_mcap
                return True, "DOWN", multiplier
        
        return False, "", 1.0
    except Exception as e:
        print(f"[PORTFOLIO_ALERTS] Error in should_alert_on_mcap_milestone: {e}")
        return False, "", 1.0


def update_alert_milestone(uid: int, token_mint: str, current_mcap: float) -> bool:
    """Update the last alerted mcap level for a token."""
    try:
        configs = load_auto_sell()
        uid_str = str(uid)
        
        if uid_str not in configs or token_mint not in configs[uid_str]:
            return False
        
        cfg = configs[uid_str][token_mint]
        
        if "alert_milestones" not in cfg:
            cfg["alert_milestones"] = {}
        
        # Update the alerted level
        cfg["alert_milestones"]["last_alerted_mcap"] = current_mcap
        cfg["alert_milestones"]["last_alerted_at"] = datetime.now().isoformat()
        
        save_auto_sell(configs)
        return True
    except Exception as e:
        print(f"[PORTFOLIO_ALERTS] Error updating milestone: {e}")
        return False


def format_portfolio_alert(symbol: str, mint: str, purchase_mcap: float, current_mcap: float, direction: str, multiplier: float) -> str:
    """Format a nicely readable alert message."""
    try:
        if direction == "UP":
            pct_change = (multiplier - 1) * 100
            emoji = "🚀" if multiplier >= 10 else "📈"
            return (
                f"{emoji} *Portfolio Alert!*\n\n"
                f"Token: `${symbol}`\n"
                f"Mcap Milestone Reached: `{multiplier:.1f}x` (+{pct_change:.0f}%)\n\n"
                f"Entry Mcap: `${purchase_mcap:,.0f}`\n"
                f"Current Mcap: `${current_mcap:,.0f}`\n"
                f"\n[Solscan](https://solscan.io/token/{mint})"
            )
        else:  # DOWN
            pct_loss = (1 - multiplier) * 100
            emoji = "🔴"
            return (
                f"{emoji} *Portfolio Loss Alert!*\n\n"
                f"Token: `${symbol}`\n"
                f"Mcap Loss: `{pct_loss:.0f}%` (down to `{multiplier:.2f}x`)\n\n"
                f"Entry Mcap: `${purchase_mcap:,.0f}`\n"
                f"Current Mcap: `${current_mcap:,.0f}`\n"
                f"\n[Solscan](https://solscan.io/token/{mint})"
            )
    except Exception as e:
        print(f"[PORTFOLIO_ALERTS] Error formatting message: {e}")
        return f"📊 Mcap update for {symbol}: {current_mcap:,.0f}"


async def check_all_portfolio_alerts(bot, uid: int, get_mcap_func) -> list[dict]:
    """
    Check all tokens in portfolio for mcap milestones.
    Returns list of alerts sent.
    
    Args:
        bot: Telegram bot instance
        uid: User ID
        get_mcap_func: Function to get current mcap for a token (mint) -> float
    """
    alerts = []
    try:
        configs = load_auto_sell()
        uid_str = str(uid)
        
        if uid_str not in configs:
            return alerts
        
        user_tokens = configs[uid_str]
        
        for token_mint, cfg in user_tokens.items():
            try:
                if not cfg.get("enabled", True):
                    continue
                
                # Get current mcap
                try:
                    current_mcap = get_mcap_func(token_mint)
                except Exception:
                    current_mcap = 0
                
                if current_mcap <= 0:
                    continue
                
                # Check if should alert
                should_alert, direction, multiplier = should_alert_on_mcap_milestone(token_mint, current_mcap, cfg)
                
                if should_alert and direction:
                    # Format and send alert
                    symbol = cfg.get("symbol", token_mint[:6])
                    purchase_mcap = float(cfg.get("purchase_mcap", 0))
                    
                    message = format_portfolio_alert(symbol, token_mint, purchase_mcap, current_mcap, direction, multiplier)
                    
                    try:
                        await bot.send_message(
                            chat_id=uid,
                            text=message,
                            parse_mode="Markdown"
                        )
                        
                        # Update milestone
                        update_alert_milestone(uid, token_mint, current_mcap)
                        
                        alerts.append({
                            "symbol": symbol,
                            "direction": direction,
                            "multiplier": multiplier,
                            "current_mcap": current_mcap
                        })
                        print(f"[PORTFOLIO_ALERTS] Sent {direction} alert for {symbol} ({multiplier:.1f}x)")
                    except Exception as e:
                        print(f"[PORTFOLIO_ALERTS] Failed to send alert to {uid}: {e}")
            except Exception as e:
                print(f"[PORTFOLIO_ALERTS] Error checking token {token_mint}: {e}")
        
        return alerts
    except Exception as e:
        print(f"[PORTFOLIO_ALERTS] Critical error in check_all_portfolio_alerts: {e}")
        import traceback
        traceback.print_exc()
        return alerts
