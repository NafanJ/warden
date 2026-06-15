#!/usr/bin/env bash
# Install/update warden on the target host (run ON the host, from the repo root):
#   sudo bash deploy/install.sh
#
# The venv is built with `uv` on Python 3.12 — the host's default python3 is too
# new for the dependencies and ships without pip, so plain `python3 -m venv` +
# pip does not work here.
set -euo pipefail

DEST=/opt/warden

# The user who invoked sudo owns the install and runs the services (the systemd
# units use User=nathan). Build the venv as them so uv's cache and the venv
# permissions are correct.
RUN_USER="${SUDO_USER:-nathan}"
USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Locate uv: under sudo it is not on root's PATH, but lives in the user's ~/.local/bin.
UV="$(command -v uv || true)"
if [ -z "$UV" ] && [ -n "$USER_HOME" ] && [ -x "$USER_HOME/.local/bin/uv" ]; then
  UV="$USER_HOME/.local/bin/uv"
fi
if [ -z "$UV" ]; then
  echo "ERROR: 'uv' not found. Install it as $RUN_USER, then re-run:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

mkdir -p "$DEST"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude 'state' --exclude '.env' \
  --exclude 'incidents' \
  "$REPO_DIR/" "$DEST/"

if [ ! -f "$DEST/.env" ]; then
  cp "$DEST/.env.example" "$DEST/.env"
  echo ">>> Edit $DEST/.env before enabling the services (keys, mode, channel)."
fi

mkdir -p "$DEST/state"
chown -R "$RUN_USER":"$RUN_USER" "$DEST"

# Build/refresh the venv and install warden, as the run user (-H sets HOME so uv
# uses that user's cache).
if [ ! -d "$DEST/.venv" ]; then
  sudo -u "$RUN_USER" -H "$UV" venv --python 3.12 "$DEST/.venv"
fi
sudo -u "$RUN_USER" -H "$UV" pip install --python "$DEST/.venv/bin/python" -q -e "$DEST"

cp "$DEST/deploy/warden-sentinel.service" /etc/systemd/system/
cp "$DEST/deploy/warden-sentinel.timer" /etc/systemd/system/
cp "$DEST/deploy/warden-webhook.service" /etc/systemd/system/
cp "$DEST/deploy/warden-discord.service" /etc/systemd/system/
cp "$DEST/deploy/warden-summary.service" /etc/systemd/system/
cp "$DEST/deploy/warden-summary.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now warden-sentinel.timer
systemctl enable --now warden-summary.timer
# The webhook also serves the Plex playback endpoint (/plex), so enable it
# regardless of whether WhatsApp approvals are configured.
systemctl enable --now warden-webhook.service

echo
echo "warden installed. Sentinel timer + webhook (127.0.0.1:8484) active."
echo "Point Plex at it: Settings → Webhooks → http://localhost:8484/plex"
echo "Enable the approval channel once its keys are set in $DEST/.env:"
echo "  Discord (recommended):  systemctl enable --now warden-discord.service"
echo "Status: systemctl list-timers warden* ; tail -f $DEST/state/heartbeat.log"
