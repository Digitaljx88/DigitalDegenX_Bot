# VPS Deployment Guide

## Prerequisites

### Requirements:
- Linux VPS (Ubuntu 20.04+ or similar)
- SSH access to VPS
- Python 3.10+ installed
- Domain or VPS IP address
- Already tested bot locally

### Services You'll Need:
- Telegram Bot Token
- Solana RPC endpoint (Helius recommended)
- Jupiter API credentials
- DexScreener API access

---

## Step 1: Prepare Your VPS

### 1.1 SSH into VPS
```bash
ssh user@your_vps_ip
# Or if using key file:
ssh -i /path/to/key user@your_vps_ip
```

### 1.2 Update System
```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git curl wget
```

### 1.3 Create App Directory
```bash
# Create directory for bot
sudo mkdir -p /opt/digitaldegen-bot
sudo chown $USER:$USER /opt/digitaldegen-bot
cd /opt/digitaldegen-bot
```

---

## Step 2: Clone or Upload Code

### Option A: Clone from GitHub
```bash
cd /opt/digitaldegen-bot
git clone https://github.com/Digitaljx88/DigitalDegenX_Bot.git .
# Or if private:
git clone git@github.com:Digitaljx88/DigitalDegenX_Bot.git .
```

### Option B: Upload via SCP
```bash
# From your local machine:
scp -r /Users/rosalindjames/DigitalDegenX_Bot/* user@your_vps_ip:/opt/digitaldegen-bot/

# Or with key file:
scp -i /path/to/key -r /Users/rosalindjames/DigitalDegenX_Bot/* user@your_vps_ip:/opt/digitaldegen-bot/
```

### Option C: Upload via Git/Rsync (Recommended)
```bash
# From your local machine:
rsync -avz -e "ssh -i /path/to/key" /Users/rosalindjames/DigitalDegenX_Bot/ user@your_vps_ip:/opt/digitaldegen-bot/
```

---

## Step 3: Set Up Python Virtual Environment

```bash
cd /opt/digitaldegen-bot

# Create virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip setuptools wheel
```

---

## Step 4: Install Dependencies

### 4.1 Install from Requirements
```bash
cd /opt/digitaldegen-bot
source venv/bin/activate

# Create requirements.txt if you don't have it
cat > requirements.txt << 'EOF'
python-telegram-bot==20.3
requests==2.31.0
solders==0.20.0
solana==0.34.0
construct==2.10.68
EOF

# Install all requirements
pip install -r requirements.txt
```

### 4.2 Install Base Requirements
```bash
source venv/bin/activate

# Core dependencies
pip install python-telegram-bot==20.3
pip install requests==2.31.0
pip install solders==0.20.0
pip install solana==0.34.0

# Check installation
python3 -c "from telegram.ext import Application; print('✅ Telegram bot library installed')"
python3 -c "import requests; print('✅ Requests library installed')"
```

---

## Step 5: Configure Secrets

### 5.1 Create config.py
```bash
cd /opt/digitaldegen-bot
nano config.py
```

**Add these settings:**
```python
# config.py (VPS LIVE VERSION)

# ─── Telegram ─────────────────────────────────────────
TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"
AUTHORIZED_USERS = [YOUR_TELEGRAM_USER_ID, 987654321]  # Add your ID

# ─── Solana ───────────────────────────────────────────
SOLANA_NETWORK = "mainnet-beta"  # Change to mainnet for live!
SOLANA_RPC = "https://mainnet.helius-rpc.com/?api-key=YOUR_HELIUS_API_KEY"

# Paper trading wallet (generate new)
PAPER_START_SOL = 10.0
PAPER_MODE = True

# Live wallet (if using live mode)
WALLET_PRIVATE_KEY = ""  # Leave empty if paper only
IS_LIVE_MODE = False

# ─── APIs ─────────────────────────────────────────────
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"
DEXSCREENER_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/"
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search"

JUPITER_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP = "https://quote-api.jup.ag/v6/swap"

# ─── Scanner Settings ─────────────────────────────────
SCANNER_ENABLED = True
MIN_MCAP_USD = 10_000
MAX_MCAP_USD = 10_000_000
DEFAULT_ALERT_SCORE = 70

# ─── Paths ────────────────────────────────────────────
DATA_DIR = "/opt/digitaldegen-bot/data"

print("✅ Config loaded successfully")
```

