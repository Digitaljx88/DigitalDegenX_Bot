# DigitalDegenX Bot 🤖

Advanced Solana meme coin trading bot with intelligent portfolio monitoring, auto-sell management, and distribution crash detection.

**Status:** Production Ready | **Latest Commit:** `ad1d47f`

---

## 🎯 What This Bot Does

### Core Features

1. **Portfolio Management**
   - Track multiple token positions
   - Paper trading with simulated SOL balance
   - Automatic PNL calculations
   - Position history and trade logs

2. **Auto-Sell Engine** (New!)
   - Set custom take-profit targets (e.g., 2x = 50%, 4x = 100%)
   - Pre-configured presets: Conservative, Balanced, Aggressive
   - Per-token customization
   - Live price tracking with instant execution

3. **📊 Portfolio Distribution Watcher** (NEW! 🚀)
   - Real-time crash detection using 5 signals
   - Weighted confidence scoring system
   - Smart whale/dev movement tracking
   - Automated alerts to Telegram
   - **See:** [PORTFOLIO_WATCHER_QUICKSTART.md](PORTFOLIO_WATCHER_QUICKSTART.md)

4. **Token Analysis**
   - Pump.fun metrics (dev wallet, authority status)
   - DexScreener data (liquidity, volume, price)
   - Quick technical overview with charts

5. **Trading Commands**
   - `/buy` — Enter position (paper or live)
   - `/sell` — Exit position with realized PNL
   - `/portfolio` — View current holdings
   - `/watch` — Monitor tokens for crash signals

---

## 🚨 Portfolio Distribution Watcher (NEW)

Detects 5 crash indicators before rug pulls happen:

| Signal | Weight | What It Detects |
|--------|--------|-----------------|
| 🔴 Dev Movement | 3.0x | Dev wallet moving funds (likely dumping) |
| 🔴 Whale Exodus | 2.5x | Smart money selling (3:1 sell/buy ratio) |
| 🟠 Liquidity Drain | 2.0x | LP being pulled (>15% drop) |
| 🟠 Buy/Sell Flip | 1.5x | Momentum shift (>60% sells) |
| 🟡 Volume Collapse | 1.0x | Trading activity dying (40%+ drop) |

**How it works:**
- Scores signals on weighted scale (0-5.0)
- Sends alerts when score ≥ 3.0 (HIGH) or ≥ 2.0 (MEDIUM)
- Per-signal cooldown prevents alert spam (5 min)
- Baseline establishment for first 3 cycles (~180s)

**Quick Start:**
```bash
1. Set MAIN_CHANNEL_ID in config.py (your Telegram channel)
2. Start bot: python3 bot.py
3. Buy token: /buy <mint_address>
4. Send command: /watch
5. Monitor Telegram for crash alerts 🚨
```

For detailed setup: [PORTFOLIO_WATCHER_QUICKSTART.md](PORTFOLIO_WATCHER_QUICKSTART.md)

---

## 🚀 Getting Started

### Prerequisites
- Python 3.10+
- Telegram Bot Token (get from @BotFather)
- Solana RPC endpoint (Helius recommended: free tier available)

### Local Setup

1. **Clone/Download**
```bash
git clone https://github.com/Digitaljx88/DigitalDegenX_Bot.git
cd DigitalDegenX_Bot
```

2. **Create Config**
```bash
cp config.example.py config.py
# Edit config.py with your tokens and settings
nano config.py
```

**Critical settings:**
```python
TELEGRAM_TOKEN = "YOUR_BOT_TOKEN"
SOLANA_RPC = "https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
MAIN_CHANNEL_ID = -1001234567890  # For portfolio watcher alerts
PAPER_MODE = True  # Start with paper trading!
```

3. **Install Dependencies**
```bash
pip install -r requirements.txt
```

4. **Run Bot**
```bash
python3 bot.py
```

5. **Test in Telegram**
```
/start  → Bot responds with main menu
/buy DEF1... → Buy 1M of a token
/portfolio → View your holdings
/watch → Monitor for crash signals
```

---

## 📋 Main Commands

| Command | Purpose |
|---------|---------|
| `/start` | Show main menu |
| `/buy` | Enter a position |
| `/sell` | Exit position with PNL |
| `/portfolio` | View holdings |
| `/watch` | Monitor tokens for crashes |
| `/analytics` | View trading stats |
| `/autosell` | Configure auto-sell targets |
| `/settings` | Adjust bot preferences |

