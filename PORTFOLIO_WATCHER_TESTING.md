# Portfolio Distribution Watcher - Testing & Validation Guide

## Pre-Deployment Testing (Local)

### 1. Syntax & Import Validation
```bash
cd /Users/rosalindjames/DigitalDegenX_Bot

# Test all Python files compile
python3 -m py_compile portfolio_watcher.py config.py pumpfeed.py bot.py
echo "✅ All syntax valid"

# Test imports in isolation
python3 << 'EOF'
import sys
sys.path.insert(0, '/Users/rosalindjames/DigitalDegenX_Bot')

# Test portfolio_watcher imports
from portfolio_watcher import (
    fetch_dexscreener_metrics,
    fetch_pump_dev_status,
    score_signals,
    check_portfolio_for_alerts,
)
print("✅ portfolio_watcher imports OK")

# Test integration with pumpfeed
import pumpfeed as pf
print("✅ pumpfeed imports OK")

# Test config loading
import config
print(f"✅ config imports OK - MAIN_CHANNEL_ID type: {type(config.MAIN_CHANNEL_ID)}")

EOF
```

### 2. Configuration Validation

**Required Configuration in config.py:**
```python
# ✅ CRITICAL - Must be set before testing
MAIN_CHANNEL_ID = -1001234567890  # Your telegram channel ID (negative format)

# ✅ Optional but recommended
PORTFOLIO_WATCHER_ENABLED = True
PORTFOLIO_WATCHER_INTERVAL_SECS = 60
PORTFOLIO_WATCHER_WATCH_LIMIT = 20

WATCHER_SIGNAL_WEIGHTS = {
    "dev_movement": 3.0,
    "whale_exit": 2.5,
    "liquidity_drain": 2.0,
    "buy_sell_flip": 1.5,
    "volume_collapse": 1.0,
}

WATCHER_ALERT_THRESHOLD_HIGH = 3.0
WATCHER_ALERT_THRESHOLD_MEDIUM = 2.0
WATCHER_ALERT_THRESHOLD_LOW = 1.0
WATCHER_SIGNAL_COOLDOWN_SECS = 300
WATCHER_MIN_BASELINE_CYCLES = 3
```

**Verify config:**
```bash
python3 << 'EOF'
import config
print(f"✅ MAIN_CHANNEL_ID: {config.MAIN_CHANNEL_ID}")
print(f"✅ Watcher Enabled: {config.PORTFOLIO_WATCHER_ENABLED}")
print(f"✅ Watch Limit: {config.PORTFOLIO_WATCHER_WATCH_LIMIT}")
print(f"✅ Signal Weights: {config.WATCHER_SIGNAL_WEIGHTS}")
print(f"✅ Alert Thresholds: HIGH={config.WATCHER_ALERT_THRESHOLD_HIGH}, MEDIUM={config.WATCHER_ALERT_THRESHOLD_MEDIUM}, LOW={config.WATCHER_ALERT_THRESHOLD_LOW}")
EOF
```

---

## Unit Testing: Signal Detectors

### 3. Test DexScreener API Integration
```bash
python3 << 'EOF'
import asyncio
from portfolio_watcher import fetch_dexscreener_metrics

async def test_dexscreener():
    # Test with well-known token (BONK = SOL blockchain)
    mint = "DezXAZ8z7PnrnRJjz3wXBoQskzw1ia8dx5TbuD5t8Z1s"  # BONK
    
    try:
        metrics = await fetch_dexscreener_metrics(mint)
        print("✅ DexScreener API working")
        print(f"   Price: ${metrics.get('price', 'N/A')}")
        print(f"   Liquidity: ${metrics.get('liquidity', 'N/A'):,.0f}")
        print(f"   Volume 5m: ${metrics.get('volume_5m', 'N/A'):,.0f}")
        print(f"   Volume 1h: ${metrics.get('volume_1h', 'N/A'):,.0f}")
        print(f"   Buys 5m: {metrics.get('buys_5m', 'N/A')}")
        print(f"   Sells 5m: {metrics.get('sells_5m', 'N/A')}")
    except Exception as e:
        print(f"❌ DexScreener API failed: {e}")

asyncio.run(test_dexscreener())
EOF
```

