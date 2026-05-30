#!/bin/bash
# ============================================================
#  PCA Phobos — TURNKEY installer (primary / panel node)
#
#  One command, all dependencies, from a clean VPS:
#    bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/install.sh)
#
#  Installs, in order:
#    deps -> wg-obfuscator (Ground-Zerro) -> WireGuard wg0 ->
#    obfuscator services -> Phobos repo (onboarding scripts) +
#    PCA patches -> web panel -> nginx (/init,/packages) ->
#    server-side router watchdog.
#
#  Env (all optional):
#    PANEL_PORT   random 10000-59999   web panel port
#    PANEL_PASS   OcAdmin2026!         panel admin password
#    API_KEY      random               shared key (agents + router pull token)
#    OBF_PORTS    2083,5443,993        obfuscator listen ports
#    TG_TOKEN / TG_CHAT                Telegram alerts
#    PCA_BRANCH   main                 branch to pull PCA files from
# ============================================================
set -e

PANEL_PASS="${PANEL_PASS:-OcAdmin2026!}"
TG_TOKEN="${TG_TOKEN:-}"
TG_CHAT="${TG_CHAT:-}"
OBF_PORTS="${OBF_PORTS:-2083,5443,993}"
# Канал: stable по умолчанию (ветка main). Бета — по желанию: CHANNEL=beta (или PCA_BRANCH=beta).
CHANNEL="${CHANNEL:-stable}"
if [ -z "${PCA_BRANCH:-}" ]; then
    case "$CHANNEL" in beta) PCA_BRANCH="beta";; *) PCA_BRANCH="main";; esac
fi
GH_TOKEN="${GH_TOKEN:-}"   # set for private-repo installs; empty for public
PHOBOS_DIR="/opt/Phobos"
PANEL_DIR="/opt/phobos-panel"
RAW="https://raw.githubusercontent.com/andrey271192/PCA_Phobos/${PCA_BRANCH}"

[ "$EUID" -eq 0 ] || { echo "Run as root"; exit 1; }

if [ -z "$PANEL_PORT" ]; then
    PANEL_PORT=$(shuf -i 10000-59999 -n 1 2>/dev/null || awk 'BEGIN{srand(); print int(10000+rand()*50000)}')
fi
API_KEY="${API_KEY:-$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)}"

SERVER_IP=$(curl -s -m8 https://api.ipify.org || hostname -I | awk '{print $1}')
IFACE=$(ip route get 8.8.8.8 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="dev") print $(i+1)}' | head -1)
IFACE="${IFACE:-eth0}"
ARCH=$(uname -m)

echo "============================================"
echo "  PCA Phobos - turnkey primary install"
echo "  IP=$SERVER_IP iface=$IFACE arch=$ARCH"
echo "  panel port=$PANEL_PORT  obf ports=$OBF_PORTS"
echo "============================================"

# ── 1. dependencies ──
echo "[1/9] dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq wireguard wireguard-tools iptables jq curl git \
    python3 python3-flask gunicorn nginx cron python3-qrcode >/dev/null
systemctl enable cron -q 2>/dev/null || true; systemctl start cron 2>/dev/null || true

# ── 2. wg-obfuscator binary (Ground-Zerro) ──
echo "[2/9] wg-obfuscator..."
mkdir -p "$PHOBOS_DIR"/{server,clients,bin,tokens,www/init,www/packages,packages}
if [ ! -x /usr/local/bin/wg-obfuscator ]; then
    R=/tmp/phobos-obf; rm -rf "$R"; mkdir -p "$R"; cd "$R"
    git init -q; git remote add origin https://github.com/Ground-Zerro/Phobos.git
    git config core.sparseCheckout true; echo "wg-obfuscator" > .git/info/sparse-checkout
    git pull origin main -q
    cp -f "wg-obfuscator/bin/wg-obfuscator-${ARCH}" "$PHOBOS_DIR/bin/" 2>/dev/null || true
    chmod +x "$PHOBOS_DIR/bin/"wg-obfuscator-* 2>/dev/null || true
    ln -sf "$PHOBOS_DIR/bin/wg-obfuscator-${ARCH}" /usr/local/bin/wg-obfuscator
    cd /; rm -rf "$R"
fi
[ -x /usr/local/bin/wg-obfuscator ] || { echo "ERROR: obfuscator binary for $ARCH missing"; exit 1; }

# ── 3. Phobos repo (onboarding scripts) ──
echo "[3/9] Phobos repo (onboarding scripts)..."
R="$PHOBOS_DIR/repo"; rm -rf "$R"; mkdir -p "$R"; cd "$R"
git init -q; git remote add origin https://github.com/Ground-Zerro/Phobos.git
git config core.sparseCheckout true
printf 'server\nclient\n' > .git/info/sparse-checkout
git pull origin main -q; rm -rf .git
find "$R" -name '*.sh' -exec chmod +x {} \; 2>/dev/null || true
cd /

