#!/bin/bash
# ============================================================
#  Phobos Secondary Server Setup
#  Deploys WG + obfuscator + mini-API (no web panel)
#
#  Usage:
#    MAIN_SERVER=144.124.252.104 MAIN_API_KEY=secret123 \
#    bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/server/secondary-setup.sh)
# ============================================================

set -e

MAIN_SERVER="${MAIN_SERVER:?Set MAIN_SERVER=ip_of_main_server}"
MAIN_API_KEY="${MAIN_API_KEY:?Set MAIN_API_KEY=your_api_key}"
OBF_PORTS="${OBF_PORTS:-51821,51822,51823}"
PHOBOS_DIR="/opt/Phobos"

SERVER_IP=$(curl -s https://api.ipify.org || hostname -I | awk '{print $1}')
IFACE=$(ip route get 8.8.8.8 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="dev") print $(i+1)}' | head -1)
IFACE="${IFACE:-eth0}"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║    Phobos Secondary Server Setup                     ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Server IP   : $SERVER_IP"
echo "║  Main server : $MAIN_SERVER"
echo "║  OBF ports   : $OBF_PORTS"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. Dependencies ──
echo "[1/6] Installing dependencies..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wireguard jq curl python3 python3-flask gunicorn

# ── 2. Phobos binaries ──
echo "[2/6] Getting Phobos binaries..."
mkdir -p "$PHOBOS_DIR"/{server,clients,bin}

# Clone obfuscator binary from main Phobos repo
REPO_DIR="/tmp/phobos-repo"
rm -rf "$REPO_DIR"
mkdir -p "$REPO_DIR"
cd "$REPO_DIR"
git init -q
git remote add origin https://github.com/Ground-Zerro/Phobos.git
git config core.sparseCheckout true
echo "wg-obfuscator" > .git/info/sparse-checkout
git pull origin main -q 2>/dev/null
cp -f wg-obfuscator/bin/wg-obfuscator-$(uname -m) "$PHOBOS_DIR/bin/" 2>/dev/null || true
chmod +x "$PHOBOS_DIR/bin/"wg-obfuscator-*
ln -sf "$PHOBOS_DIR/bin/wg-obfuscator-$(uname -m)" /usr/local/bin/wg-obfuscator
rm -rf "$REPO_DIR"
echo "   Obfuscator installed."

# ── 3. WireGuard ──
echo "[3/6] Configuring WireGuard..."
WG_PRIV=$(wg genkey)
WG_PUB=$(echo "$WG_PRIV" | wg pubkey)

cat > /etc/wireguard/wg0.conf << WGEOF
[Interface]
Address = 10.25.0.1/16
ListenPort = 51820
PrivateKey = $WG_PRIV
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o $IFACE -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o $IFACE -j MASQUERADE
WGEOF

sysctl -w net.ipv4.ip_forward=1 -q
grep -q "net.ipv4.ip_forward = 1" /etc/sysctl.conf || echo "net.ipv4.ip_forward = 1" >> /etc/sysctl.conf

systemctl enable wg-quick@wg0 -q
systemctl restart wg-quick@wg0
echo "   WireGuard running."

# ── 4. Obfuscator (multi-port) ──
echo "[4/6] Setting up obfuscator..."
OBF_KEY=$(head -c 32 /dev/urandom | base64 | tr -d "/+=" | head -c 32)

# Block direct WG access
iptables -C INPUT -p udp --dport 51820 ! -s 127.0.0.1 -j DROP 2>/dev/null \
    || iptables -A INPUT -p udp --dport 51820 ! -s 127.0.0.1 -j DROP

IFS=',' read -ra PORTS <<< "$OBF_PORTS"
for PORT in "${PORTS[@]}"; do
    cat > "$PHOBOS_DIR/server/wg-obfuscator-${PORT}.conf" << EOF
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

    cat > /etc/systemd/system/wg-obfuscator-${PORT}.service << EOF
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
for PORT in "${PORTS[@]}"; do
    systemctl enable "wg-obfuscator-${PORT}" -q
    systemctl start "wg-obfuscator-${PORT}"
done
echo "   Obfuscator running on ports: $OBF_PORTS"