### 4. Test Pump.fun API Integration
```bash
python3 << 'EOF'
import asyncio
from portfolio_watcher import fetch_pump_dev_status

async def test_pump_fun():
    # Test with real pump.fun token
    mint = "F7C6jNhJXyCXZr3qqYvJQWHeaKgP8ytchWz1yFxfBXmX"
    
    try:
        pump_data = await fetch_pump_dev_status(mint)
        print("✅ Pump.fun API working")
        print(f"   Dev Wallet: {pump_data.get('dev_wallet', 'N/A')[:10]}...")
        print(f"   Dev Active (hours): {pump_data.get('last_trade_hours_ago', 'N/A')}")
        print(f"   Authority Revoked: {pump_data.get('is_dev_authority_revoked', 'N/A')}")
    except Exception as e:
        print(f"❌ Pump.fun API failed: {e}")

asyncio.run(test_pump_fun())
EOF
```

### 5. Test Signal Detection Logic
```bash
python3 << 'EOF'
import asyncio
from portfolio_watcher import score_signals
import json

async def test_signal_scoring():
    # Test with realistic metrics
    test_cases = [
        {
            "name": "Multiple Strong Signals",
            "metrics": {
                "price": 0.0001,
                "liquidity": 50000,
                "volume_5m": 5000,
                "volume_1h": 50000,
                "buys_5m": 10,
                "sells_5m": 40,  # 4:1 sell ratio = whale exit
                "dev_activity_hours": 0.5,  # Very recent dev activity
            },
            "expected_risk": "HIGH"
        },
        {
            "name": "Moderate Signals",
            "metrics": {
                "price": 0.0001,
                "liquidity": 100000,
                "volume_5m": 20000,
                "volume_1h": 25000,  # 80% volume retained
                "buys_5m": 50,
                "sells_5m": 65,  # 1.3:1 sell ratio
                "dev_activity_hours": 2,
            },
            "expected_risk": "MEDIUM"
        },
        {
            "name": "No Signals - Healthy",
            "metrics": {
                "price": 0.0001,
                "liquidity": 500000,
                "volume_5m": 100000,
                "volume_1h": 100000,
                "buys_5m": 100,
                "sells_5m": 95,  # Balanced
                "dev_activity_hours": 4,  # No recent dev activity
            },
            "expected_risk": "LOW"
        }
    ]
    
    for test in test_cases:
        try:
            signals, score, risk_level = score_signals(
                mint="TEST_MINT",
                symbol=test["name"],
                metrics=test["metrics"]
            )
            
            print(f"\n✅ {test['name']}")
            print(f"   Score: {score:.2f}/5.0")
            print(f"   Risk Level: {risk_level}")
            print(f"   Signals: {json.dumps(signals, indent=6)}")
            
            if risk_level == test["expected_risk"]:
                print(f"   ✅ Risk level matches expected: {test['expected_risk']}")
            else:
                print(f"   ⚠️  Expected {test['expected_risk']}, got {risk_level}")
                
        except Exception as e:
            print(f"❌ Error testing {test['name']}: {e}")

asyncio.run(test_signal_scoring())
EOF
```

---

## Integration Testing: State Management

### 6. Test State File Creation & Persistence
```bash
python3 << 'EOF'
import os
import json
from portfolio_watcher import _load_state, _init_token_state, _save_state

# Test state file operations
try:
    # Load/create state
    state = _load_state()
    print("✅ State file loaded/created")
    
    # Initialize a test token
    test_mint = "TEST_TOKEN_ABC123"
    _init_token_state(
        mint=test_mint,
        symbol="TEST",
        dev_wallet="DevWalletAddress123"
    )
    print(f"✅ Token state initialized: {test_mint}")
    
    # Reload and verify
    state = _load_state()
    if test_mint in state:
        print(f"✅ Token persisted in state file")
        print(f"   Symbol: {state[test_mint].get('symbol')}")
        print(f"   Dev Wallet: {state[test_mint].get('dev_wallet')}")
        print(f"   Cycles Observed: {state[test_mint].get('cycles_observed')}")
    
except Exception as e:
    print(f"❌ State management failed: {e}")

EOF
```

### 7. Create Test Portfolio
```bash
python3 << 'EOF'
import json
import os

test_portfolio = {
    "USER_ID_123": {
        "tokens": {
            "DezXAZ8z7PnrnRJjz3wXBoQskzw1ia8dx5TbuD5t8Z1s": {  # BONK
                "symbol": "BONK",
                "amount": 1000000,
                "entry_price": 0.00001,
                "entry_time": 1709573000
            },
            "F7C6jNhJXyCXZr3qqYvJQWHeaKgP8ytchWz1yFxfBXmX": {
                "symbol": "TEST_TOKEN",
                "amount": 500000,
                "entry_price": 0.0001,
                "entry_time": 1709573000
            }
        },
        "paper_balance": 10.0
    }
}

# Save test portfolio
os.makedirs("data", exist_ok=True)
with open("data/portfolios.json", "w") as f:
    json.dump(test_portfolio, f, indent=2)
    
print("✅ Test portfolio created in data/portfolios.json")
print(json.dumps(test_portfolio, indent=2))

EOF
```

