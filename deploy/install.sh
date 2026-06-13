#!/usr/bin/env bash
# Install/update warden on the target host (run ON the host, from the repo root):
#   sudo bash deploy/install.sh
set -euo pipefail

DEST=/opt/warden
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$DEST"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude 'state' --exclude '.env' \
  "$REPO_DIR/" "$DEST/"

if [ ! -f "$DEST/.env" ]; then
  cp "$DEST/.env.example" "$DEST/.env"
  echo ">>> Edit $DEST/.env before enabling the timer (keys, mode, channel)."
fi

if [ ! -d "$DEST/.venv" ]; then
  python3 -m venv "$DEST/.venv"
fi
"$DEST/.venv/bin/pip" install -q -e "$DEST"

mkdir -p "$DEST/state"
chown -R nathan:nathan "$DEST"

cp "$DEST/deploy/warden-sentinel.service" /etc/systemd/system/
cp "$DEST/deploy/warden-sentinel.timer" /etc/systemd/system/
cp "$DEST/deploy/warden-webhook.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now warden-sentinel.timer

echo "warden installed. Sentinel timer active. Webhook (optional until WhatsApp is set up):"
echo "  systemctl enable --now warden-webhook.service"
echo "Status: systemctl list-timers warden* ; tail -f $DEST/state/heartbeat.log"
