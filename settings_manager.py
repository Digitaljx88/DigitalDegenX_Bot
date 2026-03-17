"""
Settings Manager: Runtime Configuration Management
──────────────────────────────────────────────────

Handles loading, saving, validating, and swapping user heat score settings.
Stores per-user configuration in global_settings.json under 'heat_score_v2' key.
"""

import json
import os
import logging
from typing import Optional, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "data", "global_settings.json")
HEAT_SCORE_SETTINGS_KEY = "heat_score_v2"

# ─── Embedded defaults (not dependent on config.py) ──────────────────────────

HEAT_SCORE_V2_DEFAULTS = {
    # ─── Momentum Factor (0-20 pts)
    "momentum_weight_usd_vol": 50,
    "momentum_weight_creation_momentum": 50,
    "momentum_min_vol": 5000.0,
    # ─── Liquidity Factor (0-20 pts)
    "liquidity_min_usd": 50000.0,
    "liquidity_good_usd": 10000.0,
    "liquidity_fair_usd": 2000.0,
    # ─── Risk Safety Factor (0-25 pts)
    "risk_dev_sell_threshold_pct": 50,
    "risk_top_holder_threshold_pct": 20,
    "risk_bundle_severity": 50,
    # ─── Social/Narrative Factor (0-15 pts)
    "social_twitter_follower_min": 1000,
    "social_narrative_trending_boost": 50,
    # ─── Wallet Behavior Factor (0-15 pts)
    "wallet_cluster_boost_pts": 5,
    "wallet_known_seed_boost_pts": 8,
    # ─── Migration Status Factor (0-10 pts)
    "migration_new_boost_pts": 8,
    "migration_grad_boost_pts": 6,
    "migration_migrated_penalty_pts": 2,
    # ─── Directional Bias Factor (0-10 pts)
    "bias_buy_threshold_pct": 70,
    "bias_buy_good_threshold_pct": 60,
    # ─── Volume Trend Factor (0-5 pts)
    "trend_explosive_threshold": 5,
    "trend_strong_threshold": 3,
    # ─── Scout Tier Thresholds
    "scout_tier_brewing_threshold": 35,
    "scout_tier_warm_threshold": 50,
    "scout_tier_hot_threshold": 70,
    # ─── Alert Notification Thresholds
    "alert_ultra_hot_threshold": 85,
    "alert_hot_threshold": 70,
    "alert_warm_threshold": 55,
    "alert_scouted_threshold": 35,
    # ─── Scanner MCap Filters (per-user)
    "scanner_mcap_min": 5_000,
    "scanner_mcap_max": 10_000_000,
}

SCOUT_PRESETS = {
    "conservative": {
        "name": "🛡️ Conservative",
        "description": "High threshold, low false positives, best for risk-averse traders",
        "overrides": {
            "alert_ultra_hot_threshold": 95,
            "alert_hot_threshold": 90,
            "alert_warm_threshold": 80,
            "alert_scouted_threshold": 70,
            "risk_dev_sell_threshold_pct": 30,
            "risk_top_holder_threshold_pct": 10,
            "liquidity_min_usd": 100000.0,
        }
    },
    "balanced": {
        "name": "⚖️ Balanced",
        "description": "Standard thresholds, good balance of signals and accuracy",
        "overrides": {
            "alert_ultra_hot_threshold": 85,
            "alert_hot_threshold": 70,
            "alert_warm_threshold": 55,
            "alert_scouted_threshold": 35,
            "risk_dev_sell_threshold_pct": 50,
            "risk_top_holder_threshold_pct": 20,
            "liquidity_min_usd": 50000.0,
        }
    },
    "aggressive": {
        "name": "🚀 Aggressive",
        "description": "Lower thresholds, more signals, best for active traders",
        "overrides": {
            "alert_ultra_hot_threshold": 85,
            "alert_hot_threshold": 70,
            "alert_warm_threshold": 55,
            "alert_scouted_threshold": 40,
            "risk_dev_sell_threshold_pct": 70,
            "risk_top_holder_threshold_pct": 30,
            "liquidity_min_usd": 10000.0,
        }
    },
    "whale-mode": {
        "name": "🐋 Whale Mode",
        "description": "Lowest thresholds, maximum signals, for whale hunters",
        "overrides": {
            "alert_ultra_hot_threshold": 80,
            "alert_hot_threshold": 65,
            "alert_warm_threshold": 50,
            "alert_scouted_threshold": 30,
            "risk_dev_sell_threshold_pct": 90,
            "risk_top_holder_threshold_pct": 50,
            "liquidity_min_usd": 2000.0,
        }
    },
}