---

## End-to-End Testing: Alert Flow

### 8. Simulate Alert Trigger
```bash
python3 << 'EOF'
import asyncio
import json
from portfolio_watcher import check_portfolio_for_alerts

async def test_alert_flow():
    # Load test portfolio
    with open("data/portfolios.json") as f:
        portfolios = json.load(f)
    
    test_uid = list(portfolios.keys())[0]
    test_portfolio = portfolios[test_uid]
    
    print(f"Testing alert flow for user: {test_uid}")
    print(f"Portfolio tokens: {list(test_portfolio['tokens'].keys())}")
    
    try:
        # Run watcher check
        alerts = await check_portfolio_for_alerts(
            bot=None,  # Mock bot for testing
            uid=test_uid,
            portfolio=test_portfolio,
            get_portfolio_func=lambda x: test_portfolio
        )
        
        print(f"\n✅ Alert check completed")
        print(f"   Alerts generated: {len(alerts)}")
        
        for alert in alerts:
            mint, symbol, signals, score, risk_level, message = alert
            print(f"\n   Token: {symbol} ({mint[:8]}...)")
            print(f"   Score: {score:.2f}/5.0")
            print(f"   Risk: {risk_level}")
            print(f"   Signals: {list(signals.keys())}")
            
    except Exception as e:
        print(f"⚠️  Alert check encountered: {e}")
        print("   (This is expected if APIs are rate-limited or offline)")

asyncio.run(test_alert_flow())
EOF
```

---

## Bot Handler Testing

### 9. Test Telegram Command Handlers
```bash
# Run bot in test mode with logging
python3 << 'EOF'
import logging
logging.basicConfig(level=logging.DEBUG)

# Test imports
try:
    from bot import cmd_portfolio_watch, watch_callback
    print("✅ Bot handlers imported successfully")
    print(f"   cmd_portfolio_watch: {cmd_portfolio_watch}")
    print(f"   watch_callback: {watch_callback}")
except Exception as e:
    print(f"❌ Failed to import handlers: {e}")

EOF
```

---

## Manual Testing Checklist

### 10. Live Bot Testing
```
[ ] 1. Start bot locally
    python3 bot.py
    
[ ] 2. Open Telegram and send: /watch
    Expected: Bot shows portfolio overview with baseline/active status
    
[ ] 3. Click "⚙️ Settings" button
    Expected: Settings screen shows 5 signal types being monitored
    
[ ] 4. Click "Toggle" button
    Expected: Watcher disables/enables for your user (stored in global_settings.json)
    
[ ] 5. Buy a test token (paper mode)
    /buy <token_address>
    
[ ] 6. Monitor logs for [WATCH] messages
    Expected output:
    [WATCH] Baseline (1/3) - Token
    [WATCH] Baseline (2/3) - Token
    [WATCH] Baseline (3/3) - Token
    [WATCH] Active monitoring - Token (ready to alert)
    
[ ] 7. Wait 3-5 minutes for baseline to complete
    
[ ] 8. Manually trigger a signal (for testing)
    Edit data/portfolio_watcher_state.json:
    - Set token's "last_liquidity" to -20% of current
    - Set "cycles_observed" to 4+ (past baseline)
    - Wait 60 seconds for next polling cycle
    
[ ] 9. Check MAIN_CHANNEL_ID for alert
    Expected format:
    🔴 DISTRIBUTION WARNING
    Token: $SYMBOL
    Risk Level: HIGH
    Confidence: 2.5/5.0
    
    Signals Detected:
    • Liquidity draining -18%
    
    Action: Monitor closely or exit position
    
[ ] 10. Verify alert has cooldown (5 min)
    No duplicate alerts for same token/signal within 300s
```

---

## Debugging & Logging

### 11. Enable Debug Logging
```bash
# Edit bot.py and add at top:
import logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Run with debug output
python3 bot.py 2>&1 | grep -E "\[WATCH\]|ERROR|DEBUG"
```

### 12. Inspect State File
```bash
# View current watcher state
python3 << 'EOF'
import json

try:
    with open("data/portfolio_watcher_state.json") as f:
        state = json.load(f)
        print(json.dumps(state, indent=2))
except FileNotFoundError:
    print("State file not created yet (will be on first watch run)")
EOF
```

