#!/bin/zsh

set -euo pipefail

REPO_DIR="/Users/rosalindjames/DigitalDegenX_Bot"
DASHBOARD_DIR="$REPO_DIR/nextjs"
LAUNCHD_LABEL="com.digitaldegenx.dashboard"
SYSTEMD_SERVICE="digitaldegenx-dashboard"

cd "$DASHBOARD_DIR"

if [ ! -d node_modules ]; then
  npm ci
fi

npm run build

if launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL"
  echo "Dashboard rebuilt and restarted via launchd: $LAUNCHD_LABEL"
  exit 0
fi

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "$SYSTEMD_SERVICE.service" >/dev/null 2>&1; then
  sudo systemctl restart "$SYSTEMD_SERVICE"
  echo "Dashboard rebuilt and restarted via systemd: $SYSTEMD_SERVICE"
  exit 0
fi

echo "Dashboard rebuilt. No known service manager unit was found to restart automatically."
