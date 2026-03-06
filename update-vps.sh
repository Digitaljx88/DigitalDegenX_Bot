#!/bin/bash

# VPS Update Script - Pull latest changes from GitHub and restart bot

set -e  # Exit on any error

echo "🔄 DigitalDegenX Bot - VPS Update Script"
echo "========================================"
echo ""

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

BOT_DIR="/opt/digitaldegen-bot"
SERVICE_NAME="digitaldegen-bot"

# 1. Verify we're in the right directory
echo -e "${BLUE}📍 Checking directory...${NC}"
if [ ! -d "$BOT_DIR" ]; then
    echo -e "${RED}❌ Bot directory not found at $BOT_DIR${NC}"
    exit 1
fi
cd "$BOT_DIR"
echo -e "${GREEN}✅ In correct directory: $BOT_DIR${NC}"
echo ""

# 2. Check current branch and status
echo -e "${BLUE}📋 Git status before update:${NC}"
git status
echo ""

# 3. Stop the bot service
echo -e "${BLUE}⏹️  Stopping bot service...${NC}"
sudo systemctl stop $SERVICE_NAME
echo -e "${GREEN}✅ Service stopped${NC}"
echo ""

# 4. Backup current version (optional but recommended)
echo -e "${BLUE}💾 Creating backup...${NC}"
BACKUP_DIR="$BOT_DIR/backups"
mkdir -p $BACKUP_DIR
BACKUP_FILE="$BACKUP_DIR/backup_$(date +%Y%m%d_%H%M%S).tar.gz"
tar -czf $BACKUP_FILE data/ bot.py config.py 2>/dev/null || true
echo -e "${GREEN}✅ Backup created: $BACKUP_FILE${NC}"
echo ""

# 5. Pull latest changes from GitHub
echo -e "${BLUE}🔄 Pulling latest changes from GitHub...${NC}"
git fetch origin main
git pull origin main
echo -e "${GREEN}✅ Changes pulled successfully${NC}"
echo ""

# 6. Check if we need to reinstall dependencies
echo -e "${BLUE}📦 Checking dependencies...${NC}"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo -e "${GREEN}✅ Dependencies up to date${NC}"
echo ""

# 6.1 Validate syntax before restart
echo -e "${BLUE}🧪 Running syntax validation...${NC}"
python -m compileall bot.py scanner.py pumpfeed.py portfolio_watcher.py >/dev/null
echo -e "${GREEN}✅ Syntax validation passed${NC}"
echo ""

# 7. Restart the bot service
echo -e "${BLUE}▶️  Starting bot service...${NC}"
sudo systemctl start $SERVICE_NAME
echo -e "${GREEN}✅ Service started${NC}"
echo ""

# 8. Verify service is running
echo -e "${BLUE}🔍 Verifying service status...${NC}"
if sudo systemctl is-active --quiet $SERVICE_NAME; then
    echo -e "${GREEN}✅ Service is running${NC}"
else
    echo -e "${RED}❌ Service failed to start. Check logs:${NC}"
    echo "   sudo journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi
echo ""

# 9. Show recent logs
echo -e "${BLUE}📝 Recent logs (last 20 lines):${NC}"
sudo journalctl -u $SERVICE_NAME -n 20 --no-pager
echo ""

echo -e "${GREEN}✨ Update completed successfully!${NC}"
echo ""
echo -e "${YELLOW}💡 Quick commands:${NC}"
echo "   View logs:    sudo journalctl -u $SERVICE_NAME -f"
echo "   Stop service: sudo systemctl stop $SERVICE_NAME"
echo "   Start service: sudo systemctl start $SERVICE_NAME"
echo "   Check status:  sudo systemctl status $SERVICE_NAME"