def _get_defaults():
    """Get defaults — use config.py if available, otherwise embedded defaults."""
    try:
        import config
        return getattr(config, 'HEAT_SCORE_V2_DEFAULTS', HEAT_SCORE_V2_DEFAULTS)
    except Exception:
        return HEAT_SCORE_V2_DEFAULTS


def _get_presets():
    """Get presets — use config.py if available, otherwise embedded presets."""
    try:
        import config
        return getattr(config, 'SCOUT_PRESETS', SCOUT_PRESETS)
    except Exception:
        return SCOUT_PRESETS


def _ensure_settings_file() -> Dict[str, Any]:
    """Ensure settings file exists and return its contents."""
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w") as f:
            json.dump({}, f)
    
    try:
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error reading settings file: {e}")
        return {}


def _write_settings_file(data: Dict[str, Any]) -> None:
    """Write settings data to file."""
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except IOError as e:
        logger.error(f"Error writing settings file: {e}")


def get_user_settings(user_id: int) -> Dict[str, Any]:
    """
    Get heat score settings for a specific user.
    Falls back to defaults if user has no custom settings.
    
    Args:
        user_id: Telegram user ID
    
    Returns:
        Dict with all heat score v2 settings (merged defaults + user overrides)
    """
    settings_data = _ensure_settings_file()
    user_key = f"user_{user_id}"
    
    # Start with defaults from config
    merged_settings = _get_defaults().copy()
    
    # Check for old min_score setting and migrate if needed
    if user_key not in settings_data or HEAT_SCORE_SETTINGS_KEY not in settings_data.get(user_key, {}):
        # User has no v2 settings yet - check for old min_score migration
        try:
            old_min_score = migrate_min_score_to_v2(user_id)
            if old_min_score:
                merged_settings.update(old_min_score)
        except Exception:
            pass  # Silently fail if migration doesn't work
    
    # Merge user overrides if they exist
    if user_key in settings_data:
        user_settings = settings_data[user_key].get(HEAT_SCORE_SETTINGS_KEY, {})
        merged_settings.update(user_settings)
    
    return merged_settings


