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
        out = subprocess.check_output(["wg", "show", "wg0", "dump"], text=True, timeout=5)
        peer_keys = []
        handshakes = {}
        for line in out.strip().split("\n")[1:]:
            parts = line.split("\t")
            if len(parts) >= 4:
                pub = parts[0]
                peer_keys.append(pub)
                try:
                    handshakes[pub] = int(parts[4])
                except Exception:
                    handshakes[pub] = 0
        peers = len(peer_keys)
    except Exception:
        peers = 0
        peer_keys = []
        handshakes = {}
    try:
        out = subprocess.check_output("top -bn1 | grep Cpu", shell=True, text=True, timeout=5)
        idle = float([x for x in out.split(",") if "id" in x][0].split()[0])
        cpu = f"{round(100 - idle, 1)}%"
    except Exception:
        cpu = "?"
    try:
        mem = subprocess.check_output("free -m", shell=True, text=True).split("\n")[1].split()
        mem_str = f"{mem[2]}/{mem[1]}MB"
    except Exception:
        mem_str = "?"
    return jsonify({"status": "ok", "peers": peers, "peer_keys": peer_keys, "handshakes": handshakes, "cpu": cpu, "mem": mem_str})

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
        "ports": env.get("OBFUSCATOR_PORTS", "2083").split(","),
        "role": "secondary"
    })

ROUTER_CONFIGS_DIR = "/opt/Phobos/server/router-configs"


@app.route("/api/router-config/<client_id>")
def router_config(client_id):
    """Serve a client's failover.conf so routers can PULL via the tunnel
    (http://10.25.0.1:8444/...). Token via ?token= or X-API-Key."""
    token = request.args.get("token", "") or request.headers.get("X-API-Key", "")
    if token != load_env().get("MAIN_API_KEY", ""):
        return ("forbidden", 403)
    path = os.path.join(ROUTER_CONFIGS_DIR, client_id + ".conf")
    if not os.path.exists(path):
        return ("not found", 404)
    with open(path) as fh:
        return (fh.read(), 200, {"Content-Type": "text/plain; charset=utf-8"})


@app.route("/api/router-config-set", methods=["POST"])
def router_config_set():
    """Panel fan-out: store a client's failover.conf on this server."""
    if not check_api_key():
        return jsonify({"status": "error", "msg": "unauthorized"}), 403
    data = request.get_json(force=True, silent=True) or {}
    cid = data.get("client_id", "")
    conf = data.get("conf", "")
    if not cid or "SERVER_1=" not in conf:
        return jsonify({"status": "error", "msg": "bad payload"}), 400
    os.makedirs(ROUTER_CONFIGS_DIR, exist_ok=True)
    with open(os.path.join(ROUTER_CONFIGS_DIR, cid + ".conf"), "w") as fh:
        fh.write(conf)
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8444)
