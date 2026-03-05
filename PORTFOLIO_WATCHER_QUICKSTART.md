# Portfolio Distribution Watcher - Feature Summary & Quick Start

## 🎯 Feature Overview

The Portfolio Distribution Watcher is a real-time sentinel system that monitors your held tokens for 5 crash indicators, using weighted confidence scoring to detect smart-money distribution patterns before rug pulls or major dumps occur.

**Deployed:** Commit `e27dfa9`  
**Status:** ✅ Ready for VPS deployment

---

## 📊 What It Monitors

### 5 Crash Signal Detectors

| Signal | Weight | Trigger | Risk Level |
|--------|--------|---------|-----------|
| **Dev Movement** | 3.0x | Dev wallet active in last 2 hours | 🔴 Highest |
| **Whale Exodus** | 2.5x | Sells exceed buys by 3:1 ratio | 🔴 Very High |
| **Liquidity Drain** | 2.0x | Liquidity drops >15% | 🟠 High |
| **Buy/Sell Flip** | 1.5x | Sells represent >60% of trades | 🟠 Medium |
| **Volume Collapse** | 1.0x | 5m volume <60% of 1h average | 🟡 Low |

### Alert Tiers

```
🔴 HIGH RISK (Score ≥3.0)     → Instant alert, consider exiting
🟠 MEDIUM RISK (Score ≥2.0)   → Alert with caution
🟡 LOW RISK (Score ≥1.0)      → Monitor position closely
```

---

## 🚀 Quick Start (Local Testing)

### 1. Install Dependencies
```bash
cd /Users/rosalindjames/DigitalDegenX_Bot
pip install -r requirements.txt
```

### 2. Configure (CRITICAL!)
Edit `config.py` and set your Telegram channel:
```python
MAIN_CHANNEL_ID = -1001234567890  # Change to YOUR channel ID
PORTFOLIO_WATCHER_ENABLED = True
PORTFOLIO_WATCHER_INTERVAL_SECS = 60
```

### 3. Start Bot
```bash
python3 bot.py
```

### 4. Test in Telegram
- Send `/watch` command
- You'll see tokens you're monitoring (top 20 by portfolio %)
- First 3 cycles (~180s) show "📍 Baseline" status
- From cycle 4+, alerts will fire for high-confidence signals

---

## 🔧 Configuration Options

| Setting | Default | Purpose |
|---------|---------|---------|
| `MAIN_CHANNEL_ID` | -1001234567890 | **REQUIRED:** Alert destination channel |
| `PORTFOLIO_WATCHER_ENABLED` | True | Master toggle |
| `PORTFOLIO_WATCHER_INTERVAL_SECS` | 60 | Polling frequency |
| `PORTFOLIO_WATCHER_WATCH_LIMIT` | 20 | Rate limit (monitor top 20 tokens) |
| `WATCHER_SIGNAL_COOLDOWN_SECS` | 300 | Prevent spam (5 min per signal per token) |
| `WATCHER_MIN_BASELINE_CYCLES` | 3 | Cycles before alerting (~180s) |
| `WATCHER_ALERT_THRESHOLD_HIGH` | 3.0 | Score threshold for 🔴 alerts |
| `WATCHER_ALERT_THRESHOLD_MEDIUM` | 2.0 | Score threshold for 🟠 alerts |
| `WATCHER_ALERT_THRESHOLD_LOW` | 1.0 | Score threshold for 🟡 alerts |

### Adjust Signal Weights
```python
WATCHER_SIGNAL_WEIGHTS = {
    "dev_movement": 3.0,      # How important dev activity is
    "whale_exit": 2.5,        # How important whale selling is
    "liquidity_drain": 2.0,   # How important liquidity loss is
    "buy_sell_flip": 1.5,     # How important sell ratio is
    "volume_collapse": 1.0,   # How important volume drop is
}
```

---

## 📱 Commands & Usage

### `/watch`
Shows tokens being monitored with their baseline/active status
- Display top 20 tokens by portfolio %
- Each shows baseline cycle progress
- Button: ⚙️ Settings

### `/watch` Settings
- View what signals are monitored
- Toggle watcher on/off per user
- Stored in `global_settings.json` as `watcher_enabled_{uid}`

---

## 📁 Files & Structure