def migrate_min_score_to_v2(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Migrate old min_score setting from scanner_state.json to v2 thresholds.
    
    Maps old single threshold to new tier-based thresholds:
    - min_score < 60: aggressive (50/60/70/85)
    - min_score 60-70: balanced (55/65/75/90)
    - min_score 70-80: conservative (65/75/85/95)
    - min_score >= 80: ultra-conservative (75/85/95/100)
    
    Args:
        user_id: Telegram user ID
    
    Returns:
        Dict with migrated v2 settings, or None if no old setting found
    """
    try:
        import scanner as sc
        old_min_score = sc.get_user_min_score(user_id)
        
        # Map old min_score to v2 thresholds
        if old_min_score < 60:
            return {
                "alert_scouted_threshold": 50,
                "alert_warm_threshold": 60,
                "alert_hot_threshold": 70,
                "alert_ultra_hot_threshold": 85,
            }
        elif old_min_score <= 70:
            return {
                "alert_scouted_threshold": 55,
                "alert_warm_threshold": 65,
                "alert_hot_threshold": 75,
                "alert_ultra_hot_threshold": 90,
            }
        elif old_min_score <= 80:
            return {
                "alert_scouted_threshold": 65,
                "alert_warm_threshold": 75,
                "alert_hot_threshold": 85,
                "alert_ultra_hot_threshold": 95,
            }
        else:
            return {
                "alert_scouted_threshold": 75,
                "alert_warm_threshold": 85,
                "alert_hot_threshold": 95,
                "alert_ultra_hot_threshold": 100,
            }
    except Exception:
        return None


def save_user_settings(user_id: int, settings_dict: Dict[str, Any]) -> bool:
    """
    Save heat score settings for a user.
    
    Args:
        user_id: Telegram user ID
        settings_dict: Dict of setting_name -> value pairs to save
    
    Returns:
        True if successful, False otherwise
    """
    try:
        settings_data = _ensure_settings_file()
        user_key = f"user_{user_id}"
        
        if user_key not in settings_data:
            settings_data[user_key] = {}
        
        # Validate all settings before saving
        for key, value in settings_dict.items():
            if not validate_setting(key, value):
                logger.warning(f"Invalid setting {key}={value}, skipping")
                continue
            
            if HEAT_SCORE_SETTINGS_KEY not in settings_data[user_key]:
                settings_data[user_key][HEAT_SCORE_SETTINGS_KEY] = {}
            
            settings_data[user_key][HEAT_SCORE_SETTINGS_KEY][key] = value
        
        _write_settings_file(settings_data)
        logger.info(f"Saved settings for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving settings for user {user_id}: {e}")
        return False


def validate_setting(setting_key: str, value: Any) -> bool:
    """
    Validate that a setting value is within acceptable ranges.
    
    Args:
        setting_key: Name of the setting
        value: Value to validate
    
    Returns:
        True if valid, False otherwise
    """
    # Integer range settings (all 0-100 unless specified)
    int_range_settings = {
        "momentum_weight_usd_vol": (1, 100),
        "momentum_weight_creation_momentum": (1, 100),
        "liquidity_min_usd": (0, 999999),  # Any non-negative USD
        "liquidity_good_usd": (0, 999999),
        "liquidity_fair_usd": (0, 999999),
        "risk_dev_sell_threshold_pct": (0, 100),
        "risk_top_holder_threshold_pct": (0, 100),
        "risk_bundle_severity": (0, 100),
        "social_twitter_follower_min": (0, 999999),
        "social_narrative_trending_boost": (0, 100),
        "wallet_cluster_boost_pts": (0, 15),
        "wallet_known_seed_boost_pts": (0, 15),
        "migration_new_boost_pts": (0, 10),
        "migration_grad_boost_pts": (0, 10),
        "migration_migrated_penalty_pts": (0, 10),
        "bias_buy_threshold_pct": (0, 100),
        "bias_buy_good_threshold_pct": (0, 100),
        "trend_explosive_threshold": (0, 5),
        "trend_strong_threshold": (0, 5),
        "scout_tier_brewing_threshold": (0, 100),
        "scout_tier_warm_threshold": (0, 100),
        "scout_tier_hot_threshold": (0, 100),
        "alert_ultra_hot_threshold": (0, 100),
        "alert_hot_threshold": (0, 100),
        "alert_warm_threshold": (0, 100),
        "alert_scouted_threshold": (0, 100),
        # Scanner MCap range (USD) — per-user filters applied on top of global MCAP_MIN/MAX
        "scanner_mcap_min": (0, 999_999_999),
        "scanner_mcap_max": (0, 999_999_999),
    }
    
    if setting_key not in int_range_settings:
        logger.warning(f"Unknown setting: {setting_key}")
        return False
    
    try:
        min_val, max_val = int_range_settings[setting_key]
        
        # Try to convert to appropriate type
        if isinstance(value, (int, float)):
            num_val = float(value) if "usd" in setting_key.lower() else int(value)
        else:
            num_val = float(value)
        
        if min_val <= num_val <= max_val:
            return True
        else:
            logger.warning(f"Setting {setting_key}={value} out of range [{min_val}, {max_val}]")
            return False
    except (ValueError, TypeError) as e:
        logger.warning(f"Invalid type for setting {setting_key}: {e}")
        return False


def reset_user_settings(user_id: int) -> bool:
    """
    Reset a user's settings to defaults (delete their custom overrides).
    
    Args:
        user_id: Telegram user ID
    
    Returns:
        True if successful, False otherwise
    """
    try:
        settings_data = _ensure_settings_file()
        user_key = f"user_{user_id}"
        
        if user_key in settings_data:
            if HEAT_SCORE_SETTINGS_KEY in settings_data[user_key]:
                del settings_data[user_key][HEAT_SCORE_SETTINGS_KEY]
            if not settings_data[user_key]:  # Remove user key if empty
                del settings_data[user_key]
        
        _write_settings_file(settings_data)
        logger.info(f"Reset settings to defaults for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error resetting settings for user {user_id}: {e}")
        return False


def apply_preset(user_id: int, preset_name: str) -> bool:
    """
    Apply a preset configuration (Conservative/Balanced/Aggressive/Whale-Mode).
    
    Args:
        user_id: Telegram user ID
        preset_name: Name of preset ("conservative", "balanced", "aggressive", "whale-mode")
    
    Returns:
        True if successful, False if preset not found
    """
    presets = _get_presets()
    if preset_name not in presets:
        logger.warning(f"Preset not found: {preset_name}")
        return False
    
    preset = presets[preset_name]
    overrides = preset.get("overrides", {})
    
    return save_user_settings(user_id, overrides)


def get_preset_info(preset_name: str) -> Optional[Dict[str, Any]]:
    """
    Get metadata about a preset.
    
    Args:
        preset_name: Name of preset
    
    Returns:
        Dict with 'name', 'description', 'overrides' or None if not found
    """
    presets = _get_presets()
    if preset_name not in presets:
        return None
    
    return presets[preset_name]


def list_presets() -> Dict[str, Dict[str, Any]]:
    """
    Get all available presets.
    
    Returns:
        Dict mapping preset_name -> {name, description, overrides}
    """
    return {
        name: {
            "display_name": preset.get("name", name),
            "description": preset.get("description", ""),
            "setting_count": len(preset.get("overrides", {}))
        }
        for name, preset in _get_presets().items()
    }


def detect_current_preset(user_id: int) -> str:
    """Detect which preset (if any) matches the user's current settings."""
    user_cfg = get_user_settings(user_id)
    for preset_name, preset_cfg in _get_presets().items():
        overrides = preset_cfg.get("overrides", {})
        if overrides and all(user_cfg.get(k) == v for k, v in overrides.items()):
            return preset_cfg.get("name", preset_name)
    return "Custom"


def get_setting_description(setting_key: str) -> str:
    """
    Get human-readable description of a setting.
    
    Args:
        setting_key: Name of the setting
    
    Returns:
        Description string
    """
    descriptions = {
        # Momentum
        "momentum_weight_usd_vol": "📊 Volume Weight: Importance of trading volume vs token age (1-100)",
        "momentum_weight_creation_momentum": "⏰ Newness Weight: Importance of token freshness (1-100)",
        "momentum_min_vol": "💰 Minimum Volume: USD volume needed for full momentum points",
        
        # Liquidity
        "liquidity_min_usd": "🏦 Max Liquidity: USD threshold for full liquidity score",
        "liquidity_good_usd": "🏦 Good Liquidity: USD threshold for solid liquidity score",
        "liquidity_fair_usd": "🏦 Fair Liquidity: USD threshold for baseline liquidity score",
        
        # Risk
        "risk_dev_sell_threshold_pct": "⚠️ Dev Dump Threshold: Dev >X% sold = auto-disqualify (0-100)",
        "risk_top_holder_threshold_pct": "⚠️ Holder Concentration: Single holder >X% = auto-disqualify (0-100)",
        "risk_bundle_severity": "📦 Bundle Risk: How much to penalize bundled tokens (0-100)",
        
        # Social
        "social_twitter_follower_min": "🐦 Twitter Min Followers: Followers needed for social points",
        "social_narrative_trending_boost": "🔥 Narrative Boost: Importance of trending narratives (0-100)",
        
        # Wallets
        "wallet_cluster_boost_pts": "👥 Cluster Bonus: Points for cluster wallet match (0-15)",
        "wallet_known_seed_boost_pts": "🌱 Seed Bonus: Points for known seed wallet entry (0-15)",
        
        # Migration
        "migration_new_boost_pts": "🆕 New Token Boost: Points for tokens <1 hour old (0-10)",
        "migration_grad_boost_pts": "🎓 Graduation Bonus: Points for pump.fun graduates (0-10)",
        "migration_migrated_penalty_pts": "📈 Migration Penalty: Points deducted for already-migrated (0-10)",
        
        # Bias
        "bias_buy_threshold_pct": "🟢 Buy Bias Max: Buy % needed for full buy pressure score (0-100)",
        "bias_buy_good_threshold_pct": "🟢 Buy Bias Good: Buy % for good buy pressure score (0-100)",
        
        # Trend
        "trend_explosive_threshold": "📈 Explosive Trend: Max points for explosive volume trend (0-5)",
        "trend_strong_threshold": "📈 Strong Trend: Points for strong volume growth (0-5)",
        
        # Thresholds
        "scout_tier_brewing_threshold": "🫖 Brewing Tier: Score threshold for BREWING scout tier (0-100)",
        "scout_tier_warm_threshold": "☕ Warm Tier: Score threshold for WARM scout tier (0-100)",
        "scout_tier_hot_threshold": "🔥 Hot Tier: Score threshold for HOT scout tier (0-100)",
        
        # Alerts
        "alert_ultra_hot_threshold": "🔴 Ultra Hot Alert: Score threshold for ULTRA_HOT alerts (0-100)",
        "alert_hot_threshold": "🟠 Hot Alert: Score threshold for HOT alerts (0-100)",
        "alert_warm_threshold": "🟡 Warm Alert: Score threshold for WARM alerts (0-100)",
        "alert_scouted_threshold": "⚪ Scouted Watchlist: Score threshold for watchlist adds (0-100)",
        # Scanner MCap
        "scanner_mcap_min": "📊 MCap Min Filter: Minimum token market cap (USD) shown in your alerts",
        "scanner_mcap_max": "📊 MCap Max Filter: Maximum token market cap (USD) shown in your alerts",
    }
    
    return descriptions.get(setting_key, f"Setting: {setting_key}")


def format_settings_display(user_id: int, compact: bool = False) -> str:
    """
    Format user settings for display in Telegram.
    
    Args:
        user_id: Telegram user ID
        compact: If True, show only non-default settings; if False, show all
    
    Returns:
        Formatted string for display
    """
    current = get_user_settings(user_id)
    defaults = _get_defaults()
    
    lines = ["*⚙️ Current Heat Score Settings*\n"]
    
    # Group settings by category
    categories = {
        "Momentum": [k for k in current.keys() if k.startswith("momentum_")],
        "Liquidity": [k for k in current.keys() if k.startswith("liquidity_")],
        "Risk": [k for k in current.keys() if k.startswith("risk_")],
        "Social": [k for k in current.keys() if k.startswith("social_")],
        "Wallets": [k for k in current.keys() if k.startswith("wallet_")],
        "Migration": [k for k in current.keys() if k.startswith("migration_")],
        "Bias": [k for k in current.keys() if k.startswith("bias_")],
        "Trend": [k for k in current.keys() if k.startswith("trend_")],
        "Thresholds": [k for k in current.keys() if k.startswith("scout_tier_") or k.startswith("alert_")],
    }
    
    for category, keys in categories.items():
        if not keys:
            continue
        
        lines.append(f"\n*{category}*")
        for key in sorted(keys):
            current_val = current[key]
            default_val = defaults.get(key)
            
            # Skip if compact and not different from default
            if compact and current_val == default_val:
                continue
            
            # Highlight if different from default
            marker = "✏️" if current_val != default_val else "  "
            
            # Format value nicely
            if isinstance(current_val, float) and current_val > 1000:
                val_str = f"${current_val:,.0f}"
            else:
                val_str = str(current_val)
            
            lines.append(f"{marker} `{key}`: {val_str}")
    
    return "\n".join(lines)