**Save:** Press `Ctrl+X`, then `Y`, then `Enter`

### 5.2 Secure config.py
```bash
chmod 600 config.py
```

### 5.3 Create data directory
```bash
mkdir -p data
chmod 755 data
```

---

## Step 6: Run Bot as Service (Systemd)

### 6.1 Create systemd service file
```bash
sudo nano /etc/systemd/system/digitaldegen-bot.service
```

**Add this content:**
```ini
[Unit]
Description=DigitalDegenX Telegram Bot
After=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/opt/digitaldegen-bot
Environment="PATH=/opt/digitaldegen-bot/venv/bin"
ExecStart=/opt/digitaldegen-bot/venv/bin/python3 /opt/digitaldegen-bot/bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Resource limits
MemoryLimit=512M
CPUQuota=50%
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

**Save:** Press `Ctrl+X`, then `Y`, then `Enter`

### 6.2 Enable and start service
```bash
sudo systemctl daemon-reload
sudo systemctl enable digitaldegen-bot
sudo systemctl start digitaldegen-bot

# Check status
sudo systemctl status digitaldegen-bot

# View logs
sudo journalctl -u digitaldegen-bot -f
```

---

## Step 7: Logging & Monitoring

### 7.1 Set up application logging
```bash
cd /opt/digitaldegen-bot
cat > logging.conf << 'EOF'
[loggers]
keys=root

[handlers]
keys=console,file

[formatters]
keys=standard

[logger_root]
level=INFO
handlers=console,file

[handler_console]
class=StreamHandler
level=DEBUG
formatter=standard
args=(sys.stderr,)

[handler_file]
class=FileHandler
level=INFO
formatter=standard
args=('/var/log/digitaldegen-bot.log', 'a')

[formatter_standard]
format=%(asctime)s - %(name)s - %(levelname)s - %(message)s
datefmt=%Y-%m-%d %H:%M:%S
EOF
```

### 7.2 Create log directory
```bash
sudo mkdir -p /var/log/digitaldegen-bot
sudo chown $USER:$USER /var/log/digitaldegen-bot
```

### 7.3 View logs
```bash
# Real-time logs
sudo journalctl -u digitaldegen-bot -f

# Last 100 lines
sudo journalctl -u digitaldegen-bot -n 100

# Search for errors
sudo journalctl -u digitaldegen-bot | grep -i error
```

---

## Step 8: Health Check & Monitoring

### 8.1 Create health check script
```bash
cat > /opt/digitaldegen-bot/check_health.sh << 'EOF'
#!/bin/bash

# Check if bot process is running
if sudo systemctl is-active --quiet digitaldegen-bot; then
    echo "✅ Bot is running"
    
    # Check if data files exist
    if [ -f "data/portfolios.json" ]; then
        echo "✅ Data files present"
    else
        echo "⚠️  Data files missing"
    fi
    
    # Check disk space
    DISK_USAGE=$(df -h /opt/digitaldegen-bot | awk 'NR==2 {print $5}' | sed 's/%//')
    if [ "$DISK_USAGE" -lt 80 ]; then
        echo "✅ Disk usage OK: ${DISK_USAGE}%"
    else
        echo "⚠️  Disk usage high: ${DISK_USAGE}%"
    fi
    
    # Check memory usage
    MEM_USAGE=$(ps aux | grep "python3.*bot.py" | grep -v grep | awk '{print $6}')
    if [ -z "$MEM_USAGE" ]; then
        echo "⚠️  Could not determine memory usage"
    else
        echo "✅ Memory usage: ${MEM_USAGE}K"
    fi
else
    echo "❌ Bot is NOT running"
    exit 1
fi
EOF

chmod +x /opt/digitaldegen-bot/check_health.sh
```

### 8.2 Run health check
```bash
./check_health.sh
```

### 8.3 Set up monitoring with cron
```bash
# Add to crontab
crontab -e

# Add this line to check bot every 5 minutes
*/5 * * * * /opt/digitaldegen-bot/check_health.sh >> /var/log/bot_health.log 2>&1
```

---

## Step 9: Backup Strategy

### 9.1 Create backup script
```bash
cat > /opt/digitaldegen-bot/backup.sh << 'EOF'
#!/bin/bash

