#!/bin/bash
# ============================================================
#  PCA Phobos — Web Panel Installer
#  Requires: Phobos already installed (/opt/Phobos)
#
#  Usage:
#    bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/install.sh)
# ============================================================

set -e

PANEL_PASS="${PANEL_PASS:-OcAdmin2026!}"
TG_TOKEN="${TG_TOKEN:-}"
TG_CHAT="${TG_CHAT:-}"
PANEL_PORT="${PANEL_PORT:-8443}"
PANEL_DIR="/opt/phobos-panel"

# ── Check Phobos is installed ──
if [ ! -d "/opt/Phobos" ]; then
    echo "ERROR: Phobos not found at /opt/Phobos"
    echo "Install Phobos first: https://git.zerrolabs.org/Ground-Zerro/Phobos"
    exit 1
fi

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║        PCA Phobos Panel Installer                    ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Panel port  : $PANEL_PORT"
echo "║  Phobos dir  : /opt/Phobos"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. Install dependencies ──
echo "[1/3] Installing dependencies..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-flask gunicorn

# ── 2. Install panel ──
echo "[2/3] Installing web panel..."
mkdir -p "$PANEL_DIR"

SERVER_IP=$(curl -s https://api.ipify.org || hostname -I | awk '{print $1}')

curl -fsSL "https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/app.py" \
    | sed "s|SERVER_IP = .*|SERVER_IP = \"$SERVER_IP\"|g" \
    > "$PANEL_DIR/app.py"

# Create initial settings
if [ ! -f "$PANEL_DIR/settings.json" ]; then
    cat > "$PANEL_DIR/settings.json" <<EOF
{
  "admin_pass": "$PANEL_PASS",
  "tg_bot_token": "$TG_TOKEN",
  "tg_chat_id": "$TG_CHAT",
  "monitor_interval": 30,
  "labels": {},
  "subscriptions": {}
}
EOF
fi

# ── 3. Setup systemd service ──
echo "[3/3] Setting up service..."

curl -fsSL "https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/phobos-panel.service" \
    > /etc/systemd/system/phobos-panel.service

systemctl daemon-reload
systemctl enable phobos-panel -q
systemctl restart phobos-panel
sleep 2
systemctl is-active --quiet phobos-panel && echo "   Panel running." || { echo "ERROR: panel failed!"; journalctl -u phobos-panel -n 20; exit 1; }

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║              Installation Complete!                  ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Web Panel   : http://$SERVER_IP:$PANEL_PORT"
echo "║  Admin login : admin"
echo "║  Admin pass  : $PANEL_PASS"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