```
portfolio_watcher.py          (NEW - 400 lines)
├── fetch_dexscreener_metrics()    - Get token metrics from DexScreener API
├── fetch_pump_dev_status()        - Get dev wallet data from Pump.fun API
├── detect_dev_movement()          - Signal detector (#1)
├── detect_whale_exit()            - Signal detector (#2)
├── detect_liquidity_drain()       - Signal detector (#3)
├── detect_buy_sell_flip()         - Signal detector (#4)
├── detect_volume_collapse()       - Signal detector (#5)
├── score_signals()                - Weighted scoring algorithm
├── check_portfolio_for_alerts()   - Main async entry point
└── (+ state management & formatting)

config.py                     (NEW - watcher config section)
├── MAIN_CHANNEL_ID
├── PORTFOLIO_WATCHER_*
├── WATCHER_SIGNAL_WEIGHTS
├── WATCHER_ALERT_THRESHOLD_*
└── WATCHER_MIN_BASELINE_CYCLES

pumpfeed.py                   (MODIFIED)
└── run_portfolio_watch()     - Async polling loop (runs every 60s)

bot.py                        (MODIFIED)
├── cmd_portfolio_watch()     - Handler for /watch command
├── watch_callback()          - Handler for settings buttons
└── post_init()               - Starts watcher on bot initialization

data/ (Persistent State)
└── portfolio_watcher_state.json
    └── Per-token tracking: cycles, baselines, cooldowns, last alerts
```

---

## 📊 Alert Message Format

When a high-confidence signal is detected:

```
🔴 DISTRIBUTION WARNING
Token: $SYMBOL (Mint: ABC123...)
Risk Level: HIGH
Confidence Score: 3.2/5.0

Signals Detected:
• Dev wallet moved funds (2h ago)
• Liquidity draining -18%

Current Price: $0.000105
Suggested Action: Monitor closely or exit position

⏰ Cooldown: Alert suppressed for 5 min on this signal
```

---

## 🔄 How It Works

### Lifecycle of a Token

1. **Token Added to Portfolio** (via `/buy`)
   - Watcher initializes state in `portfolio_watcher_state.json`
   - Begins baseline collection (3 cycles)

2. **Baseline Phase** (First ~180 seconds)
   - Watcher collects metrics: liquidity, volume, dev status
   - No alerts fire during this period
   - Establishes baseline for comparison

3. **Active Monitoring** (After baseline complete)
   - Every 60s: Fetch fresh metrics
   - Score all 5 signals with weights
   - If score ≥ threshold: Send alert (unless in cooldown)

4. **Cooldown** (After alert)
   - Same signal suppressed for 5 min on that token
   - Prevents alert spam from the same pattern
   - Other signals still trigger normally

5. **Token Sold** (via `/sell`)
   - Watcher keeps state for historical purposes
   - Stops polling that token

---

## 🔍 Example Score Calculations

### Example 1: Dev Just Dumped
```
Dev Movement:        TRUE (3.0x) = 3.0
Whale Exit:         FALSE (2.5x) = 0
Liquidity Drain:    FALSE (2.0x) = 0
Buy/Sell Flip:      FALSE (1.5x) = 0
Volume Collapse:    FALSE (1.0x) = 0
─────────────────────────────────
Total Score:        3.0/5.0
Risk Level:         🔴 HIGH (≥3.0)
Alert:              ✅ YES
```

### Example 2: Multiple Moderate Signals
```
Dev Movement:        TRUE (3.0x) = 3.0
Whale Exit:         FALSE (2.5x) = 0
Liquidity Drain:     TRUE (2.0x) = 2.0
Buy/Sell Flip:      FALSE (1.5x) = 0
Volume Collapse:     TRUE (1.0x) = 1.0
─────────────────────────────────
Total Score:        6.0/5.0 (capped at 5.0)
Risk Level:         🔴 HIGH (≥3.0)
Alert:              ✅ YES
```

### Example 3: Healthy Token
```
Dev Movement:       FALSE (3.0x) = 0
Whale Exit:         FALSE (2.5x) = 0
Liquidity Drain:    FALSE (2.0x) = 0
Buy/Sell Flip:      FALSE (1.5x) = 0
Volume Collapse:    FALSE (1.0x) = 0
─────────────────────────────────
Total Score:        0.0/5.0
Risk Level:         ⚪ NO ALERT
Alert:              ❌ NO
```

---

## 🚀 VPS Deployment

### Prerequisites
- Linux VPS (Ubuntu 20.04+)
- Python 3.10+
- SSH access

### Deployment Steps

1. **Copy code to VPS**
```bash
rsync -avz /Users/rosalindjames/DigitalDegenX_Bot/ user@vps_ip:/opt/digitaldegen-bot/
```

