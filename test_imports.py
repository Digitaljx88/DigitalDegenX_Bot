#!/usr/bin/env python3
"""Quick validation of portfolio watcher imports and config"""

import sys
sys.path.insert(0, '/Users/rosalindjames/DigitalDegenX_Bot')

print("🔍 Testing imports and configuration...\n")

# Test portfolio_watcher imports
try:
    from portfolio_watcher import (
        fetch_dexscreener_metrics,
        fetch_pump_dev_status,
        score_signals,
        check_portfolio_for_alerts,
    )
    print("✅ portfolio_watcher imports OK")
except Exception as e:
    print(f"❌ portfolio_watcher import failed: {e}")
    sys.exit(1)

# Test pumpfeed integration
try:
    import pumpfeed as pf
    print("✅ pumpfeed imports OK")
except Exception as e:
    print(f"❌ pumpfeed import failed: {e}")
    sys.exit(1)

# Test config loading
try:
    import config
    print(f"✅ config imports OK")
    print(f"   - PORTFOLIO_WATCHER_ENABLED: {config.PORTFOLIO_WATCHER_ENABLED}")
    print(f"   - PORTFOLIO_WATCHER_INTERVAL_SECS: {config.PORTFOLIO_WATCHER_INTERVAL_SECS}")
    print(f"   - PORTFOLIO_WATCHER_WATCH_LIMIT: {config.PORTFOLIO_WATCHER_WATCH_LIMIT}")
    
    if config.MAIN_CHANNEL_ID == -1001234567890:
        print(f"⚠️  MAIN_CHANNEL_ID is still demo value")
        print(f"   👉 Remember to set to your real Telegram channel ID before deploying")
    else:
        print(f"   - MAIN_CHANNEL_ID: {config.MAIN_CHANNEL_ID}")
        
except Exception as e:
    print(f"❌ config import failed: {e}")
    sys.exit(1)

# Test bot handlers
try:
    from bot import cmd_portfolio_watch, watch_callback
    print(f"✅ bot handlers imported successfully")
except Exception as e:
    print(f"❌ bot handlers import failed: {e}")
    sys.exit(1)

print("\n" + "="*60)
print("✅ ALL VALIDATION CHECKS PASSED")
print("="*60)
print("\nNext steps:")
print("1. Set MAIN_CHANNEL_ID in config.py to your Telegram channel")
print("2. Follow PORTFOLIO_WATCHER_TESTING.md for the full validation steps")
print("3. Start bot locally: python3 bot.py")
print("4. Test /watch command in Telegram")