### 13. Check Alert History
```bash
# View recent bot messages that were sent
python3 << 'EOF'
import json
from datetime import datetime, timedelta

with open("data/portfolio_watcher_state.json") as f:
    state = json.load(f)

print("Recent Alerts:")
print("-" * 60)

for mint, token_state in state.items():
    last_alert = token_state.get("last_alert_time")
    if last_alert:
        alert_time = datetime.fromtimestamp(last_alert)
        age = (datetime.now() - alert_time).total_seconds() / 60
        
        if age < 60:  # Last 60 minutes
            print(f"\n{token_state.get('symbol')} ({mint[:8]}...)")
            print(f"  Alerted: {age:.1f} min ago")
            print(f"  Signals: {token_state.get('last_alert_signals')}")

EOF
```

---

## Performance Testing

### 14. API Rate Limiting Check
```bash
# Verify that watcher respects rate limits
python3 << 'EOF'
import time
import asyncio
from portfolio_watcher import check_portfolio_for_alerts
import json

async def test_rate_limiting():
    with open("data/portfolios.json") as f:
        portfolios = json.load(f)
    
    uid = list(portfolios.keys())[0]
    portfolio = portfolios[uid]
    
    # Simulate multiple rapid checks
    print("Testing rate limiting with 5 rapid checks...")
    
    for i in range(5):
        start = time.time()
        try:
            alerts = await check_portfolio_for_alerts(
                bot=None,
                uid=uid,
                portfolio=portfolio,
                get_portfolio_func=lambda x: portfolio
            )
            elapsed = time.time() - start
            print(f"  Check {i+1}: {elapsed:.2f}s, {len(alerts)} alerts")
        except Exception as e:
            print(f"  Check {i+1}: Error - {str(e)[:50]}")
        
        time.sleep(1)  # Small delay between checks

asyncio.run(test_rate_limiting())
EOF
```

### 15. Memory Usage Check
```bash
# Monitor bot memory during polling
python3 << 'EOF'
import psutil
import os

bot_pid = os.getpid()
process = psutil.Process(bot_pid)

print(f"Bot Process ID: {bot_pid}")
print(f"Memory: {process.memory_info().rss / 1024 / 1024:.2f} MB")
print(f"CPU %: {process.cpu_percent(interval=1):.2f}%")
print(f"Threads: {process.num_threads()}")

EOF
```

---

## Pre-Deployment Checklist

- [ ] All syntax valid: `python3 -m py_compile portfolio_watcher.py config.py pumpfeed.py bot.py`
- [ ] Config has `MAIN_CHANNEL_ID` set to real Telegram channel ID
- [ ] All imports work: `python3 -c "from portfolio_watcher import *"`
- [ ] DexScreener API responds
- [ ] Pump.fun API responds
- [ ] Signal detection logic produces correct scores
- [ ] State file creates and persists correctly
- [ ] Test portfolio created successfully
- [ ] Bot handlers import without errors
- [ ] `/watch` command returns portfolio list
- [ ] Manual trading + watcher integration works
- [ ] First 3 baseline cycles complete without alerts
- [ ] Manual signal trigger generates alert
- [ ] Alert sent to MAIN_CHANNEL_ID
- [ ] Cooldown prevents duplicate alerts
- [ ] Memory usage stable (<200MB)
- [ ] No CPU spikes during polling

---

## Deployment Steps (After Local Testing)

### 16. Copy to VPS
```bash
# From local machine:
rsync -avz -e "ssh -i ~/.ssh/id_rsa" \
  /Users/rosalindjames/DigitalDegenX_Bot/ \
  user@your_vps_ip:/opt/digitaldegen-bot/
```

### 17. Restart Bot on VPS
```bash
ssh user@your_vps_ip
sudo systemctl restart digitaldegen-bot
sudo journalctl -u digitaldegen-bot -f
```

### 18. Verify Deployment
```bash
# Check logs for portfolio watcher startup
sudo journalctl -u digitaldegen-bot | grep -i watch

# Expected output:
# [WATCH] Portfolio watcher started
# [WATCH] Polling every 60s...
```

---

## Post-Deployment Monitoring

### Monitor alert delivery
```bash
# Check how many alerts sent
sudo journalctl -u digitaldegen-bot | grep -c "DISTRIBUTION WARNING"

# Check for errors
sudo journalctl -u digitaldegen-bot | grep -i "error\|exception"

# View recent alerts
sudo journalctl -u digitaldegen-bot -n 100 | grep "\[WATCH\]"
```

---

## Rollback Plan

If issues occur:

```bash
# Stop bot
sudo systemctl stop digitaldegen-bot

# Restore previous version
cd /opt/digitaldegen-bot
git revert HEAD~1  # or git checkout <previous_commit>
git push

# Restart
sudo systemctl start digitaldegen-bot
```