# ── 4. WireGuard wg0 (primary) ──
echo "[4/9] WireGuard wg0..."
if [ ! -f /etc/wireguard/wg0.conf ]; then
    WG_PRIV=$(wg genkey); WG_PUB=$(echo "$WG_PRIV" | wg pubkey)
    cat > /etc/wireguard/wg0.conf <<WG
[Interface]
Address = 10.25.0.1/16
ListenPort = 51820
PrivateKey = $WG_PRIV
PostUp = iptables -I FORWARD 1 -i wg0 -j ACCEPT; iptables -I FORWARD 1 -o wg0 -m state --state RELATED,ESTABLISHED -j ACCEPT; iptables -t nat -A POSTROUTING -o $IFACE -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -D FORWARD -o wg0 -m state --state RELATED,ESTABLISHED -j ACCEPT; iptables -t nat -D POSTROUTING -o $IFACE -j MASQUERADE
WG
    chmod 600 /etc/wireguard/wg0.conf
else
    WG_PRIV=$(grep '^PrivateKey' /etc/wireguard/wg0.conf | cut -d= -f2- | tr -d ' ')
    WG_PUB=$(echo "$WG_PRIV" | wg pubkey)
fi
sysctl -w net.ipv4.ip_forward=1 -q
grep -q '^net.ipv4.ip_forward = 1' /etc/sysctl.conf || echo 'net.ipv4.ip_forward = 1' >> /etc/sysctl.conf
systemctl enable wg-quick@wg0 -q 2>/dev/null || true
systemctl restart wg-quick@wg0

# ── 5. obfuscator services (multi-port) ──
echo "[5/9] obfuscator services..."
OBF_KEY=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
# Block direct WG (force obfuscation) unless ALLOW_PLAIN_WG=1 (e.g. for iOS WireGuard)
if [ -z "${ALLOW_PLAIN_WG:-}" ]; then
    iptables -C INPUT -p udp --dport 51820 ! -s 127.0.0.1 -j DROP 2>/dev/null \
        || iptables -A INPUT -p udp --dport 51820 ! -s 127.0.0.1 -j DROP
else
    iptables -D INPUT -p udp --dport 51820 ! -s 127.0.0.1 -j DROP 2>/dev/null || true
fi
IFS=',' read -ra PORTS <<< "$OBF_PORTS"
for PORT in "${PORTS[@]}"; do
    cat > "$PHOBOS_DIR/server/wg-obfuscator-${PORT}.conf" <<EOF
[instance]
source-if = 0.0.0.0
source-lport = ${PORT}
target = 127.0.0.1:51820
key = ${OBF_KEY}
masking = AUTO
verbose = INFO
idle-timeout = 300
max-dummy = 50
EOF
    cat > /etc/systemd/system/wg-obfuscator-${PORT}.service <<EOF
[Unit]
Description=WireGuard Obfuscator (port ${PORT})
After=network.target wg-quick@wg0.service
[Service]
Type=simple
ExecStart=/usr/local/bin/wg-obfuscator --config ${PHOBOS_DIR}/server/wg-obfuscator-${PORT}.conf
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
done
systemctl daemon-reload
for PORT in "${PORTS[@]}"; do systemctl enable wg-obfuscator-${PORT} -q; systemctl restart wg-obfuscator-${PORT}; done

# server.env (primary)
cat > "$PHOBOS_DIR/server/server.env" <<EOF
SERVER_WG_PRIVATE_KEY=$WG_PRIV
SERVER_WG_PUBLIC_KEY=$WG_PUB
SERVER_PUBLIC_IP_V4=$SERVER_IP
OBFUSCATOR_KEY=$OBF_KEY
OBFUSCATOR_PORTS=$OBF_PORTS
CLIENT_WG_PORT=51820
ROLE=primary
EOF