BACKUP_DIR="/opt/digitaldegen-bot/backups"
mkdir -p $BACKUP_DIR

# Backup data
tar -czf $BACKUP_DIR/data_$(date +%Y%m%d_%H%M%S).tar.gz data/

# Keep only last 7 days of backups
find $BACKUP_DIR -name "data_*.tar.gz" -mtime +7 -delete

echo "✅ Backup completed at $(date)"
EOF

chmod +x /opt/digitaldegen-bot/backup.sh
```

### 9.2 Schedule daily backups
```bash
crontab -e

# Add this line for daily backup at 2 AM
0 2 * * * /opt/digitaldegen-bot/backup.sh >> /var/log/bot_backup.log 2>&1
```

### 9.3 Manual backup
```bash
./backup.sh
ls -lh backups/
```

---

## Step 10: Update & Deployment

### 10.1 Deploy updates
```bash
cd /opt/digitaldegen-bot

# Pull latest changes
git pull origin main

# If you uploaded files, restart service
sudo systemctl restart digitaldegen-bot

# Check logs for errors
sudo journalctl -u digitaldegen-bot -n 50
```

### 10.2 Hot reload (without restart)
For configuration-only changes:
```bash
# Edit config.py
nano config.py

# Bot will reload on next check cycle (usually within 15 seconds)
# No restart needed
```

### 10.3 Graceful restart
```bash
sudo systemctl restart digitaldegen-bot

# Monitor restart
sudo journalctl -u digitaldegen-bot -f
```

---

## Step 11: Security Hardening

### 11.1 Firewall setup
```bash
# If using ufw
sudo ufw allow 22/tcp   # SSH
sudo ufw enable

# Bot uses Telegram's servers, no inbound ports needed
```

### 11.2 File permissions
```bash
cd /opt/digitaldegen-bot

# Config should be readable only by user
chmod 600 config.py