---

## ⚙️ Configuration

### config.py Settings

**Telegram:**
```python
TELEGRAM_TOKEN = "YOUR_BOT_TOKEN"          # From @BotFather
AUTHORIZED_USERS = [123456789]             # Your Telegram ID
```

**Solana:**
```python
SOLANA_NETWORK = "mainnet-beta"            # or devnet for testing
SOLANA_RPC = "https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
PAPER_MODE = True                          # ✅ Start here!
```

**Portfolio Watcher (NEW!):**
```python
MAIN_CHANNEL_ID = -1001234567890           # ⚠️ REQUIRED for alerts
PORTFOLIO_WATCHER_ENABLED = True
PORTFOLIO_WATCHER_INTERVAL_SECS = 60       # Check every minute
PORTFOLIO_WATCHER_WATCH_LIMIT = 20         # Monitor top 20 tokens
```

**Auto-Sell:**
```python
AUTOSELL_ENABLED = True
AUTOSELL_CHECK_INTERVAL = 30                # Check every 30s
```

See [config.example.py](config.example.py) for all options.

---

## 📊 Features Deep-Dive

### Auto-Sell Management
Set multi-tier take-profit targets:
```
2x  → Sell 50%
4x  → Sell 30%
6x+ → Sell 20%
```

Switch between presets:
- 🛡️ **Conservative** — Exit early, lock profits
- ⚖️ **Balanced** — Balanced growth/safety
- 🚀 **Aggressive** — Maximize upside, high risk

### Portfolio Watcher Alerts
When a token shows distribution signs:

```
🔴 DISTRIBUTION WARNING
Token: $SYMBOL
Risk Level: HIGH
Confidence: 3.2/5.0

Signals Detected:
• Liquidity draining -18%
• Dev wallet moved funds

Action: Monitor closely or exit position
```

### Analytics Dashboard
- Win rate calculation
- Total PNL tracking
- Most profitable tokens
- Trade history with entry/exit prices

---

## 🔧 Installation for VPS

For production deployment on a VPS:

```bash
# 1. SSH into VPS
ssh user@your_vps_ip

# 2. Clone code
cd /opt/digitaldegen-bot
git clone https://github.com/Digitaljx88/DigitalDegenX_Bot.git .

# 3. Set up Python
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. Configure (edit with your keys)
nano config.py

# 5. Start as service
sudo systemctl enable digitaldegen-bot
sudo systemctl start digitaldegen-bot

# 6. Monitor
sudo journalctl -u digitaldegen-bot -f
```

**Full VPS guide:** [DEPLOYMENT.md](DEPLOYMENT.md)

---

## 📁 Project Structure

```
DigitalDegenX_Bot/
├── bot.py                              # Main Telegram bot handler
├── pumpfeed.py                         # API polling & updates
├── portfolio_watcher.py                # ⭐ Crash detection engine
├── auto_sell_v2.py                     # Auto-sell execution
├── scanner.py                          # Token scanner
├── config.py                           # Configuration (user-specific)
├── config.example.py                   # Config template
├── requirements.txt                    # Python dependencies
│
├── DEPLOYMENT.md                       # VPS setup guide
├── PORTFOLIO_WATCHER_QUICKSTART.md     # Watcher feature guide
├── PORTFOLIO_WATCHER_TESTING.md        # Testing procedures
├── ALERTS.md                           # Alert configuration
├── TOOLS.md                            # Available tools & APIs
│
└── data/                               # Persistent storage
    ├── portfolios.json                 # Your positions
    ├── auto_sell.json                  # Auto-sell targets
    ├── portfolio_watcher_state.json    # ⭐ Watcher state
    ├── trade_log.json                  # Trade history
    └── global_settings.json            # User preferences
```

---

## 🎓 Usage Examples

### Example 1: Buy and Auto-Sell

```
/buy DEF1...
→ Bot: "Bought 1M DEF @ $0.0001"
→ Sets automatic exit at 2x, 4x, etc.
→ Bot sells at preset targets automatically
→ Shows PNL in Telegram when target hits
```

### Example 2: Monitor for Crashes