# ── 6. PCA patches over onboarding scripts + server-side helpers ──
echo "[6/9] PCA patches (tunnel-pull, self-heal, watchdog, 403 fix)..."
fetch() {
    if [ -n "$GH_TOKEN" ]; then
        curl -fsSL -m20 -H "Authorization: token $GH_TOKEN" "$RAW/$1" -o "$2" && return 0
    else
        curl -fsSL -m20 "$RAW/$1" -o "$2" && return 0
    fi
    echo "  WARN: fetch $1 failed"; return 1
}
fetch overlay/phobos-client.sh                 "$PHOBOS_DIR/repo/server/scripts/phobos-client.sh"        && chmod +x "$PHOBOS_DIR/repo/server/scripts/phobos-client.sh"
fetch overlay/install-router.sh.template       "$PHOBOS_DIR/repo/client/templates/install-router.sh.template"
fetch overlay/router-configure-wireguard.sh    "$PHOBOS_DIR/repo/client/templates/router-configure-wireguard.sh" && chmod +x "$PHOBOS_DIR/repo/client/templates/router-configure-wireguard.sh"
fetch overlay/phobos-pull.sh                    "$PHOBOS_DIR/repo/client/templates/phobos-pull.sh"        && chmod +x "$PHOBOS_DIR/repo/client/templates/phobos-pull.sh"
fetch server/phobos-health.sh                   "$PHOBOS_DIR/server/phobos-health.sh"                     && chmod +x "$PHOBOS_DIR/server/phobos-health.sh"
fetch server/phobos-pull.sh                     "$PHOBOS_DIR/server/phobos-pull.sh"                       && chmod +x "$PHOBOS_DIR/server/phobos-pull.sh"
fetch server/phobos-router-watchdog.py          "$PHOBOS_DIR/server/phobos-router-watchdog.py"
fetch update.sh                                 "$PHOBOS_DIR/server/update.sh"             && chmod +x "$PHOBOS_DIR/server/update.sh" && ln -sf "$PHOBOS_DIR/server/update.sh" /usr/local/bin/phobos-update
mkdir -p "$PANEL_DIR"; fetch VERSION "$PANEL_DIR/.version" 2>/dev/null || true
[ -f "$PHOBOS_DIR/tokens/tokens.json" ] || echo '[]' > "$PHOBOS_DIR/tokens/tokens.json"

# ── 7. web panel ──
echo "[7/9] web panel..."
mkdir -p "$PANEL_DIR"
fetch app.py "$PANEL_DIR/app.py" || { echo "ERROR: panel app.py fetch failed"; exit 1; }
if [ ! -f "$PANEL_DIR/settings.json" ]; then
    cat > "$PANEL_DIR/settings.json" <<EOF
{
  "admin_pass": "$PANEL_PASS",
  "tg_bot_token": "$TG_TOKEN",
  "tg_chat_id": "$TG_CHAT",
  "monitor_interval": 30,
  "server_api_key": "$API_KEY",
  "labels": {},
  "subscriptions": {},
  "router_access": {},
  "client_assignments": {}
}
EOF
fi
echo "$PANEL_PORT" > "$PANEL_DIR/.port"
cat > /etc/systemd/system/phobos-panel.service <<EOF
[Unit]
Description=Phobos VPN Web Panel
After=network.target wg-quick@wg0.service
Wants=wg-quick@wg0.service
[Service]
Type=simple
WorkingDirectory=$PANEL_DIR
ExecStart=/usr/bin/gunicorn -w 1 -b 0.0.0.0:$PANEL_PORT app:app
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload; systemctl enable phobos-panel -q; systemctl restart phobos-panel

# ── 8. nginx (serve /init + /packages over plain HTTP for routers) ──
echo "[8/9] nginx..."
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
cat > /etc/nginx/sites-available/phobos <<'NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    location /init/     { alias /opt/Phobos/www/init/;     default_type application/x-sh; }
    location /packages/ { alias /opt/Phobos/www/packages/; default_type application/octet-stream; }
    location /app/      { alias /opt/Phobos/www/app/;      default_type application/octet-stream; }
    location / { return 404; }
}
NGINX
ln -sf /etc/nginx/sites-available/phobos /etc/nginx/sites-enabled/phobos
mkdir -p /opt/Phobos/www/app; chmod 755 /opt/Phobos/www /opt/Phobos/www/init /opt/Phobos/www/packages /opt/Phobos/www/app
nginx -t >/dev/null 2>&1 && systemctl enable nginx -q 2>/dev/null && systemctl restart nginx || echo "  WARN: nginx config test failed"

# ── 9. router watchdog (auto reboot-recovery) ──
echo "[9/9] router watchdog..."
if [ -f "$PHOBOS_DIR/server/phobos-router-watchdog.py" ]; then
    # set -e safe: grep -v on an empty crontab returns 1, so guard with || true
    CRON_CUR=$(crontab -l 2>/dev/null | grep -v phobos-router-watchdog || true)
    printf '%s\n%s\n' "$CRON_CUR" \
      "*/3 * * * * /usr/bin/python3 $PHOBOS_DIR/server/phobos-router-watchdog.py >/dev/null 2>&1" \
      | grep -v '^[[:space:]]*$' | crontab -
fi

sleep 2
echo ""
echo "============================================"
echo "  Installation complete"
echo "  Panel : http://$SERVER_IP:$PANEL_PORT"
echo "  Login : admin"
echo "  Pass  : $PANEL_PASS"
echo "  API key (agents+pull): $API_KEY"
echo "  WG pub: $WG_PUB"
echo "============================================"
echo "Status:"
for s in wg-quick@wg0 phobos-panel nginx; do printf "  %-18s %s\n" "$s" "$(systemctl is-active $s 2>/dev/null)"; done
for PORT in "${PORTS[@]}"; do printf "  %-18s %s\n" "wg-obfuscator-$PORT" "$(systemctl is-active wg-obfuscator-$PORT 2>/dev/null)"; done