# Data directory
chmod 755 data
chmod 644 data/*.json

# Scripts executable
chmod +x *.sh
```

### 11.3 Create dedicated bot user (Optional)
```bash
sudo useradd -m -s /bin/bash -d /home/digitaldegen digitaldegen
sudo chown -R digitaldegen:digitaldegen /opt/digitaldegen-bot

# Update systemd service User= to 'digitaldegen'
# Then restart service
```

---

## Step 12: Verify Deployment

### 12.1 Quick verification
```bash
# 1. Check service status
sudo systemctl status digitaldegen-bot

# 2. Check recent logs
sudo journalctl -u digitaldegen-bot -n 20

# 3. Test bot connectivity
# Send a message to your bot on Telegram
# Expected: Bot responds

# 4. Check data directory
ls -lah data/

# 5. Verify config loaded
grep -o "✅ Config loaded" <(sudo journalctl -u digitaldegen-bot -n 100)
```

### 12.2 Test specific features
```bash
# SSH into VPS
ssh user@your_vps_ip

# Activate venv
cd /opt/digitaldegen-bot
source venv/bin/activate

# Test imports
python3 -c "
from telegram.ext import Application
import requests
print('✅ All imports working')
"

# Test config
python3 -c "
import config
print(f'✅ Config loaded: Token={config.TELEGRAM_TOKEN[:10]}...')
print(f'✅ RPC: {config.SOLANA_RPC[:50]}...')
"
```

---

## Troubleshooting

### Bot won't start
```bash
# Check errors
sudo journalctl -u digitaldegen-bot -n 50

# Check if Python venv is intact
source venv/bin/activate
python3 -c "import sys; print(sys.prefix)"

# If corrupted, rebuild venv
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Memory issues
```bash
# Check memory usage
ps aux | grep bot.py

# If too high, check for memory leaks
sudo systemctl restart digitaldegen-bot

# Monitor memory growth
watch -n 5 'ps aux | grep bot.py'
```

### Disk space issue
```bash
# Check what's taking space
du -sh data/

# Backup and clean old logs
./backup.sh
sudo journalctl --vacuum=30d  # Keep 30 days
```

### Network connectivity
```bash
# Test Solana RPC
curl -X POST https://mainnet.helius-rpc.com/?api-key=YOUR_KEY \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"getSlot"}'

# Test Telegram
curl https://api.telegram.org/botYOUR_TOKEN/getMe
```

---

## Monitoring Dashboard (Optional)

### Using Grafana (Advanced)
```bash
# Install Prometheus
sudo apt install prometheus

# Install Grafana
sudo apt install grafana-server

# Create exporter for bot metrics
# Add to your bot.py:
from prometheus_client import start_http_server, Counter, Gauge
import threading

# Start metrics server
def start_metrics_server():
    start_http_server(8000)
    threading.Thread(target=start_metrics_server, daemon=True).start()
```

---

## Daily Operations

### Starting/Stopping Bot
```bash
# Start
sudo systemctl start digitaldegen-bot

# Stop
sudo systemctl stop digitaldegen-bot

# Restart
sudo systemctl restart digitaldegen-bot

# Check status
sudo systemctl status digitaldegen-bot
```

### Viewing Logs
```bash
# Live logs
sudo journalctl -u digitaldegen-bot -f

# Last 50 lines
sudo journalctl -u digitaldegen-bot -n 50

# Search for specific message
sudo journalctl -u digitaldegen-bot | grep "Auto-Sell"

# Export logs
sudo journalctl -u digitaldegen-bot > /tmp/bot_logs.txt
```

### Database/Data Inspection
```bash
cd /opt/digitaldegen-bot

# View portfolios
python3 -c "import json; print(json.dumps(json.load(open('data/portfolios.json')), indent=2))" | head -30

# View auto-sell configs
python3 -c "import json; print(json.dumps(json.load(open('data/auto_sell.json')), indent=2))" | head -50

# View trades log
python3 -c "import json; d=json.load(open('data/trade_log.json')); print(f'Total trades: {len(d[\"trades\"])}'); print(json.dumps(d['trades'][-5:], indent=2))"
```

---

## Automatic Restart on VPS Reboot

The systemd service automatically starts on reboot:

```bash
# Verify it's enabled
sudo systemctl is-enabled digitaldegen-bot
# Output: enabled

# It will start automatically when VPS reboots
```

---

## Performance Optimization

### 6.1 Increase ulimits
```bash
sudo nano /etc/security/limits.conf

# Add these lines
* soft nofile 65536
* hard nofile 65536
* soft nproc 65536
* hard nproc 65536
```

### 6.2 Optimize systemd service
```bash
# Already included in our service file, but check:
# MemoryLimit=512M
# CPUQuota=50%
# Adjust based on your VPS specs
```

---

## Rollback Procedure

If deployment goes wrong:

```bash
# 1. Stop bot
sudo systemctl stop digitaldegen-bot

# 2. Restore from backup
cd /opt/digitaldegen-bot
tar -xzf backups/data_LATEST.tar.gz

# 3. Checkout previous version
git log --oneline -5
git checkout <previous_commit_hash>

# 4. Rebuild venv if needed
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 5. Start bot
sudo systemctl start digitaldegen-bot

# 6. Monitor logs
sudo journalctl -u digitaldegen-bot -f
```

---

## Final Checklist

- [ ] VPS SSH access working
- [ ] Python 3.10+ installed
- [ ] Code cloned/uploaded
- [ ] Virtual environment created
- [ ] Dependencies installed
- [ ] config.py configured with real secrets
- [ ] Systemd service file created
- [ ] Service started and enabled
- [ ] Logs showing successful startup
- [ ] Bot responds on Telegram
- [ ] Data directory has correct permissions
- [ ] Backup script scheduled
- [ ] Health check running
- [ ] Monitor logs regularly

---

## Support & Debugging

**If you encounter issues:**

1. Check logs: `sudo journalctl -u digitaldegen-bot -f`
2. Verify imports: `python3 -c "from telegram.ext import Application"`
3. Test config: `python3 -c "import config; print(config.TELEGRAM_TOKEN)"`
4. SSH directly and run bot manually to see errors
5. Share logs and error messages

**Keep in touch with:**
- Bot logs location: `/var/log/bot_health.log`
- Data location: `/opt/digitaldegen-bot/data/`
- Service logs: `journalctl -u digitaldegen-bot`