```
/buy PUMP_TOKEN
→ Bot adds to portfolio
→ /watch shows it in baseline phase (📍 Baseline 1/3)
→ After 3 cycles, watcher monitors all 5 signals
→ Alert: 🔴 Dev wallet dumped → Check chart, decide to exit
→ /sell PUMP_TOKEN to close position
```

### Example 3: Run on VPS

```
# Deploy once, runs forever
sudo systemctl start digitaldegen-bot

# Check it's working
sudo journalctl -u digitaldegen-bot -f

# Logs show:
# [WATCH] Portfolio watcher started
# [WATCH] Baseline (1/3) - Token: $ABC
# [TRADE] Sold 500k ABC @ $0.00015 for +$25.00 PNL
```

---

## 🔐 Security Best Practices

1. **Use Paper Mode First**
   ```python
   PAPER_MODE = True  # Test everything before live trading!
   ```

2. **Secure Your config.py**
   ```bash
   chmod 600 config.py  # Only you can read it
   ```

3. **Use VPS for 24/7 Trading**
   - Don't run on personal laptop (leaves it on)
   - Use small/cheap VPS ($3-5/month)
   - Enable automatic backups

4. **Limit Bot Permissions**
   - Don't give wallet with large SOL balance to bot
   - Use separate wallet for trading bot
   - Move profits to cold storage regularly

---

## 📊 Recent Updates

### Latest: Portfolio Distribution Watcher (Commit `e27dfa9`)
- ✅ 5 crash signal detectors with weighted scoring
- ✅ DexScreener + Pump.fun API integration
- ✅ Per-signal-per-token cooldowns (prevent spam)
- ✅ Baseline establishment (first 3 cycles, no false positives)
- ✅ Telegram alerts to MAIN_CHANNEL_ID
- ✅ `/watch` command for monitoring
- ✅ Settings UI for enable/disable per user

### Previous: Auto-Sell Preset Propagation (Commit `b0b7afe`)
- ✅ Fixed preset changes not applying to open positions
- ✅ Now updates all open token targets immediately

---

## 🐛 Troubleshooting

### Bot not responding?
1. Check Telegram token in config.py
2. Verify bot is running: `ps aux | grep bot.py`
3. Check logs: `python3 bot.py` (run in foreground to see errors)

### Alerts not appearing?
1. Set `MAIN_CHANNEL_ID` in config.py (critical!)
2. Verify Telegram channel exists
3. Wait 3 cycles for baseline to complete (watch logs)

### Import errors?
1. Install requirements: `pip install -r requirements.txt`
2. Check Python version: `python3 --version` (needs 3.10+)
3. Run validation: `python3 test_imports.py`

### VPS deployment issues?
See [DEPLOYMENT.md](DEPLOYMENT.md) troubleshooting section

---

## 📞 Support & Documentation

- **Getting Started:** This README
- **Portfolio Watcher Feature:** [PORTFOLIO_WATCHER_QUICKSTART.md](PORTFOLIO_WATCHER_QUICKSTART.md)
- **Testing Guide:** [PORTFOLIO_WATCHER_TESTING.md](PORTFOLIO_WATCHER_TESTING.md)
- **VPS Deployment:** [DEPLOYMENT.md](DEPLOYMENT.md)
- **Alert Configuration:** [ALERTS.md](ALERTS.md)
- **API Reference:** [TOOLS.md](TOOLS.md)

---

## 📈 Roadmap

Planned features:
- [ ] Discord notifications
- [ ] ML-based score refinement
- [ ] Multi-exchange support (Magic Eden, Orca)
- [ ] Historical crash patterns database
- [ ] Auto-sell on crash (manual exit only for now)
- [ ] Mobile app interface

---

## ⚖️ Disclaimer

**This bot is for educational purposes.** Trading crypto carries significant risk:
- Test thoroughly in PAPER MODE first
- Start with small amounts
- Never invest more than you can afford to lose
- DYOR (Do Your Own Research)
- Not financial advice — make your own decisions

---

## 📝 License

MIT License - See LICENSE file

---

## 🤝 Contributing

Found a bug? Have a feature idea? PRs welcome!

1. Test locally first
2. Create feature branch
3. Write clear commit messages
4. Submit PR with description

---

**Happy Trading! 🚀**

Questions? Check the docs or review [PORTFOLIO_WATCHER_QUICKSTART.md](PORTFOLIO_WATCHER_QUICKSTART.md) for the new crash detection feature.