# ── 5. Save config ──
echo "[5/6] Saving config..."
cat > "$PHOBOS_DIR/server/server.env" << EOF
SERVER_WG_PRIVATE_KEY=$WG_PRIV
SERVER_WG_PUBLIC_KEY=$WG_PUB
SERVER_PUBLIC_IP_V4=$SERVER_IP
OBFUSCATOR_KEY=$OBF_KEY
OBFUSCATOR_PORTS=$OBF_PORTS
MAIN_SERVER=$MAIN_SERVER
MAIN_API_KEY=$MAIN_API_KEY
ROLE=secondary
EOF

# ── 6. Mini-API ──
echo "[6/6] Setting up mini-API..."
cat > "$PHOBOS_DIR/server/api.py" << 'PYEOF'
#!/usr/bin/env python3
"""Phobos Secondary Server API — peer management + health."""
import json, os, subprocess
from flask import Flask, request, jsonify

app = Flask(__name__)

def load_env():
    env = {}
    with open("/opt/Phobos/server/server.env") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v
    return env

def check_api_key():
    env = load_env()
    key = request.headers.get("X-API-Key", "")
    return key == env.get("MAIN_API_KEY", "")

@app.route("/api/health")
def health():
    try:
        wg = subprocess.check_output(["wg", "show", "wg0"], text=True, timeout=5)
        peers = wg.count("peer:")
        return jsonify({"status": "ok", "peers": peers})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/api/peers", methods=["GET"])
def list_peers():
    if not check_api_key():
        return jsonify({"error": "unauthorized"}), 401
    try:
        out = subprocess.check_output(["wg", "show", "wg0", "allowed-ips"], text=True, timeout=5)
        peers = {}
        for line in out.strip().split("\n"):
            if "\t" in line:
                pub, ips = line.split("\t", 1)
                peers[pub.strip()] = ips.strip()
        return jsonify({"peers": peers})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/peers/add", methods=["POST"])
def add_peer():
    if not check_api_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.json
    pub_key = data.get("public_key", "")
    allowed_ips = data.get("allowed_ips", "")
    if not pub_key or not allowed_ips:
        return jsonify({"error": "missing public_key or allowed_ips"}), 400
    try:
        subprocess.run(["wg", "set", "wg0", "peer", pub_key, "allowed-ips", allowed_ips], check=True, timeout=5)
        subprocess.run(["wg-quick", "save", "wg0"], timeout=5)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/peers/remove", methods=["POST"])
def remove_peer():
    if not check_api_key():
        return jsonify({"error": "unauthorized"}), 401
    pub_key = request.json.get("public_key", "")
    if not pub_key:
        return jsonify({"error": "missing public_key"}), 400
    try:
        subprocess.run(["wg", "set", "wg0", "peer", pub_key, "remove"], check=True, timeout=5)
        subprocess.run(["wg-quick", "save", "wg0"], timeout=5)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/info")
def info():
    if not check_api_key():
        return jsonify({"error": "unauthorized"}), 401
    env = load_env()
    return jsonify({
        "ip": env.get("SERVER_PUBLIC_IP_V4"),
        "wg_public_key": env.get("SERVER_WG_PUBLIC_KEY"),
        "obfuscator_key": env.get("OBFUSCATOR_KEY"),
        "ports": env.get("OBFUSCATOR_PORTS", "51821").split(","),
        "role": "secondary"
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8444)
PYEOF

cat > /etc/systemd/system/phobos-api.service << EOF
[Unit]
Description=Phobos Secondary API
After=network.target wg-quick@wg0.service

[Service]
Type=simple
WorkingDirectory=/opt/Phobos/server
ExecStart=/usr/bin/gunicorn -w 1 -b 0.0.0.0:8444 api:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable phobos-api -q
systemctl start phobos-api

# ── 7. Register with main server ──
echo ""
echo "Registering with main server..."
REG_DATA=$(cat << EOF
{
    "ip": "$SERVER_IP",
    "wg_public_key": "$WG_PUB",
    "obfuscator_key": "$OBF_KEY",
    "ports": "$(echo $OBF_PORTS)"
}
EOF
)
curl -s -X POST "http://${MAIN_SERVER}:8443/api/servers/register" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: ${MAIN_API_KEY}" \
    -d "$REG_DATA" || echo "   (Registration will be done manually via panel)"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║         Secondary Server Ready!                      ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Server IP     : $SERVER_IP"
echo "║  WG Public Key : $WG_PUB"
echo "║  OBF Key       : $OBF_KEY"
echo "║  OBF Ports     : $OBF_PORTS"
echo "║  API           : http://$SERVER_IP:8444"
echo "║  Main server   : $MAIN_SERVER"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
