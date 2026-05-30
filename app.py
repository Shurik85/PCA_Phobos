#!/usr/bin/env python3
"""
PCA Phobos — Web Panel for Phobos (Obfuscated WireGuard VPN)
Management panel: clients, sessions, labels, subscriptions, Telegram alerts.
"""

import json, os, subprocess, threading, time, secrets, hashlib, re, fcntl
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, redirect, url_for, session, make_response

_settings_lock = threading.Lock()

app = Flask(__name__)

PHOBOS_DIR = "/opt/Phobos"
CLIENTS_DIR = f"{PHOBOS_DIR}/clients"
SERVER_ENV = f"{PHOBOS_DIR}/server/server.env"
PANEL_DIR = "/opt/phobos-panel"
SETTINGS_FILE = f"{PANEL_DIR}/settings.json"
SECRET_FILE = f"{PANEL_DIR}/.secret_key"
SERVER_IP = subprocess.getoutput("curl -s https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}'").strip()

os.makedirs(PANEL_DIR, exist_ok=True)

if os.path.exists(SECRET_FILE):
    app.secret_key = open(SECRET_FILE).read().strip()
else:
    app.secret_key = secrets.token_hex(32)
    with open(SECRET_FILE, "w") as f:
        f.write(app.secret_key)

DEFAULT_SETTINGS = {
    "admin_pass": "OcAdmin2026!",
    "tg_bot_token": "",
    "tg_chat_id": "",
    "monitor_interval": 30,
    "labels": {},
    "subscriptions": {}
}


def load_settings():
    with _settings_lock:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE) as f:
                s = json.load(f)
            for k, v in DEFAULT_SETTINGS.items():
                s.setdefault(k, v)
            return s
        return dict(DEFAULT_SETTINGS)


def save_settings(s):
    with _settings_lock:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)


def tg_send(text):
    s = load_settings()
    token, chat = s.get("tg_bot_token", ""), s.get("tg_chat_id", "")
    if not token or not chat:
        return
    try:
        import urllib.request
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def get_wg_peers():
    """Parse `wg show wg0` to get active peers with transfer/handshake info."""
    try:
        out = subprocess.check_output(["wg", "show", "wg0"], text=True, timeout=5)
    except Exception:
        return {}

    peers = {}
    current_pub = None
    for line in out.split("\n"):
        line = line.strip()
        if line.startswith("peer:"):
            current_pub = line.split("peer:")[1].strip()
            peers[current_pub] = {}
        elif current_pub and ":" in line:
            key, val = line.split(":", 1)
            peers[current_pub][key.strip()] = val.strip()
    return peers


def get_clients():
    """Read all clients from /opt/Phobos/clients/*/metadata.json."""
    clients = []
    clients_path = Path(CLIENTS_DIR)
    if not clients_path.exists():
        return clients
    for d in sorted(clients_path.iterdir()):
        meta_file = d / "metadata.json"
        if meta_file.exists():
            try:
                with open(meta_file) as f:
                    meta = json.load(f)
                meta["_dir"] = str(d)
                clients.append(meta)
            except Exception:
                pass
    return clients


def get_active_sessions():
    """Combine WG peers with client metadata to build session list."""
    peers = get_wg_peers()
    clients = get_clients()

    pub_to_client = {}
    for c in clients:
        pub_to_client[c.get("public_key", "")] = c

    sessions = []
    for pub_key, info in peers.items():
        handshake = info.get("latest handshake", "")
        if not handshake:
            continue

        client = pub_to_client.get(pub_key, {})
        client_id = client.get("client_id", "unknown")
        tunnel_ip = client.get("tunnel_ip_v4", "")

        endpoint = info.get("endpoint", "")
        real_ip = endpoint.split(":")[0] if endpoint else ""

        rx = info.get("transfer", "")
        rx_bytes = rx.split("received,")[0].strip() if "received," in rx else ""
        tx_bytes = rx.split("received,")[1].strip().replace("sent", "").strip() if "received," in rx else ""

        sessions.append({
            "client_id": client_id,
            "public_key": pub_key,
            "tunnel_ip": tunnel_ip,
            "real_ip": real_ip,
            "endpoint": endpoint,
            "handshake": handshake,
            "rx": rx_bytes,
            "tx": tx_bytes,
        })
    return sessions


def is_peer_online(handshake_str):
    """Check if peer had a handshake within last 3 minutes."""
    try:
        parts = handshake_str.split(",")
        total_seconds = 0
        for p in parts:
            p = p.strip()
            if "minute" in p:
                total_seconds += int(re.search(r"(\d+)", p).group(1)) * 60
            elif "second" in p:
                total_seconds += int(re.search(r"(\d+)", p).group(1))
            elif "hour" in p:
                total_seconds += int(re.search(r"(\d+)", p).group(1)) * 3600
        return total_seconds < 180
    except Exception:
        return False


def kick_peer(public_key):
    """Remove and re-add peer to force disconnect."""
    try:
        out = subprocess.check_output(["wg", "show", "wg0"], text=True, timeout=5)
        allowed = ""
        found = False
        for line in out.split("\n"):
            if line.strip().startswith("peer:") and public_key in line:
                found = True
            elif found and "allowed ips:" in line:
                allowed = line.split("allowed ips:")[1].strip()
                break

        subprocess.run(["wg", "set", "wg0", "peer", public_key, "remove"], timeout=5)
        if allowed:
            subprocess.run(["wg", "set", "wg0", "peer", public_key, "allowed-ips", allowed], timeout=5)
        return True
    except Exception:
        return False


def check_expiry():
    """Check subscription expiry, lock expired clients."""
    s = load_settings()  # Always reload fresh to avoid overwriting concurrent changes
    subs = s.get("subscriptions", {})
    today = datetime.now().date()
    changed = False

    for client_id, info in list(subs.items()):
        if not info.get("expiry"):
            continue
        try:
            exp_date = datetime.strptime(info["expiry"], "%Y-%m-%d").date()
        except ValueError:
            continue

        days_left = (exp_date - today).days

        if days_left <= 0 and not info.get("locked"):
            info["locked"] = True
            changed = True
            kick_client_by_id(client_id)
            tg_send(f"⛔ <b>{client_id}</b> — подписка истекла! Клиент заблокирован.")
        elif days_left == 3 and not info.get("warn3"):
            info["warn3"] = True
            changed = True
            tg_send(f"⚠️ <b>{client_id}</b> — подписка истекает через 3 дня ({info['expiry']})")
        elif days_left == 1 and not info.get("warn1"):
            info["warn1"] = True
            changed = True
            tg_send(f"⚠️ <b>{client_id}</b> — подписка истекает ЗАВТРА ({info['expiry']})")

    if changed:
        save_settings(s)


def kick_client_by_id(client_id):
    """Find client's public key and kick them."""
    clients = get_clients()
    for c in clients:
        if c.get("client_id") == client_id:
            kick_peer(c.get("public_key", ""))
            return True
    return False


prev_session_keys = None
prev_server_status = {}
_last_fanout = 0
server_stats_cache = {}
server_handshakes_cache = {}


def count_client_peers():
    """Count only client peers (exclude secondary server peers)."""
    clients = get_clients()
    client_pubs = {c.get("public_key", "") for c in clients}
    wg_peers = get_wg_peers()
    return sum(1 for pub in wg_peers if pub in client_pubs)


def get_local_stats():
    try:
        cpu = subprocess.getoutput("top -bn1 | grep 'Cpu(s)' | awk '{print $2}'").strip()
        mem = subprocess.getoutput("free -m | awk '/Mem:/{printf \"%.0f/%dMB\", $3, $2}'").strip()
        peers = count_client_peers()
        return {"cpu": cpu + "%", "mem": mem, "peers": peers, "status": "ok"}
    except Exception:
        return {"cpu": "?", "mem": "?", "peers": 0, "status": "ok"}


def get_remote_stats(server):
    try:
        import urllib.request
        url = f"http://{server['ip']}:8444/api/health"
        req = urllib.request.Request(url, headers={"X-API-Key": server.get("api_key", "")})
        resp = urllib.request.urlopen(req, timeout=3)
        data = json.loads(resp.read())
        # cache handshakes so page renders never block on remote HTTP
        server_handshakes_cache[server.get("ip", "")] = data.get("handshakes", {}) or {}
        # Filter peers: only count known client public keys
        clients = get_clients()
        client_pubs = {c.get("public_key", "") for c in clients}
        remote_keys = data.get("peer_keys", [])
        client_peers = sum(1 for k in remote_keys if k in client_pubs)
        return {"cpu": data.get("cpu", "—"), "mem": data.get("mem", "—"), "peers": client_peers, "status": "ok" if data.get("status") == "ok" else "down"}
    except Exception:
        return {"cpu": "—", "mem": "—", "peers": 0, "status": "down"}


def server_load(stats):
    """Estimate server load 0..1 from cached stats (CPU+RAM). Missing stats =
    neutral 0.5; explicitly down = 1.0 (avoid). Used by load-aware rebalance."""
    if not stats:
        return 0.5
    if stats.get("status") == "down":
        return 1.0
    try:
        cpu = float(str(stats.get("cpu", "")).replace("%", "").strip()) / 100.0
    except Exception:
        cpu = 0.5
    mem = 0.5
    try:
        used, total = str(stats.get("mem", "")).replace("MB", "").split("/")
        mem = float(used) / max(float(total), 1.0)
    except Exception:
        pass
    return max(0.0, min(1.0, 0.6 * cpu + 0.4 * mem))


def remote_handshakes(server_ip):
    # Pure cache read — the background session_monitor refreshes it every cycle.
    # Page renders must NEVER block on remote HTTP (that caused multi-second hangs).
    return server_handshakes_cache.get(server_ip, {})


def auto_assign_server(client_id):
    s = load_settings()
    assignments = s.get("client_assignments", {})
    if client_id in assignments:
        return assignments[client_id]

    all_servers = get_all_servers_ordered()
    server_ips = [sv["ip"] for sv in all_servers]
    if not server_ips:
        return SERVER_IP

    counts = {ip: 0 for ip in server_ips}
    for cid, sip in assignments.items():
        if sip in counts:
            counts[sip] += 1

    assigned = min(counts, key=counts.get)
    assignments[client_id] = assigned
    s["client_assignments"] = assignments
    save_settings(s)
    return assigned