2. **Set up Python environment**
```bash
ssh user@vps_ip
cd /opt/digitaldegen-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. **Configure secrets**
```bash
nano config.py
# Set: TELEGRAM_TOKEN, SOLANA_RPC, MAIN_CHANNEL_ID (critical!)
chmod 600 config.py
```

4. **Start as systemd service**
```bash
sudo nano /etc/systemd/system/digitaldegen-bot.service
# Copy config from DEPLOYMENT.md Step 6.1
sudo systemctl daemon-reload
sudo systemctl enable digitaldegen-bot
sudo systemctl start digitaldegen-bot
```

5. **Verify deployment**
```bash
sudo systemctl status digitaldegen-bot
sudo journalctl -u digitaldegen-bot -f
# Look for: "[WATCH] Portfolio watcher started"
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for complete VPS setup guide.

---

## ⚠️ Important Notes

### MUST SET MAIN_CHANNEL_ID
Without this, alerts won't send. Get your Telegram channel ID:
1. Forward any message to @userinfobot
2. It returns your "Group ID" in negative format: `-1001234567890`

### API Rate Limiting
- Watcher monitors top 20 tokens by portfolio %
- Polling every 60 seconds = 20 API calls per minute
- Well within DexScreener/Pump.fun limits

### State File Location
```
data/portfolio_watcher_state.json
```
- Persists across bot restarts
- Contains per-token baselines and cooldowns
- Can be safely deleted to reset all watcher state

### Manual Alerts Disabled
- Watcher sends **warnings only**, no auto-sell
- You decide when to exit based on alerts
- Prevents accidental liquidation of positions

---

## 🔧 Troubleshooting

### Alerts not appearing?
1. ✅ Check `MAIN_CHANNEL_ID` is set in config.py
2. ✅ Verify you have a portfolio (`/portfolio`)
3. ✅ Wait 3+ cycles for baseline to complete (watch logs)
4. ✅ Check logs: `grep "\[WATCH\]" bot_logs.txt`

### Too many alerts?
1. Increase `WATCHER_SIGNAL_COOLDOWN_SECS` (default 300s)
2. Increase `WATCHER_ALERT_THRESHOLD_*` values
3. Disable specific signals: set weight to 0 in `WATCHER_SIGNAL_WEIGHTS`

### Wrong risk level?
Adjust `WATCHER_ALERT_THRESHOLD_*`:
```python
WATCHER_ALERT_THRESHOLD_HIGH = 3.0    # More strict = fewer alerts
WATCHER_ALERT_THRESHOLD_MEDIUM = 2.0
WATCHER_ALERT_THRESHOLD_LOW = 1.0
```

---

## 📈 Future Enhancements

Potential additions (not yet implemented):
- [ ] Auto-sell trigger on HIGH risk score
- [ ] Multi-wallet exit strategy (don't sell all at once)
- [ ] Discord/Email notifications in addition to Telegram
- [ ] ML-based score refinement (PCA, clustering)
- [ ] Historical pattern database (track crashes)
- [ ] Per-narrative thresholds (higher for meme coins)

---

## 📚 Files Modified

| File | Changes | Lines |
|------|---------|-------|
| portfolio_watcher.py | NEW | 400 |
| pumpfeed.py | Added `run_portfolio_watch()` | +65 |
| bot.py | Added handlers + integration | +180 |
| config.py | Added watcher settings | +30 |
| requirements.txt | NEW | 5 |

**Total new code:** ~680 lines  
**Commit:** `e27dfa9`

---

## 🎓 How to Use Effectively

### Best Practices

1. **Set reasonable thresholds** — Don't set too aggressive
   ```python
   WATCHER_ALERT_THRESHOLD_HIGH = 3.0  # Good starting point
   ```

2. **Monitor early alerts closely** — First week shows patterns
   ```bash
   grep "HIGH\|MEDIUM" bot_logs.txt | head -20
   ```

3. **Adjust weights for your strategy**
   - Scalpers: increase `volume_collapse` weight
   - Long-term holders: increase `dev_movement` weight

4. **Use alerts as one signal, not gospel**
   - Check chart patterns yourself
   - Verify with manual checking
   - Don't auto-sell (watcher is warning only)

---

## 📞 Support

For issues or questions:
1. Check `PORTFOLIO_WATCHER_TESTING.md` for detailed test procedures
2. Review logs: `sudo journalctl -u digitaldegen-bot -f`
3. Check `DEPLOYMENT.md` for VPS setup issues
4. Verify imports: `python3 test_imports.py`

---

## ✅ Completion Status

| Component | Status |
|-----------|--------|
| Signal Detection | ✅ 5 detectors implemented |
| Weighted Scoring | ✅ 5-level confidence system |
| State Management | ✅ Persistent tracking |
| Bot Integration | ✅ `/watch` command + handlers |
| Configuration | ✅ 14 new config variables |
| Documentation | ✅ PORTFOLIO_WATCHER_TESTING.md |
| Testing Guide | ✅ Multi-step validation checklist |
| Deployment Ready | ✅ VPS-compatible |

**Ready for production deployment!** 🚀