def add_peer_to_server(server_ip, public_key, allowed_ips):
    """Add WG peer to a server via its API (secondary) or locally (primary)."""
    if server_ip == SERVER_IP:
        # Primary — add locally
        try:
            subprocess.run(["wg", "set", "wg0", "peer", public_key, "allowed-ips", allowed_ips], check=True, timeout=5)
            subprocess.run("wg-quick save wg0", shell=True, timeout=5)
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "msg": str(e)}
    # Secondary — call API
    servers = load_servers()
    api_key = ""
    for srv in servers:
        if srv["ip"] == server_ip:
            api_key = srv.get("api_key", "")
            break
    if not api_key:
        return {"status": "error", "msg": f"No API key for {server_ip}"}
    try:
        import urllib.request
        data = json.dumps({"public_key": public_key, "allowed_ips": allowed_ips}).encode()
        req = urllib.request.Request(
            f"http://{server_ip}:8444/api/peers/add",
            data=data,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        return {"status": "error", "msg": str(e)[:200]}


def restart_router_obfuscator(client_id):
    """Restart wg-obfuscator on router via SSH (tries tunnel, then static)."""
    s = load_settings()
    access = s.get("router_access", {}).get(client_id)
    if not access:
        return {"status": "error", "msg": f"No SSH for {client_id}"}
    ok_ip, out = _ssh_run(access, "/opt/etc/init.d/S49wg-obfuscator restart 2>/dev/null; echo OK")
    if ok_ip and "OK" in out:
        return {"status": "ok"}
    return {"status": "error", "msg": out[:200]}


def generate_failover_conf_for_client(client_id):
    s = load_settings()
    assignments = s.get("client_assignments", {})
    assigned_ip = assignments.get(client_id, SERVER_IP)
    all_servers = get_all_servers_ordered()

    by_ip = {sv["ip"]: sv for sv in all_servers}
    ordered = []
    if assigned_ip in by_ip:
        ordered.append(by_ip[assigned_ip])
    for sv in all_servers:
        if sv["ip"] != assigned_ip:
            ordered.append(sv)

    # Get main server WG public key
    try:
        main_wg_pub = subprocess.check_output(["wg", "show", "wg0", "public-key"], text=True, timeout=5).strip()
    except Exception:
        main_wg_pub = ""

    lines = ["# Phobos Failover Configuration"]
    for i, srv in enumerate(ordered, 1):
        lines.append(f"SERVER_{i}={srv['ip']}:{srv.get('ports', '2083,5443,993')}")
        lines.append(f"KEY_{i}={srv.get('obfuscator_key', '')}")
        wg_pub = main_wg_pub if srv.get("is_primary") else srv.get("wg_public_key", "")
        if wg_pub:
            lines.append(f"WGKEY_{i}={wg_pub}")
    return "\n".join(lines) + "\n"


def panel_version():
    try:
        return open(os.path.join(PANEL_DIR, ".version")).read().strip()
    except Exception:
        return "unknown"


def run_update(arg):
    import subprocess
    try:
        r = subprocess.run(["/opt/Phobos/server/update.sh", arg],
                           capture_output=True, text=True, timeout=150)
        return (r.stdout + r.stderr)[-1200:]
    except Exception as e:
        return "update error: " + str(e)


def build_phone_config(cid, mode="android"):
    """Assemble a phone client config from the client's files.
    android = WireGuard + [instance] obfuscator -> phobos:// link (PhobosWG app).
    ios     = plain WireGuard (no obfuscation), Endpoint -> server:51820."""
    import base64, urllib.parse
    cdir = os.path.join(CLIENTS_DIR, cid)
    wgpath = os.path.join(cdir, cid + ".conf")
    instpath = os.path.join(cdir, "wg-obfuscator.conf")
    if not os.path.exists(wgpath):
        return None, None
    wg = open(wgpath).read().rstrip()
    if mode == "ios":
        server_ip = SERVER_IP
        try:
            for ln in open(instpath):
                if ln.strip().startswith("target"):
                    server_ip = ln.split("=", 1)[1].strip().split(":")[0]
                    break
        except Exception:
            pass
        out = []
        for ln in wg.split("\n"):
            out.append("Endpoint = %s:51820" % server_ip if ln.strip().startswith("Endpoint") else ln)
        return "\n".join(out) + "\n", None
    inst = ""
    if os.path.exists(instpath):
        inst = open(instpath).read().strip()
    conf = wg + "\n\n" + inst + "\n"
    b64 = base64.urlsafe_b64encode(conf.encode()).decode().rstrip("=")
    link = "phobos://" + b64 + "#" + urllib.parse.quote(cid)
    return conf, link


def qr_datauri(data):
    try:
        import qrcode, io, base64
        buf = io.BytesIO()
        qrcode.make(data).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def fanout_router_config(client_id):
    """Push this client's failover.conf to every secondary server's agent so
    routers can PULL it through the tunnel (10.25.0.1:8444) — survives a public
    panel-IP ban and follows the active server. Best-effort, non-blocking."""
    try:
        conf = generate_failover_conf_for_client(client_id)
    except Exception:
        return
    import urllib.request
    payload = json.dumps({"client_id": client_id, "conf": conf}).encode()
    for srv in load_servers():
        ip = srv.get("ip", "")
        key = srv.get("api_key", "")
        if not ip:
            continue
        # skip servers the monitor already knows are down (avoid blocking)
        if server_stats_cache.get(ip, {}).get("status") == "down":
            continue
        try:
            req = urllib.request.Request(
                f"http://{ip}:8444/api/router-config-set",
                data=payload,
                headers={"X-API-Key": key, "Content-Type": "application/json"},
                method="POST")
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass


def _ssh_run(access, cmd, timeout=20):
    """Try SSH: tunnel IP first, then static IP as fallback."""
    ssh_user = access.get("ssh_user", "root")
    ssh_pass = access.get("ssh_pass", "")
    ips_to_try = []
    if access.get("ssh_ip"):
        ips_to_try.append(access["ssh_ip"])
    if access.get("ssh_static"):
        ips_to_try.append(access["ssh_static"])
    if not ips_to_try or not ssh_pass:
        return None, "SSH IP/pass missing"
    for ip in ips_to_try:
        try:
            result = subprocess.run(
                ["sshpass", "-p", ssh_pass, "ssh",
                 "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=3",
                 f"{ssh_user}@{ip}", cmd],
                capture_output=True, text=True, timeout=timeout
            )
            if result.returncode == 0:
                return ip, result.stdout
        except Exception:
            continue
    return None, f"SSH failed on all IPs: {', '.join(ips_to_try)}"


def push_config_to_router(client_id):
    s = load_settings()
    access = s.get("router_access", {}).get(client_id)
    if not access:
        return {"status": "error", "msg": f"No SSH for {client_id}"}

    conf = generate_failover_conf_for_client(client_id)
    write_cmd = f"mkdir -p /opt/etc/Phobos && cat > /opt/etc/Phobos/failover.conf << 'FAILCONF'\n{conf}FAILCONF"

    ok_ip, out = _ssh_run(access, write_cmd)
    if ok_ip:
        return {"status": "ok", "msg": f"Config pushed to {client_id} ({ok_ip})"}
    return {"status": "error", "msg": out[:200]}


def session_monitor():
    global prev_session_keys, prev_server_status, server_stats_cache, _last_fanout
    while True:
        try:
            s = load_settings()
            interval = s.get("monitor_interval", 30)

            # Client session monitoring
            sessions = get_active_sessions()
            current_keys = set()
            for sess in sessions:
                if is_peer_online(sess.get("handshake", "")):
                    key = (sess["client_id"], sess["real_ip"])
                    current_keys.add(key)

            if prev_session_keys is not None:
                labels = s.get("labels", {})
                for key in current_keys - prev_session_keys:
                    client_id, real_ip = key
                    label = labels.get(real_ip, "")
                    name = f"{label} ({real_ip})" if label else real_ip
                    tg_send(f"🟢 <b>{client_id}</b> connected — {name}")

                for key in prev_session_keys - current_keys:
                    client_id, real_ip = key
                    label = labels.get(real_ip, "")
                    name = f"{label} ({real_ip})" if label else real_ip
                    tg_send(f"🔴 <b>{client_id}</b> disconnected — {name}")

            prev_session_keys = current_keys

            # Server health monitoring
            servers = load_servers()
            local_stats = get_local_stats()
            server_stats_cache[SERVER_IP] = local_stats

            for srv in servers:
                ip = srv["ip"]
                stats = get_remote_stats(srv)
                server_stats_cache[ip] = stats
                was_up = prev_server_status.get(ip, "ok")
                now_status = stats["status"]

                if was_up == "ok" and now_status == "down":
                    tg_send(f"🔴 <b>Server {ip}</b> DOWN!")
                elif was_up == "down" and now_status == "ok":
                    tg_send(f"🟢 <b>Server {ip}</b> back ONLINE")

                prev_server_status[ip] = now_status

            # periodic config fan-out to secondaries (covers CLI-created clients
            # and config drift; gated ~5 min, skips down servers)
            global _last_fanout
            if time.time() - _last_fanout >= 300:
                _last_fanout = time.time()
                for _c in get_clients():
                    fanout_router_config(_c.get("client_id", ""))

            check_expiry()
            time.sleep(interval)
        except Exception:
            time.sleep(30)


monitor_thread = threading.Thread(target=session_monitor, daemon=True)
monitor_thread.start()


PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Phobos VPN Panel</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
.header{background:linear-gradient(135deg,#1e1b4b,#312e81);padding:20px;text-align:center;border-bottom:2px solid #4f46e5}
.header h1{font-size:1.6em;color:#a5b4fc}
.header .subtitle{font-size:.85em;color:#818cf8;margin-top:4px}
.container{max-width:1000px;margin:20px auto;padding:0 16px}
.card{background:#1e293b;border-radius:12px;padding:20px;margin-bottom:16px;border:1px solid #334155}
.card h2{color:#a5b4fc;font-size:1.1em;margin-bottom:12px;border-bottom:1px solid #334155;padding-bottom:8px}
table{width:100%;border-collapse:collapse}
th{text-align:left;color:#94a3b8;font-size:.8em;padding:8px 6px;border-bottom:1px solid #334155}
td{padding:8px 6px;border-bottom:1px solid #1e293b;font-size:.9em}
tr:hover{background:#262f3d}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.75em;font-weight:600}
.badge-on{background:#065f46;color:#6ee7b7}
.badge-off{background:#7f1d1d;color:#fca5a5}
.badge-warn{background:#78350f;color:#fcd34d}
.badge-lock{background:#581c87;color:#d8b4fe}
.btn{padding:6px 14px;border:none;border-radius:6px;cursor:pointer;font-size:.85em;color:#fff;text-decoration:none;display:inline-block}
.btn-primary{background:#4f46e5}.btn-primary:hover{background:#4338ca}
.btn-danger{background:#dc2626}.btn-danger:hover{background:#b91c1c}
.btn-sm{padding:4px 10px;font-size:.78em}
input,select{background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:8px 12px;border-radius:6px;font-size:.9em}
input:focus{outline:none;border-color:#4f46e5}
.form-row{display:flex;gap:10px;margin-bottom:10px;align-items:center;flex-wrap:wrap}
.form-row label{min-width:120px;color:#94a3b8;font-size:.85em}
.expiry-badge{font-size:.78em;padding:2px 6px;border-radius:6px}
.nav{display:flex;gap:10px;justify-content:center;margin:16px 0}
.nav a{color:#818cf8;text-decoration:none;padding:6px 16px;border-radius:6px;font-size:.9em}
.nav a:hover,.nav a.active{background:#312e81;color:#a5b4fc}
.footer{text-align:center;padding:30px 20px;color:#475569;font-size:.8em;border-top:1px solid #1e293b;margin-top:30px}
.footer a{color:#6366f1;text-decoration:none}
.footer a:hover{text-decoration:underline}
.login-box{max-width:360px;margin:80px auto;padding:30px;background:#1e293b;border-radius:12px;border:1px solid #334155}
.login-box h2{text-align:center;color:#a5b4fc;margin-bottom:20px}
.login-box input{width:100%;margin-bottom:12px}
.login-box .btn{width:100%}
.alert{padding:10px 14px;border-radius:8px;margin-bottom:12px;font-size:.85em}
.alert-error{background:#7f1d1d;color:#fca5a5;border:1px solid #991b1b}
.alert-ok{background:#065f46;color:#6ee7b7;border:1px solid #047857}
.help{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;background:#334155;color:#a5b4fc;font-size:11px;font-weight:700;cursor:pointer;margin-left:4px;user-select:none;flex:none}
.help:hover{background:#4f46e5;color:#fff}
.help-box{display:none;background:#0b1220;border:1px solid #4f46e5;color:#cbd5e1;padding:8px 11px;border-radius:8px;font-size:.78em;max-width:340px;margin-left:6px;line-height:1.4;vertical-align:middle}
.help-box.show{display:inline-block}
</style>
</head>
<body>
<div class="header">
<h1>🛡️ Phobos VPN Panel</h1>
<div class="subtitle">Obfuscated WireGuard · """ + SERVER_IP + """</div>
</div>
CONTENT
<div class="footer">
<b style="color:#a5b4fc">PCA Phobos Panel</b> — панель управления &copy; <a href="https://github.com/andrey271192/PCA_Phobos" target="_blank" rel="noopener">andrey271192</a><br>
Поддержать проект: <a href="https://boosty.to/andrey27/donate" target="_blank" rel="noopener">💖 Boosty</a> · <a href="https://finance.ozon.ru/apps/sbp/ozonbankpay/019dc200-2a5d-7931-a619-782d285f6798" target="_blank" rel="noopener">💳 Ozon Bank</a> · <a href="https://t.me/c/2474115507/78722" target="_blank" rel="noopener">✉️ Telegram</a>
<div style="margin-top:8px;font-size:.88em;color:#475569;line-height:1.7;text-align:left;max-width:660px;margin:8px auto 0">
<b>Создано на основе открытых проектов:</b><br>
&bull; <a href="https://github.com/Ground-Zerro/Phobos" target="_blank" rel="noopener">Phobos</a> — обфусцированный WireGuard VPN, автор <b>Ground_Zerro</b> &middot; <a href="https://boosty.to/ground_zerro" target="_blank" rel="noopener">поддержать</a><br>
&bull; <a href="https://github.com/wg-easy/wg-easy" target="_blank" rel="noopener">WireGuard Easy</a> — веб-панель WireGuard (AGPL-3.0), автор <b>Emile Nijssen</b> &middot; <a href="https://github.com/sponsors/WeeJeWel" target="_blank" rel="noopener">поддержать</a><br>
&bull; <a href="https://github.com/ClusterM/wg-obfuscator" target="_blank" rel="noopener">wg-obfuscator</a> — обфускация WireGuard-трафика (GPL-3.0), автор <b>ClusterM</b> &middot; <a href="https://boosty.to/cluster" target="_blank" rel="noopener">поддержать</a>
</div>
</div>
<script>
function showHelp(el){var b=el.nextElementSibling; if(b&&b.classList.contains('help-box')){b.classList.toggle('show');}}
</script>
</body></html>"""


def render(content):
    return PAGE.replace("CONTENT", content)


def cur_lang():
    try:
        from flask import request as _rq
        return "en" if _rq.cookies.get("lang", "ru") == "en" else "ru"
    except Exception:
        return "ru"


def tr(ru, en):
    return ru if cur_lang() == "ru" else en


def hlp(ru, en):
    """Inline '?' badge; click reveals a plain-language explanation (bilingual)."""
    txt = (ru if cur_lang() == "ru" else en).replace('"', '&quot;')
    return ('<span class="help" onclick="showHelp(this)">?</span>'
            '<span class="help-box">' + txt + '</span>')


def nav(active, sess_count=None):
    L = cur_lang()
    items = [
        ("sessions", "/sessions", "Сессии", "Sessions"),
        ("clients",  "/clients",  "Клиенты", "Clients"),
        ("labels",   "/labels",   "Метки",  "Labels"),
        ("servers",  "/servers",  "Серверы", "Servers"),
        ("settings", "/settings", "Настройки", "Settings"),
        ("logout",   "/logout",   "Выход",  "Logout"),
    ]
    parts = ['<div class="nav">']
    for key, href, ru, en in items:
        lbl = ru if L == "ru" else en
        if key == "sessions" and sess_count is not None:
            lbl = lbl + " (" + str(sess_count) + ")"
        cls = ' class="active"' if key == active else ''
        parts.append('<a href="' + href + '"' + cls + '>' + lbl + '</a>')
    other = "en" if L == "ru" else "ru"
    flag = "EN" if L == "ru" else "RU"
    parts.append('<a href="/lang/' + other + '" title="language" style="margin-left:12px;border:1px solid #4f46e5;color:#a5b4fc">' + flag + '</a>')
    parts.append('</div>')
    return "".join(parts)


@app.route("/lang/<code>")
def set_lang(code):
    from flask import make_response
    resp = make_response(redirect(request.referrer or url_for("dashboard")))
    resp.set_cookie("lang", "en" if code == "en" else "ru", max_age=31536000)
    return resp



@app.route("/login", methods=["GET", "POST"])
def login():
    s = load_settings()
    msg = ""
    if request.method == "POST":
        if request.form.get("password") == s["admin_pass"]:
            session["auth"] = True
            return redirect(url_for("dashboard"))
        msg = '<div class="alert alert-error">{tr("Неверный пароль","Wrong password")}</div>'
    html = f"""
    <div class="login-box">
    <h2>{tr("Вход в панель","Sign in")}</h2>
    {msg}
    <form method="post">
    <input type="password" name="password" placeholder="Пароль" autofocus>
    <button class="btn btn-primary" type="submit">{tr("Войти","Sign in")}</button>
    </form>
    </div>"""
    return render(html)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def auth_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/")
@auth_required
def dashboard():
    return redirect(url_for("sessions_page"))


@app.route("/sessions")
@auth_required
def sessions_page():
    s = load_settings()
    labels = s.get("labels", {})
    sessions = get_active_sessions()
    # Online = fresh handshake on ANY server (router may be on a backup)
    online_all = set()
    for _pub, _info in get_wg_peers().items():
        if is_peer_online(_info.get("latest handshake", "")):
            online_all.add(_pub)
    for _ip, _hs in server_handshakes_cache.items():
        for _pub, _ts in _hs.items():
            if _ts and (time.time() - _ts) < 180:
                online_all.add(_pub)

    rows = ""
    online_count = 0
    for sess in sessions:
        online = sess["public_key"] in online_all
        if online:
            online_count += 1
        status = '<span class="badge badge-on">Online</span>' if online else '<span class="badge badge-off">Offline</span>'
        real_ip = sess["real_ip"]
        label = labels.get(sess.get("tunnel_ip", ""), "") or labels.get(real_ip, "")

        rows += f"""<tr>
        <td>{sess['client_id']}</td>
        <td>{sess['tunnel_ip']}</td>
        <td>{real_ip}</td>
        <td>{('<b>' + label + '</b>') if label else '—'}</td>
        <td>{sess['handshake']}</td>
        <td>{sess['rx']} / {sess['tx']}</td>
        <td>{status}</td>
        <td style="display:flex;gap:4px;align-items:center"><a href="/kick/{sess['public_key']}" class="btn btn-danger btn-sm">{tr("Отключить","Kick")}</a>{hlp("Принудительно разорвать текущую сессию клиента (сбросить WG-пир). Клиент переподключится автоматически.","Force-drop the client current session (reset the WG peer). The client reconnects automatically.")}</td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="8" style="text-align:center;color:#64748b">{tr("Нет активных сессий","No active sessions")}</td></tr>'

    html = f"""
    <div class="container">
    {nav('sessions', online_count)}
    <div class="card">
    <h2>{tr("Активные сессии","Active sessions")}</h2>
    <table>
    <tr><th>Клиент</th><th>VPN IP</th><th>Real IP</th><th>Метка</th><th>Handshake</th><th>RX / TX</th><th>Статус</th><th></th></tr>
    {rows}
    </table>
    </div>
    </div>"""
    return render(html)


@app.route("/kick/<path:pub_key>")
@auth_required
def kick(pub_key):
    kick_peer(pub_key)
    return redirect(url_for("sessions_page"))


@app.route("/clients", methods=["GET", "POST"])
@auth_required
def clients_page():
    s = load_settings()
    subs = s.get("subscriptions", {})
    msg = ""

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            if name and re.match(r"^[a-zA-Z0-9_-]+$", name):
                try:
                    out = subprocess.check_output(
                        [f"{PHOBOS_DIR}/repo/server/scripts/phobos-client.sh", "add", name],
                        text=True, timeout=30, stderr=subprocess.STDOUT,
                        env={**os.environ, **_load_server_env()}
                    )
                    auto_assign_server(name)
                    fanout_router_config(name)  # push conf to all servers (tunnel pull)
                    msg = f'<div class="alert alert-ok">Client {name} created</div>'
                except subprocess.CalledProcessError as e:
                    msg = f'<div class="alert alert-error">{e.output}</div>'
            else:
                msg = '<div class="alert alert-error">Имя: буквы, цифры, _ и -</div>'

        elif action == "delete":
            client_id = request.form.get("client_id", "").strip()
            if client_id:
                try:
                    out = subprocess.check_output(
                        [f"{PHOBOS_DIR}/repo/server/scripts/phobos-client.sh", "remove", client_id],
                        text=True, timeout=15, stderr=subprocess.STDOUT,
                        env={**os.environ, **_load_server_env()}
                    )
                    msg = f'<div class="alert alert-ok">Клиент {client_id} удалён</div>'
                except subprocess.CalledProcessError as e:
                    msg = f'<div class="alert alert-error">{e.output}</div>'

        elif action == "set_expiry":
            client_id = request.form.get("client_id", "").strip()
            expiry = request.form.get("expiry", "").strip()
            if client_id:
                if client_id not in subs:
                    subs[client_id] = {}
                subs[client_id]["expiry"] = expiry
                subs[client_id].pop("locked", None)
                subs[client_id].pop("warn3", None)
                subs[client_id].pop("warn1", None)
                save_settings(s)
                msg = f'<div class="alert alert-ok">Срок для {client_id} обновлён</div>'

        elif action == "push_config":
            client_id = request.form.get("client_id", "").strip()
            res = push_config_to_router(client_id)
            if res["status"] == "ok":
                msg = f'<div class="alert alert-ok">{res["msg"]}</div>'
            else:
                msg = f'<div class="alert alert-ok">{client_id}: saved — router will pull config within ~2 min (SSH not needed for NAT routers)</div>'

        elif action == "assign_server":
            client_id = request.form.get("client_id", "").strip()
            server_ip = request.form.get("server_ip", "").strip()
            if client_id and server_ip:
                # 1. Find client public key + tunnel IP for WG peer
                client_meta = None
                for c in get_clients():
                    if c.get("client_id") == client_id:
                        client_meta = c
                        break
                errors = []
                # 2. Add WG peer on target server
                if client_meta:
                    pub = client_meta.get("public_key", "")
                    tip = client_meta.get("tunnel_ip_v4", "").split("/")[0]
                    if pub and tip:
                        allowed = f"{tip}/32"
                        peer_res = add_peer_to_server(server_ip, pub, allowed)
                        if peer_res.get("status") != "ok":
                            errors.append(f"Peer add: {peer_res.get('msg', 'fail')}")
                # 3. Save assignment
                ca = s.get("client_assignments", {})
                ca[client_id] = server_ip
                s["client_assignments"] = ca
                save_settings(s)
                fanout_router_config(client_id)  # push conf to all servers (tunnel pull)
                # 4. Router applies via pull (~15s). No synchronous SSH push:
                #    NAT routers can't be reached, and the SSH attempt blocked
                #    the click for seconds. Pull channel handles the apply.
                if errors:
                    msg = f'<div class="alert alert-error">{client_id} → {server_ip}: {"; ".join(errors)}</div>'
                else:
                    msg = f'<div class="alert alert-ok">{client_id} → {server_ip} ✓ (saved — router applies within ~15s)</div>'

    clients = get_clients()
    peers = get_wg_peers()
    online_pubs = set()
    for pub, info in peers.items():
        if is_peer_online(info.get("latest handshake", "")):
            online_pubs.add(pub)

    all_srv = get_all_servers_ordered()
    assignments = s.get("client_assignments", {})
    # Online = fresh handshake on ANY server (truthful during failover/transition)
    online_all = set(online_pubs)
    for _sv in all_srv:
        if _sv.get("ip") == SERVER_IP:
            continue
        for _pub, _ts in remote_handshakes(_sv["ip"]).items():
            if _ts and (time.time() - _ts) < 180:
                online_all.add(_pub)

    rows = ""
    today = datetime.now().date()
    for c in clients:
        cid = c.get("client_id", "")
        pub = c.get("public_key", "")
        ip = c.get("tunnel_ip_v4", "")
        created = c.get("created_at", "")[:10]
        is_online = pub in online_all
        status = '<span class="badge badge-on">Online</span>' if is_online else '<span class="badge badge-off">Offline</span>'

        sub = subs.get(cid, {})
        expiry = sub.get("expiry", "")
        locked = sub.get("locked", False)
        expiry_badge = ""
        if expiry:
            try:
                exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
                days = (exp_date - today).days
                if locked:
                    expiry_badge = '<span class="expiry-badge badge-lock">expired</span>'
                elif days <= 1:
                    expiry_badge = f'<span class="expiry-badge badge-warn">{days}d</span>'
                elif days <= 3:
                    expiry_badge = f'<span class="expiry-badge badge-warn">{days}d</span>'
                else:
                    expiry_badge = f'<span class="expiry-badge badge-on">{days}d</span>'
            except ValueError:
                pass

        assigned_ip = assignments.get(cid, SERVER_IP)
        srv_opts = ""
        for sv in all_srv:
            sel = " selected" if sv["ip"] == assigned_ip else ""
            label = f'{sv["ip"]} (Primary)' if sv.get("is_primary") else sv["ip"]
            srv_opts += f'<option value="{sv["ip"]}"{sel}>{label}</option>'

        set_lbl = tr("Задать", "Set")
        h_set = hlp("Выбери сервер выхода для этого роутера и нажми «Задать». Роутер сам переключится за ~15 секунд. Кнопку Push нажимать НЕ нужно — конфигурация подтягивается автоматически.",
                    "Pick the exit server for this router and press Set. The router switches itself within ~15 seconds. You do NOT need a Push button — the config is pulled automatically.")
        h_exp = hlp("Дата окончания подписки клиента. После этой даты клиент автоматически блокируется. Оставь пустым — без срока.",
                    "Client subscription expiry date. After this date the client is locked automatically. Leave empty for no expiry.")
        h_del = hlp("Безвозвратно удалить клиента, его ключи и конфигурацию. Отменить нельзя.",
                    "Permanently delete the client, its keys and config. Cannot be undone.")
        del_confirm = tr("Удалить " + cid + "?", "Delete " + cid + "?")
        rows += f"""<tr>
        <td>{cid}</td>
        <td>{ip}</td>
        <td>{status}</td>
        <td>
            <form method="post" style="display:flex;gap:4px;align-items:center">
            <input type="hidden" name="action" value="assign_server">
            <input type="hidden" name="client_id" value="{cid}">
            <select name="server_ip" style="background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:3px;border-radius:4px;font-size:.8em">{srv_opts}</select>
            <button class="btn btn-primary btn-sm" type="submit" style="padding:3px 8px">{set_lbl}</button>
            {h_set}
            </form>
        </td>
        <td>
            <form method="post" style="display:flex;gap:4px;align-items:center">
            <input type="hidden" name="action" value="set_expiry">
            <input type="hidden" name="client_id" value="{cid}">
            <input type="date" name="expiry" value="{expiry}" style="width:130px;padding:3px">
            <button class="btn btn-primary btn-sm" type="submit" style="padding:3px 8px">{set_lbl}</button>
            {expiry_badge}
            {h_exp}
            </form>
        </td>
        <td style="display:flex;gap:4px;align-items:center">
            <form method="post" onsubmit="return confirm('{del_confirm}')">
            <input type="hidden" name="action" value="delete">
            <input type="hidden" name="client_id" value="{cid}">
            <button class="btn btn-danger btn-sm" type="submit">{tr("Удалить", "Del")}</button>
            </form>
            <a href="/client/{cid}/phone/android" class="btn btn-sm" style="background:#16a34a" title="Android / PhobosWG">📱</a>
            <a href="/client/{cid}/phone/ios" class="btn btn-sm" style="background:#475569" title="iPhone / iOS WireGuard">🍎</a>
            {h_del}
        </td>
        </tr>"""

    html = f"""
    <div class="container">
    {nav('clients')}
    {msg}
    <div class="card">
    <h2>{tr("VPN-клиенты", "VPN Clients")}</h2>
    <form method="post" class="form-row" style="margin-bottom:16px">
    <input type="hidden" name="action" value="add">
    <input type="text" name="name" placeholder="{tr('Имя нового клиента', 'New client name')}" pattern="[a-zA-Z0-9_-]+" required>
    <button class="btn btn-primary" type="submit">{tr("Добавить", "Add")}</button>
    {hlp("Создать нового VPN-клиента (роутер/устройство). Имя — латиница, цифры, _ и -. После создания выдай команду установки на роутер.", "Create a new VPN client (router/device). Name: letters, digits, _ and -. After creation, run the install command on the router.")}
    </form>
    <table>
    <tr><th>{tr("Клиент","Client")}</th><th>VPN IP</th><th>{tr("Статус","Status")}</th><th>{tr("Сервер","Server")}</th><th>{tr("Срок","Expiry")}</th><th>{tr("Действия","Actions")}</th></tr>
    {rows}
    </table>
    </div>
    </div>"""
    return render(html)


@app.route("/labels", methods=["GET", "POST"])
@auth_required
def labels_page():
    s = load_settings()
    msg = ""
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            ip = request.form.get("ip", "").strip()
            label = request.form.get("label", "").strip()
            if ip and label:
                s["labels"][ip] = label
                save_settings(s)
                msg = f'<div class="alert alert-ok">Метка добавлена: {ip} → {label}</div>'
        elif action == "delete":
            ip = request.form.get("ip", "").strip()
            s["labels"].pop(ip, None)
            save_settings(s)
            msg = f'<div class="alert alert-ok">Метка удалена</div>'

    labels = s.get("labels", {})
    rows = ""
    for ip, label in sorted(labels.items()):
        rows += f"""<tr>
        <td>{ip}</td><td>{label}</td>
        <td><form method="post" style="display:inline">
        <input type="hidden" name="action" value="delete">
        <input type="hidden" name="ip" value="{ip}">
        <button class="btn btn-danger btn-sm" type="submit" onclick="return confirm('Удалить метку?')">Удалить</button>
        </form></td></tr>"""

    html = f"""
    <div class="container">
    {nav('labels')}
    {msg}
    <div class="card">
    <h2>{tr("Метки (по IP)","Labels (by IP)")} {hlp("Человекочитаемые имена для IP/туннелей — показываются в Сессиях вместо голого адреса.","Human-readable names for IPs/tunnels — shown in Sessions instead of the raw address.")}</h2>
    <p style="color:#94a3b8;font-size:.85em;margin-bottom:12px">Привяжите понятное имя (квартира, офис, дача) к внешнему IP адресу роутера. Метка отображается в таблице сессий и в Telegram уведомлениях вместо голого IP.</p>
    <form method="post" class="form-row" style="margin-bottom:16px">
    <input type="hidden" name="action" value="add">
    <input type="text" name="ip" placeholder="Real IP" required>
    <input type="text" name="label" placeholder="{tr('Имя объекта','Object name')}" required>
    <button class="btn btn-primary" type="submit">{tr("Добавить","Add")}</button>
    </form>
    <table>
    <tr><th>Real IP</th><th>Метка</th><th></th></tr>
    {rows}
    </table>
    </div>
    </div>"""
    return render(html)


@app.route("/settings", methods=["GET", "POST"])
@auth_required
def settings_page():
    s = load_settings()
    msg = ""
    if request.method == "POST" and request.form.get("action") in ("update", "rollback"):
        if request.form.get("action") == "update":
            _out = run_update(request.form.get("target", "").strip() or "main")
        else:
            _out = run_update("--rollback")
        s = load_settings()
        return render(f'''<div class="container">{nav("settings")}<div class="card">
        <h2>{tr("Результат", "Result")}</h2>
        <pre style="white-space:pre-wrap;font-size:.8em;background:#0f172a;padding:12px;border-radius:8px">{_out}</pre>
        <a href="/settings" class="btn btn-sm">{tr("← к настройкам", "← back to settings")}</a></div></div>''')
    if request.method == "POST":
        new_pass = request.form.get("admin_pass", "").strip()
        if new_pass:
            s["admin_pass"] = new_pass
        s["tg_bot_token"] = request.form.get("tg_bot_token", "").strip()
        s["tg_chat_id"] = request.form.get("tg_chat_id", "").strip()
        try:
            s["monitor_interval"] = max(10, int(request.form.get("monitor_interval", 30)))
        except ValueError:
            pass
        save_settings(s)
        msg = '<div class="alert alert-ok">Настройки сохранены</div>'

    html = f"""
    <div class="container">
    {nav('settings')}
    {msg}
    <div class="card">
    <h2>{tr("Настройки панели","Panel settings")}</h2>
    <form method="post">
    <div class="form-row"><label>{tr("Пароль панели","Panel password")}</label><input type="password" name="admin_pass" placeholder="{tr('Оставьте пустым','Leave empty')}">{hlp("Новый пароль для входа в эту панель. Оставь пустым — пароль не изменится.","New password to sign in to this panel. Leave empty to keep current.")}</div>
    <div class="form-row"><label>Telegram Token</label><input type="text" name="tg_bot_token" value="{s.get('tg_bot_token','')}">{hlp("Токен Telegram-бота для оповещений (падение/восстановление серверов, подключения, истечение подписок).","Telegram bot token for alerts (server up/down, connections, subscription expiry).")}</div>
    <div class="form-row"><label>Telegram Chat ID</label><input type="text" name="tg_chat_id" value="{s.get('tg_chat_id','')}"></div>
    <div class="form-row"><label>{tr("Интервал (сек)","Interval (s)")}</label><input type="number" name="monitor_interval" value="{s.get('monitor_interval',30)}" min="10">{hlp("Как часто фоновый монитор опрашивает серверы и обновляет статусы/оповещения. Меньше = свежее, но больше нагрузка.","How often the background monitor polls servers and refreshes statuses/alerts. Lower = fresher but more load.")}</div>
    <div class="form-row"><label></label><button class="btn btn-primary" type="submit">{tr("Сохранить","Save")}</button></div>
    </form>
    </div>

    <div class="card">
    <h2>{tr("Версия и обновления","Version & updates")} {hlp("Скачивает свежие файлы PCA-слоя (панель + скрипты) с GitHub и перезапускает панель. Ключи WireGuard, server.env и клиенты НЕ трогаются. Пусто или main = последняя версия; можно указать тег (например v1.1.0). Откат восстанавливает предыдущую версию из автоматического бэкапа.", "Fetches the latest PCA files (panel + scripts) from GitHub and restarts the panel. WireGuard keys, server.env and clients are NOT touched. Empty or main = latest; you can pass a tag (e.g. v1.1.0). Rollback restores the previous version from an automatic backup.")}</h2>
    <p>{tr("Установленная версия","Installed version")}: <b style="color:#a5b4fc">{panel_version()}</b></p>
    <form method="post" class="form-row">
    <input type="hidden" name="action" value="update">
    <input type="text" name="target" placeholder="main / v1.1.0" style="width:150px">
    <button class="btn btn-primary" type="submit" onclick="return confirm('{tr("Обновить панель и скрипты?","Update panel and scripts?")}')">{tr("Обновить","Update")}</button>
    </form>
    <form method="post" style="margin-top:8px">
    <input type="hidden" name="action" value="rollback">
    <button class="btn btn-danger btn-sm" type="submit" onclick="return confirm('{tr("Откатить на предыдущую версию?","Roll back to the previous version?")}')">{tr("Откатить на предыдущую","Rollback")}</button>
    </form>
    </div>

    <div class="card">
    <h2>{tr("Информация о сервере","Server info")}</h2>
    <table>
    <tr><td>VPS IP</td><td>{SERVER_IP}</td></tr>
    <tr><td>{tr("WireGuard порт","WireGuard port")}</td><td>51820 (localhost)</td></tr>
    <tr><td>{tr("Порты обфускатора","Obfuscator ports")}</td><td>{_load_server_env().get('OBFUSCATOR_PORTS', '2083,5443,993')}</td></tr>
    <tr><td>{tr("Порт панели","Panel port")}</td><td>8443</td></tr>
    <tr><td>{tr("Клиенты Phobos","Phobos clients")}</td><td>{CLIENTS_DIR}</td></tr>
    </table>
    </div>

    <div class="card">
    <h2>{tr("Установка на роутер","Router install")}</h2>
    <p style="color:#94a3b8;font-size:.85em;margin-bottom:8px">{tr("Keenetic/Netcraze с Entware — выполнить по SSH на роутере:","Keenetic/Netcraze with Entware — run over SSH on the router:")}</p>
    {_render_install_commands()}
    </div>
    </div>"""
    return render(html)


TOKENS_FILE = f"{PHOBOS_DIR}/tokens/tokens.json"


def _render_install_commands():
    """Read tokens and render install commands for each client."""
    try:
        if os.path.exists(TOKENS_FILE):
            with open(TOKENS_FILE) as f:
                tokens = json.load(f)
        else:
            tokens = []
    except Exception:
        tokens = []

    if not tokens:
        return '<p style="color:#64748b;font-size:.85em">Нет активных токенов. Добавьте клиента.</p>'

    lines = ""
    for t in tokens:
        client = t.get("client", "?")
        token = t.get("token", "")
        expires = t.get("expires", 0)
        exp_str = datetime.fromtimestamp(expires).strftime("%Y-%m-%d %H:%M") if expires else "?"
        cmd = f"wget -O - http://{SERVER_IP}/init/{token}.sh | sh"
        lines += f"""
        <div style="margin-bottom:10px">
        <span style="color:#a5b4fc;font-size:.85em">{client}</span>
        <span style="color:#64748b;font-size:.75em"> (до {exp_str})</span>
        <code style="background:#0f172a;padding:8px 12px;border-radius:6px;display:block;font-size:.82em;word-break:break-all;margin-top:4px">{cmd}</code>
        </div>"""
    return lines


SERVERS_FILE = f"{PANEL_DIR}/servers.json"


def load_servers():
    if os.path.exists(SERVERS_FILE):
        with open(SERVERS_FILE) as f:
            return json.load(f)
    return []


def save_servers(servers):
    with open(SERVERS_FILE, "w") as f:
        json.dump(servers, f, indent=2)


def get_main_server_info():
    env = _load_server_env()
    return {
        "ip": SERVER_IP,
        "ports": env.get("OBFUSCATOR_PORTS", "2083,5443,993"),
        "obfuscator_key": env.get("OBFUSCATOR_KEY", ""),
        "is_primary": True
    }


def get_all_servers_ordered():
    s = load_settings()
    servers = load_servers()
    main_info = get_main_server_info()
    order = s.get("server_order", [])

    by_ip = {}
    by_ip[main_info["ip"]] = {"ip": main_info["ip"], "ports": main_info["ports"],
                               "obfuscator_key": main_info["obfuscator_key"], "is_primary": True}
    for srv in servers:
        by_ip[srv["ip"]] = {**srv, "is_primary": False}

    if not order or main_info["ip"] not in order:
        order = [main_info["ip"]] + [sv["ip"] for sv in servers]

    result = []
    for ip in order:
        if ip in by_ip:
            result.append(by_ip[ip])
    for ip, sv in by_ip.items():
        if ip not in order:
            result.append(sv)
    return result


def check_server_health(server):
    """Check secondary server health via API."""
    try:
        import urllib.request
        url = f"http://{server['ip']}:8444/api/health"
        req = urllib.request.Request(url, headers={"X-API-Key": server.get("api_key", "")})
        resp = urllib.request.urlopen(req, timeout=5)
        return json.loads(resp.read())
    except Exception as e:
        return {"status": "error", "error": str(e)}


def sync_peer_to_server(server, public_key, allowed_ips, action="add"):
    """Add or remove peer on secondary server."""
    try:
        import urllib.request
        url = f"http://{server['ip']}:8444/api/peers/{action}"
        data = json.dumps({"public_key": public_key, "allowed_ips": allowed_ips}).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "X-API-Key": server.get("api_key", "")
        })
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception:
        return {"status": "error"}


def sync_peer_to_all_servers(public_key, allowed_ips, action="add"):
    """Sync peer to all secondary servers."""
    servers = load_servers()
    for srv in servers:
        if srv.get("enabled", True):
            sync_peer_to_server(srv, public_key, allowed_ips, action)


@app.route("/client/<cid>/phone/<mode>")
@auth_required
def client_phone(cid, mode):
    if mode not in ("android", "ios"):
        mode = "android"
    conf, link = build_phone_config(cid, mode)
    if conf is None:
        return ("client not found", 404)
    qr = qr_datauri(link if (mode == "android" and link) else conf)
    if mode == "android":
        title = tr("Android — PhobosWG (с обфускацией)", "Android — PhobosWG (obfuscated)")
        apk_btn = ""
        if os.path.exists("/opt/Phobos/www/app/PhobosWG.apk"):
            apk_btn = (f'<p style="margin-top:6px"><a href="http://{SERVER_IP}/app/PhobosWG.apk" '
                       f'class="btn btn-primary" style="background:#16a34a">📥 {tr("Скачать приложение PhobosWG (APK)","Download PhobosWG app (APK)")}</a></p>')
        extra = (apk_btn +
                 f'<p>{tr("phobos:// ссылка — импорт в PhobosWG:", "phobos:// link — import into PhobosWG:")}</p>'
                 f'<textarea readonly onclick="this.select()" style="width:100%;height:90px;font-size:.75em">{link}</textarea>')
    else:
        title = tr("iPhone / iOS WireGuard (без обфускации)", "iPhone / iOS WireGuard (plain, no obfuscation)")
        extra = (f'<p><a href="https://apps.apple.com/app/wireguard/id1441195209" target="_blank" rel="noopener" class="btn btn-primary" style="background:#0ea5e9">📥 {tr("WireGuard в App Store","WireGuard on the App Store")}</a></p>'
                 f'<p style="color:#fcd34d">{tr("⚠ Обычный WireGuard без маскировки (Endpoint → :51820). На сервере должен быть открыт порт 51820 (ALLOW_PLAIN_WG=1).", "⚠ Plain WireGuard, no obfuscation (Endpoint → :51820). Port 51820 must be open on the server (ALLOW_PLAIN_WG=1).")}</p>')
    html = f"""
    <div class="container">
    {nav('clients')}
    <div class="card" style="text-align:center">
    <h2>{cid} — {title}</h2>
    <img src="{qr}" alt="QR" style="width:300px;height:300px;background:#fff;padding:10px;border-radius:10px"><br>
    <p style="margin-top:10px">{tr("Отсканируй QR в приложении или импортируй конфиг:", "Scan the QR in the app or import the config:")}</p>
    {extra}
    <textarea readonly onclick="this.select()" style="width:100%;height:250px;font-family:monospace;font-size:.78em">{conf}</textarea>
    <p style="margin-top:10px"><a href="/clients" class="btn btn-sm">{tr("← назад", "← back")}</a></p>
    </div></div>"""
    return render(html)


@app.route("/api/router-config/<client_id>")
def router_config(client_id):
    """NAT-friendly config pull: router fetches its own failover.conf.
    Auth via per-client pull_token (falls back to server_api_key)."""
    st = load_settings()
    token = request.args.get("token", "")
    acc = st.get("router_access", {}).get(client_id, {})
    expected = acc.get("pull_token", "") or st.get("server_api_key", "")
    if not expected or token != expected:
        return ("forbidden", 403)
    conf = generate_failover_conf_for_client(client_id)
    return (conf, 200, {"Content-Type": "text/plain; charset=utf-8"})


@app.route("/api/servers/register", methods=["POST"])
def api_register_server():
    s = load_settings()
    api_key = request.headers.get("X-API-Key", "")
    if api_key != s.get("server_api_key", ""):
        return json.dumps({"error": "unauthorized"}), 401, {"Content-Type": "application/json"}

    data = request.json
    servers = load_servers()
    ip = data.get("ip", "")

    for srv in servers:
        if srv["ip"] == ip:
            srv.update(data)
            srv["last_seen"] = datetime.now().isoformat()
            save_servers(servers)
            return json.dumps({"status": "updated"}), 200, {"Content-Type": "application/json"}

    data["last_seen"] = datetime.now().isoformat()
    data["enabled"] = True
    data["api_key"] = api_key
    servers.append(data)
    save_servers(servers)
    return json.dumps({"status": "registered"}), 200, {"Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Client provisioning API — used by Keenetic Unified (KU) to auto-create a
# unique Phobos client per router (name "ku-<router_id>") and return its
# install command. Each call guarantees a unique WG keypair + tunnel IP +
# peer on ALL servers, so per-router configs never collide.
# ---------------------------------------------------------------------------

CLIENT_SCRIPT = f"{PHOBOS_DIR}/repo/server/scripts/phobos-client.sh"


def _normalize_client_id(name):
    return name.strip().lower().replace(" ", "-")


def _latest_token_for_client(cid):
    try:
        with open(TOKENS_FILE) as f:
            toks = json.load(f)
    except Exception:
        return ""
    for t in reversed(toks):
        if t.get("client") == cid:
            return t.get("token", "")
    return ""


@app.route("/api/client/ensure", methods=["POST"])
def api_client_ensure():
    s = load_settings()
    if request.headers.get("X-API-Key", "") != s.get("server_api_key", ""):
        return json.dumps({"error": "unauthorized"}), 401, {"Content-Type": "application/json"}
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name or not re.match(r"^[a-zA-Z0-9_-]+$", name):
        return json.dumps({"error": "bad_name"}), 400, {"Content-Type": "application/json"}
    cid = _normalize_client_id(name)
    env = {**os.environ, **_load_server_env()}
    exists = Path(f"{CLIENTS_DIR}/{cid}").is_dir()
    sub = "link" if exists else "add"
    try:
        out = subprocess.check_output([CLIENT_SCRIPT, sub, name], text=True,
                                      timeout=60, stderr=subprocess.STDOUT, env=env)
    except subprocess.CalledProcessError as e:
        return json.dumps({"error": "script_failed", "output": (e.output or "")[-1500:]}), 500, {"Content-Type": "application/json"}

    meta = next((c for c in get_clients() if c.get("client_id") == cid), None)
    if not meta:
        return json.dumps({"error": "no_meta", "output": out[-800:]}), 500, {"Content-Type": "application/json"}
    pub = meta.get("public_key", "")
    tip = (meta.get("tunnel_ip_v4") or "").split("/")[0]

    # Peer on ALL servers so failover works on every server, not just primary.
    if pub and tip:
        try:
            sync_peer_to_all_servers(pub, f"{tip}/32", "add")
        except Exception:
            pass
    assigned = auto_assign_server(cid)

    # Per-client pull token for the NAT-friendly config pull channel.
    s = load_settings()
    ra = s.get("router_access", {})
    acc = ra.get(cid) or {}
    if not acc.get("pull_token"):
        acc["pull_token"] = secrets.token_hex(16)
        ra[cid] = acc
        s["router_access"] = ra
        save_settings(s)
    pull_token = ra[cid]["pull_token"]

    token = _latest_token_for_client(cid)
    # phobos-client.sh writes these 0600 (umask 077 in action_add) → nginx (www-data)
    # can't read them → 403 on /init and /packages. Make them world-readable.
    if token:
        _www = f"{PHOBOS_DIR}/www"
        for pth in (f"{_www}/init/{token}.sh",
                    f"{_www}/packages/{token}/phobos-{cid}.tar.gz"):
            try:
                os.chmod(pth, 0o644)
            except Exception:
                pass
        try:
            os.chmod(f"{_www}/packages/{token}", 0o755)
        except Exception:
            pass
    install_url = f"http://{SERVER_IP}/init/{token}.sh" if token else ""
    return json.dumps({
        "ok": True, "client_id": cid, "install_url": install_url, "token": token,
        "pull_token": pull_token, "tunnel_ip": tip, "assigned_server": assigned,
    }), 200, {"Content-Type": "application/json"}


@app.route("/api/client/remove", methods=["POST"])
def api_client_remove():
    s = load_settings()
    if request.headers.get("X-API-Key", "") != s.get("server_api_key", ""):
        return json.dumps({"error": "unauthorized"}), 401, {"Content-Type": "application/json"}
    data = request.json or {}
    cid = _normalize_client_id((data.get("name") or "").strip())
    if not cid:
        return json.dumps({"error": "bad_name"}), 400, {"Content-Type": "application/json"}
    meta = next((c for c in get_clients() if c.get("client_id") == cid), None)
    pub = (meta or {}).get("public_key", "")
    tip = ((meta or {}).get("tunnel_ip_v4") or "").split("/")[0]
    env = {**os.environ, **_load_server_env()}
    try:
        subprocess.check_output([CLIENT_SCRIPT, "remove", cid], text=True,
                                timeout=30, stderr=subprocess.STDOUT, env=env)
    except subprocess.CalledProcessError as e:
        return json.dumps({"error": "script_failed", "output": (e.output or "")[-1500:]}), 500, {"Content-Type": "application/json"}
    if pub and tip:
        try:
            sync_peer_to_all_servers(pub, f"{tip}/32", "remove")
        except Exception:
            pass
    s = load_settings()
    for key in ("router_access", "client_assignments"):
        d = s.get(key, {})
        if cid in d:
            del d[cid]
            s[key] = d
    save_settings(s)
    return json.dumps({"ok": True, "client_id": cid}), 200, {"Content-Type": "application/json"}


@app.route("/api/obf-health")
def api_obf_health():
    """Liveness of the obfuscator path on THIS (primary) server.
    Returns 200 only if all wg-obfuscator-* services are active, else 503.
    Router check_primary uses this as a valid switchback signal (the panel
    HTTP port stays up even when the tunnel path is dead, so it cannot be
    used directly)."""
    try:
        listing = subprocess.check_output(
            ["systemctl", "list-units", "--type=service", "--no-legend",
             "wg-obfuscator-*"], text=True, timeout=5)
        names = [ln.split()[0] for ln in listing.splitlines() if ln.strip()]
    except Exception:
        names = []
    if not names:
        names = ["wg-obfuscator-2083.service",
                 "wg-obfuscator-5443.service",
                 "wg-obfuscator-993.service"]
    bad = []
    for n in names:
        st = subprocess.getoutput("systemctl is-active " + n).strip()
        if st != "active":
            bad.append(n + "=" + st)
    if bad:
        return "obf-down: " + ",".join(bad), 503, {"Content-Type": "text/plain"}
    return "obf-ok " + str(len(names)), 200, {"Content-Type": "text/plain"}


@app.route("/servers", methods=["GET", "POST"])
@auth_required
def servers_page():
    s = load_settings()
    servers = load_servers()
    msg = ""

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            ip = request.form.get("ip", "").strip()
            api_key = request.form.get("api_key", "").strip()
            ssh_user = request.form.get("ssh_user", "root").strip() or "root"
            ssh_pass = request.form.get("ssh_pass", "").strip()
            if ip:
                servers.append({
                    "ip": ip, "api_key": api_key, "enabled": True,
                    "last_seen": "", "wg_public_key": "", "obfuscator_key": "", "ports": "",
                    "ssh_user": ssh_user, "ssh_pass": ssh_pass
                })
                save_servers(servers)
                order = s.get("server_order", [])
                if ip not in order:
                    order.append(ip)
                    s["server_order"] = order
                    save_settings(s)
                msg = f'<div class="alert alert-ok">Сервер {ip} добавлен</div>'

        elif action == "remove":
            ip = request.form.get("ip", "").strip()
            servers = [sv for sv in servers if sv.get("ip") != ip]
            save_servers(servers)
            order = s.get("server_order", [])
            if ip in order:
                order.remove(ip)
                s["server_order"] = order
                save_settings(s)
            msg = f'<div class="alert alert-ok">Сервер удалён</div>'

        elif action == "sync":
            ip = request.form.get("ip", "").strip()
            srv = next((sv for sv in servers if sv["ip"] == ip), None)
            if srv:
                clients = get_clients()
                synced = 0
                for c in clients:
                    pub = c.get("public_key", "")
                    tip = c.get("tunnel_ip_v4", "")
                    if pub and tip:
                        res = sync_peer_to_server(srv, pub, f"{tip}/32", "add")
                        if res.get("status") == "ok":
                            synced += 1
                msg = f'<div class="alert alert-ok">Синхронизировано {synced} клиентов на {ip}</div>'

        elif action == "fetch_info":
            ip = request.form.get("ip", "").strip()
            for srv in servers:
                if srv["ip"] == ip:
                    try:
                        import urllib.request as ul
                        url = f"http://{ip}:8444/api/info"
                        rq = ul.Request(url, headers={"X-API-Key": srv.get("api_key", "")})
                        resp = ul.urlopen(rq, timeout=5)
                        info = json.loads(resp.read())
                        srv["wg_public_key"] = info.get("wg_public_key", "")
                        srv["obfuscator_key"] = info.get("obfuscator_key", "")
                        srv["ports"] = ",".join(info.get("ports", []))
                        save_servers(servers)
                        msg = f'<div class="alert alert-ok">Инфо получено от {ip}</div>'
                    except Exception as e:
                        msg = f'<div class="alert alert-error">Ошибка: {e}</div>'

        elif action == "set_api_key":
            new_key = request.form.get("server_api_key", "").strip()
            if new_key:
                s["server_api_key"] = new_key
                save_settings(s)
                msg = '<div class="alert alert-ok">API ключ обновлён</div>'

        elif action in ("move_up", "move_down"):
            ip = request.form.get("ip", "").strip()
            order = s.get("server_order", [])
            if not order or SERVER_IP not in order:
                order = [SERVER_IP] + [sv["ip"] for sv in servers]
            if ip in order:
                idx = order.index(ip)
                if action == "move_up" and idx > 0:
                    order[idx], order[idx-1] = order[idx-1], order[idx]
                elif action == "move_down" and idx < len(order) - 1:
                    order[idx], order[idx+1] = order[idx+1], order[idx]
            s["server_order"] = order
            save_settings(s)

        elif action == "save_router_access":
            clients = get_clients()
            ra = s.get("router_access", {})
            for c in clients:
                cid = c.get("client_id", "")
                ssh_ip = request.form.get(f"ssh_ip_{cid}", "").strip()
                ssh_static = request.form.get(f"ssh_static_{cid}", "").strip()
                ssh_user = request.form.get(f"ssh_user_{cid}", "root").strip() or "root"
                ssh_pass = request.form.get(f"ssh_pass_{cid}", "").strip()
                if ssh_ip or ssh_pass:
                    ra[cid] = {"ssh_ip": ssh_ip, "ssh_static": ssh_static, "ssh_user": ssh_user, "ssh_pass": ssh_pass, "ssh_ok": ra.get(cid, {}).get("ssh_ok", False)}
                elif cid in ra and not ssh_ip and not ssh_pass:
                    del ra[cid]
            s["router_access"] = ra
            save_settings(s)
            msg = '<div class="alert alert-ok">SSH доступ сохранён</div>'

        elif action == "test_ssh":
            cid = request.form.get("client_id", "").strip()
            ra = s.get("router_access", {})
            acc = ra.get(cid, {})
            results = []
            ok_ip = ""
            # Try tunnel IP first, then static
            for label, ip in [("tunnel", acc.get("ssh_ip", "")), ("static", acc.get("ssh_static", ""))]:
                if not ip:
                    continue
                try:
                    r = subprocess.run(
                        ["sshpass", "-p", acc.get("ssh_pass", ""), "ssh",
                         "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                         f"{acc.get('ssh_user','root')}@{ip}", "echo OK"],
                        capture_output=True, text=True, timeout=10
                    )
                    if "OK" in r.stdout:
                        results.append(f"{label} ({ip}): ✓")
                        if not ok_ip:
                            ok_ip = ip
                    else:
                        results.append(f"{label} ({ip}): ✗ {r.stderr[:80]}")
                except Exception as e:
                    results.append(f"{label} ({ip}): ✗ {str(e)[:80]}")
            if cid in ra:
                ra[cid]["ssh_ok"] = bool(ok_ip)
                ra[cid]["ssh_tested"] = ok_ip or ""
                s["router_access"] = ra
                save_settings(s)
            # SSH is optional under the pull model. Frame the result around whether
            # the router is actually reachable/managed, not raw SSH success.
            client_pub = ""
            for _c in get_clients():
                if _c.get("client_id") == cid:
                    client_pub = _c.get("public_key", "")
                    break
            online = False
            for _p, _i in get_wg_peers().items():
                if _p == client_pub and is_peer_online(_i.get("latest handshake", "")):
                    online = True
            for _ip, _hs in server_handshakes_cache.items():
                _ts = _hs.get(client_pub, 0)
                if _ts and (time.time() - _ts) < 180:
                    online = True
            ssh_line = " | ".join(results) if results else tr("SSH не настроен", "SSH not configured")
            if ok_ip:
                msg = f'<div class="alert alert-ok">{cid}: SSH ✓ {ok_ip} — {ssh_line}</div>'
            elif online:
                msg = f'<div class="alert alert-ok">{cid}: {tr("роутер ОНЛАЙН, управляется через pull. SSH по туннелю недоступен — это нормально для роутера за NAT (входящий SSH закрыт); управление SSH не требует.", "router is ONLINE, managed via pull. Tunnel SSH unreachable — normal for a NAT router (inbound SSH closed); management does not need SSH.")}</div>'
            else:
                msg = f'<div class="alert alert-error">{cid}: {tr("роутер НЕ виден — нет свежего handshake ни на одном сервере, и SSH недоступен.", "router NOT visible — no fresh handshake on any server, and SSH unreachable.")} {ssh_line}</div>'

        elif action == "push_all":
            clients = get_clients()
            results = []
            for c in clients:
                cid = c.get("client_id", "")
                res = push_config_to_router(cid)
                results.append(f"{cid}: {res['msg']}")
            msg = '<div class="alert alert-ok">' + '<br>'.join(results) + '</div>'

        elif action == "save_server_access":
            for srv in servers:
                ip = srv["ip"]
                su = request.form.get(f"srv_ssh_user_{ip}", "").strip()
                sp = request.form.get(f"srv_ssh_pass_{ip}", "").strip()
                if su:
                    srv["ssh_user"] = su
                if sp:
                    srv["ssh_pass"] = sp
            save_servers(servers)
            msg = '<div class="alert alert-ok">SSH доступ к серверам сохранён</div>'

        elif action == "rebalance":
            clients = get_clients()
            all_srv = get_all_servers_ordered()
            server_ips = [sv["ip"] for sv in all_srv]
            # Load-aware greedy: base load from live CPU+RAM (cached by monitor),
            # then each client goes to the least-loaded server, with a per-client
            # penalty so load spreads evenly. Down servers excluded (unless all down).
            base_load = {ip: server_load(server_stats_cache.get(ip, {})) for ip in server_ips}
            usable = [ip for ip in server_ips if base_load[ip] < 0.99] or server_ips
            placed = {ip: 0 for ip in server_ips}
            PEN = 0.08  # extra load each assigned client adds to a server's score
            assignments = {}
            rb_err = []
            for c in clients:
                cid = c["client_id"]
                target = min(usable, key=lambda ip: base_load[ip] + placed[ip] * PEN)
                placed[target] += 1
                assignments[cid] = target
                # Provision the WG peer on the target server. Round-robin assignment
                # alone is not enough — without the peer the handshake fails there and
                # the router's health monitor just fails back.
                pub = c.get("public_key", "")
                tip = c.get("tunnel_ip_v4", "").split("/")[0]
                if pub and tip:
                    r = add_peer_to_server(target, pub, f"{tip}/32")
                    if r.get("status") != "ok":
                        rb_err.append(f"{cid}->{target}: {r.get('msg', 'peer fail')}")
            s["client_assignments"] = assignments
            save_settings(s)
            for _cid in assignments:
                fanout_router_config(_cid)  # push conf to all servers (tunnel pull)
            if rb_err:
                msg = f'<div class="alert alert-error">Rebalance: {"; ".join(rb_err)}</div>'
            else:
                msg = f'<div class="alert alert-ok">{tr("Перераспределено", "Rebalanced")}: {len(clients)} -> {len(server_ips)} {tr("серв.; пиры добавлены, роутеры применят за ~15с", "servers; peers added, routers apply within ~15s")}</div>'

    # Build ordered server list
    all_ordered = get_all_servers_ordered()
    assignments = s.get("client_assignments", {})
    clients = get_clients()

    # Count assigned clients per server
    assign_counts = {}
    for cid, sip in assignments.items():
        assign_counts[sip] = assign_counts.get(sip, 0) + 1

    # Stats dashboard cards
    stats_cards = ""
    for srv in all_ordered:
        ip = srv.get("ip", "")
        is_primary = srv.get("is_primary", False)
        cached = server_stats_cache.get(ip, {})
        ports = srv.get("ports", "?")
        assigned = assign_counts.get(ip, 0)

        if is_primary:
            local = get_local_stats()
            cpu_val = local.get("cpu", "?")
            mem_val = local.get("mem", "?")
            peers_val = local.get("peers", 0)
            st_class = "badge-on"
            st_text = "Primary"
        else:
            cpu_val = cached.get("cpu", "—")
            mem_val = cached.get("mem", "—")
            peers_val = cached.get("peers", "?")
            is_up = cached.get("status", "down") == "ok"
            st_class = "badge-on" if is_up else "badge-off"
            st_text = "Online" if is_up else "Offline"

        stats_cards += f"""
        <div style="background:#0f172a;border:1px solid #334155;border-radius:8px;padding:14px;min-width:200px;flex:1">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <b style="color:#a5b4fc">{ip}</b>
            <span class="badge {st_class}">{st_text}</span>
          </div>
          <div style="font-size:.82em;color:#94a3b8;line-height:1.8">
            CPU: <b style="color:#e2e8f0">{cpu_val}</b> &nbsp; RAM: <b style="color:#e2e8f0">{mem_val}</b><br>
            Peers: <b style="color:#e2e8f0">{peers_val}</b> &nbsp; Assigned: <b style="color:#e2e8f0">{assigned}</b> &nbsp; Ports: {ports}
          </div>
        </div>"""

    # Server priority rows
    rows = ""
    for idx, srv in enumerate(all_ordered):
        ip = srv.get("ip", "")
        is_primary = srv.get("is_primary", False)
        ports = srv.get("ports", "?")
        cached = server_stats_cache.get(ip, {})
        peers_val = cached.get("peers", get_local_stats().get("peers", "?") if is_primary else "?")
        assigned = assign_counts.get(ip, 0)

        if is_primary:
            st_class, st_text = "badge-on", "Primary"
        else:
            is_up = cached.get("status", "down") == "ok"
            st_class = "badge-on" if is_up else "badge-off"
            st_text = "Online" if is_up else "Offline"

        prio = f'<span style="color:#6366f1;font-weight:600;margin-right:6px">#{idx+1}</span>'
        ptag = ' <span class="badge" style="background:#312e81;color:#a5b4fc">Primary</span>' if is_primary else ""

        mv = f"""<button name="action" value="move_up" class="btn btn-sm" style="background:#334155;padding:2px 8px" {"disabled" if idx==0 else ""}>▲</button>
            <button name="action" value="move_down" class="btn btn-sm" style="background:#334155;padding:2px 8px" {"disabled" if idx==len(all_ordered)-1 else ""}>▼</button>"""

        acts = ""
        if not is_primary:
            acts = f"""<button name="action" value="sync" class="btn btn-primary btn-sm">{tr("Синхр.","Sync")}</button>
            <button name="action" value="fetch_info" class="btn btn-primary btn-sm">{tr("Инфо","Info")}</button>
            <button name="action" value="remove" class="btn btn-danger btn-sm" onclick="return confirm('{tr("Удалить сервер?","Remove server?")}')">{tr("Удалить","Del")}</button>"""

        rows += f"""<tr>
        <td>{prio}{ip}{ptag}</td><td>{ports}</td><td>{peers_val}</td><td>{assigned}</td>
        <td><span class="badge {st_class}">{st_text}</span></td>
        <td><form method="post" style="display:inline"><input type="hidden" name="ip" value="{ip}">{mv} {acts}</form></td>
        </tr>"""

    api_key = s.get("server_api_key", "")

    # Router access rows — tunnel IP + optional static IP
    router_access = s.get("router_access", {})
    access_rows = ""
    for c in clients:
        cid = c.get("client_id", "")
        tunnel_ip = c.get("tunnel_ip_v4", "")
        acc = router_access.get(cid, {})
        ssh_ip = acc.get("ssh_ip", tunnel_ip)
        ssh_static = acc.get("ssh_static", "")
        ssh_user = acc.get("ssh_user", "root")
        ssh_pass = acc.get("ssh_pass", "")
        ssh_ok = acc.get("ssh_ok", False)
        ssh_tested = acc.get("ssh_tested", "")
        if ssh_ok:
            badge_cls = "badge-on"
            badge_txt = f"✓ {ssh_tested}" if ssh_tested else "✓"
        elif ssh_ip and ssh_pass:
            badge_cls = "badge-warn"
            badge_txt = "не проверен"
        else:
            badge_cls = "badge-off"
            badge_txt = "—"
        access_rows += f"""<tr>
        <td>{cid}</td>
        <td><input type="text" name="ssh_ip_{cid}" value="{ssh_ip}" placeholder="{tunnel_ip}" style="width:120px;padding:4px" title="WG tunnel IP"></td>
        <td><input type="text" name="ssh_static_{cid}" value="{ssh_static}" placeholder="static" style="width:120px;padding:4px" title="Static/public IP (optional)"></td>
        <td><input type="text" name="ssh_user_{cid}" value="{ssh_user}" placeholder="root" style="width:60px;padding:4px"></td>
        <td><input type="password" name="ssh_pass_{cid}" value="{ssh_pass}" placeholder="pass" style="width:100px;padding:4px"></td>
        <td><span class="badge {badge_cls}" style="font-size:.7em">{badge_txt}</span></td>
        </tr>"""

    html = f"""
    <div class="container">
    {nav('servers')}
    {msg}

    <div class="card">
    <h2>{tr("Панель серверов", "Server Dashboard")} {hlp("Живая статистика всех VPN-серверов. Peers = активные WG-подключения. Assigned = клиенты, у которых этот сервер основной. Фоновый монитор опрашивает каждые " + str(s.get('monitor_interval',30)) + " сек и шлёт оповещения в Telegram при падении/восстановлении сервера.", "Live stats for all VPN servers. Peers = active WG connections. Assigned = clients whose primary is this server. Background monitor polls every " + str(s.get('monitor_interval',30)) + "s and sends Telegram alerts on server up/down.")}</h2>
    <div style="display:flex;gap:12px;flex-wrap:wrap">{stats_cards}</div>
    </div>

    <div class="card">
    <h2>{tr("Приоритет серверов и балансировка", "Server Priority & Load Balancing")} {hlp("У каждого клиента есть основной сервер. При сбое роутер сам перебирает серверы сверху вниз. Стрелки меняют общий порядок резервирования. Роутеры за NAT подтягивают конфиг сами — ручных действий не нужно.", "Each client has a primary server. On failure the router tries servers top-to-bottom by itself. Arrows change the global fallback order. NAT routers pull config themselves — no manual action needed.")}</h2>
    <table>
    <tr><th>{tr("Сервер","Server")}</th><th>{tr("Порты","Ports")}</th><th>Peers</th><th>{tr("Назначено","Assigned")}</th><th>{tr("Статус","Status")}</th><th>{tr("Действия","Actions")}</th></tr>
    {rows}
    </table>
    <div style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
    <form method="post" style="display:inline"><input type="hidden" name="action" value="rebalance">
    <button class="btn btn-primary" type="submit" onclick="return confirm('{tr("Перераспределить всех клиентов по серверам?","Redistribute all clients across servers?")}')">{tr("Балансировать","Rebalance")}</button></form>
    {hlp("Равномерно распределить всех клиентов по серверам (round-robin). Меняет основной сервер у клиентов — они переключатся автоматически.", "Evenly redistribute all clients across servers (round-robin). Changes clients primary server — they switch automatically.")}
    <form method="post" style="display:inline"><input type="hidden" name="action" value="push_all">
    <button class="btn btn-primary" type="submit" onclick="return confirm('{tr("Отправить конфиг на все роутеры?","Push config to all routers?")}')">Push All</button></form>
    {hlp("Принудительно отправить конфиг на роутеры с прямым SSH. Роутеры за NAT это игнорируют и сами подтягивают конфиг за ~15 сек — обычно эта кнопка не нужна.", "Force-push config to routers reachable by SSH. NAT routers ignore it and auto-pull within ~15s — you usually do not need this.")}
    </div>
    <form method="post" class="form-row" style="margin-top:16px;flex-wrap:wrap">
    <input type="hidden" name="action" value="add">
    <input type="text" name="ip" placeholder="{tr('IP сервера','Server IP')}" required>
    <input type="text" name="api_key" placeholder="API key" style="width:160px">
    <input type="text" name="ssh_user" placeholder="root" style="width:70px" value="root">
    <input type="password" name="ssh_pass" placeholder="SSH pass" style="width:140px">
    <button class="btn btn-primary" type="submit">{tr("Добавить сервер","Add Server")}</button>
    {hlp("Добавить новый VPN-сервер в пул резервирования. Нужны: IP, API-ключ агента (порт 8444) и SSH-пароль для первичной настройки.", "Add a new VPN server to the failover pool. Needs: IP, agent API key (port 8444) and SSH password for initial setup.")}
    </form>
    </div>

    <div class="card">
    <h2>{tr("SSH-доступ к роутерам (опционально)", "Router SSH Access (optional)")} {hlp("НЕ обязательно. Роутеры сами тянут конфиг с панели по HTTPS каждые ~15 сек (работает за любым NAT, входящий SSH не нужен). Эта секция — только для роутеров с прямым IP, если хочешь мгновенный push. У роутеров за NAT/KeenDNS тут будут таймауты SSH — это нормально и безвредно, pull-канал их обновляет.", "NOT required. Routers pull config from the panel over HTTPS every ~15s (works behind any NAT, no inbound SSH). This section is only for direct-IP routers if you want an instant push. NAT/KeenDNS routers will show SSH timeouts here — that is normal and harmless; the pull channel keeps them updated.")}</h2>
    <form method="post">
    <input type="hidden" name="action" value="save_router_access">
    <table>
    <tr><th>{tr("Клиент","Client")}</th><th>Tunnel IP</th><th>{tr("Статич. IP","Static IP")}</th><th>{tr("Пользователь","User")}</th><th>{tr("Пароль","Pass")}</th><th>{tr("Статус","Status")}</th></tr>
    {access_rows}
    </table>
    <button class="btn btn-primary" type="submit" style="margin-top:10px">{tr("Сохранить SSH","Save SSH")}</button>
    {hlp("Сохранить SSH-доступ к роутерам. Нужно только для роутеров с прямым публичным IP. Для NAT-роутеров не требуется.", "Save SSH access to routers. Only needed for routers with a direct public IP. Not required for NAT routers.")}
    </form>
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
    {"".join(f'<form method="post" style="display:inline"><input type="hidden" name="action" value="test_ssh"><input type="hidden" name="client_id" value="{c.get("client_id","")}"><button class="btn btn-sm" style="background:#334155;padding:4px 12px;font-size:.8em" type="submit">{tr("Тест","Test")} {c.get("client_id","")}</button></form>' for c in clients)}
    </div>
    </div>

    <div class="card">
    <h2>{tr("Команды для роутеров","Router commands")}</h2>
    <p style="color:#94a3b8;font-size:.85em;margin-bottom:8px"><b>Установка</b> — выполнить по SSH на роутере (Keenetic/Netcraze с Entware):</p>
    {_render_install_commands()}
    <p style="color:#94a3b8;font-size:.85em;margin-top:16px;margin-bottom:8px"><b>Удаление Phobos</b> — выполнить по SSH на роутере:</p>
    <code style="background:#0f172a;padding:8px 12px;border-radius:6px;display:block;font-size:.82em;word-break:break-all">/opt/etc/Phobos/phobos-uninstall.sh</code>
    <p style="color:#64748b;font-size:.75em;margin-top:6px">Скрипт остановит obfuscator, удалит cron, конфиги и бинарник. WireGuard интерфейс на роутере нужно удалить вручную через веб-панель.</p>
    <p style="color:#94a3b8;font-size:.85em;margin-top:16px;margin-bottom:8px"><b>Фикс SSH через туннель</b> — если SSH по 10.25.0.x не работает (security-level):</p>
    <code style="background:#0f172a;padding:8px 12px;border-radius:6px;display:block;font-size:.82em;word-break:break-all">wget -O - http://{SERVER_IP}/init/fix-security.sh | sh</code>
    <p style="color:#64748b;font-size:.75em;margin-top:6px">Автоматически найдёт WG интерфейс Phobos и установит security-level private для входящих подключений (SSH). Выполнять на роутере по SSH через LAN (192.168.1.1).</p>
    </div>

    <div class="card">
    <h2>Server SSH Access</h2>
    <p style="color:#94a3b8;font-size:.85em;margin-bottom:12px">SSH credentials for secondary servers. Used by panel to add/remove WG peers when switching client servers. Main server uses local commands.</p>
    <form method="post">
    <input type="hidden" name="action" value="save_server_access">
    <table>
    <tr><th>Server</th><th>SSH User</th><th>SSH Pass</th><th></th></tr>
    {"".join(f'''<tr>
    <td>{srv.get("ip","")}</td>
    <td><input type="text" name="srv_ssh_user_{srv.get('ip','')}" value="{srv.get('ssh_user','root')}" placeholder="root" style="width:80px;padding:4px"></td>
    <td><input type="password" name="srv_ssh_pass_{srv.get('ip','')}" value="{srv.get('ssh_pass','')}" placeholder="pass" style="width:150px;padding:4px"></td>
    <td><span class="badge {'badge-on' if srv.get('ssh_pass') else 'badge-off'}">{'OK' if srv.get('ssh_pass') else '—'}</span></td>
    </tr>''' for srv in servers)}
    </table>
    <button class="btn btn-primary" type="submit" style="margin-top:10px">Save Server SSH</button>
    </form>
    </div>

    <div class="card">
    <h2>API Key</h2>
    <form method="post" class="form-row">
    <input type="hidden" name="action" value="set_api_key">
    <input type="text" name="server_api_key" value="{api_key}" placeholder="API key" style="width:300px">
    <button class="btn btn-primary" type="submit">Save</button>
    </form>
    </div>

    <div class="card">
    <h2>Deploy Secondary Server</h2>
    <p style="color:#94a3b8;font-size:.85em;margin-bottom:8px">One command deploys WG + obfuscator + mini-API on a new VPS. Auto-registers in panel. Run via SSH on new VPS:</p>
    <code style="background:#0f172a;padding:8px 12px;border-radius:6px;display:block;font-size:.82em;word-break:break-all">MAIN_SERVER={SERVER_IP} MAIN_API_KEY={api_key} bash &lt;(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/server/secondary-setup.sh)</code>
    </div>
    </div>"""
    return render(html)


def _load_server_env():
    """Load server.env as dict for subprocess env."""
    env = {}
    if os.path.exists(SERVER_ENV):
        with open(SERVER_ENV) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k] = v
    return env


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8443, debug=False)
