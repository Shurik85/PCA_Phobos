#!/usr/bin/env python3
"""
PCA Phobos — Web Panel for Phobos (Obfuscated WireGuard VPN)
Management panel: clients, sessions, labels, subscriptions, Telegram alerts.
"""

import json, os, subprocess, threading, time, secrets, hashlib, re
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, redirect, url_for, session, make_response

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
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
        for k, v in DEFAULT_SETTINGS.items():
            s.setdefault(k, v)
        return s
    return dict(DEFAULT_SETTINGS)


def save_settings(s):
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
    s = load_settings()
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


def session_monitor():
    """Background thread: monitor sessions, send Telegram alerts."""
    global prev_session_keys
    while True:
        try:
            s = load_settings()
            interval = s.get("monitor_interval", 30)

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
                    tg_send(f"🟢 <b>{client_id}</b> подключился — {name}")

                for key in prev_session_keys - current_keys:
                    client_id, real_ip = key
                    label = labels.get(real_ip, "")
                    name = f"{label} ({real_ip})" if label else real_ip
                    tg_send(f"🔴 <b>{client_id}</b> отключился — {name}")

            prev_session_keys = current_keys

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
</style>
</head>
<body>
<div class="header">
<h1>🛡️ Phobos VPN Panel</h1>
<div class="subtitle">Obfuscated WireGuard · """ + SERVER_IP + """</div>
</div>
CONTENT
<div class="footer">
Phobos VPN Panel
</div>
</body></html>"""


def render(content):
    return PAGE.replace("CONTENT", content)


@app.route("/login", methods=["GET", "POST"])
def login():
    s = load_settings()
    msg = ""
    if request.method == "POST":
        if request.form.get("password") == s["admin_pass"]:
            session["auth"] = True
            return redirect(url_for("dashboard"))
        msg = '<div class="alert alert-error">Неверный пароль</div>'
    html = f"""
    <div class="login-box">
    <h2>Вход в панель</h2>
    {msg}
    <form method="post">
    <input type="password" name="password" placeholder="Пароль" autofocus>
    <button class="btn btn-primary" type="submit">Войти</button>
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

    rows = ""
    online_count = 0
    for sess in sessions:
        online = is_peer_online(sess.get("handshake", ""))
        if online:
            online_count += 1
        status = '<span class="badge badge-on">Online</span>' if online else '<span class="badge badge-off">Offline</span>'
        real_ip = sess["real_ip"]
        label = labels.get(real_ip, "")
        label_display = f"<b>{label}</b> " if label else ""

        rows += f"""<tr>
        <td>{sess['client_id']}</td>
        <td>{sess['tunnel_ip']}</td>
        <td>{label_display}{real_ip}</td>
        <td>{sess['handshake']}</td>
        <td>{sess['rx']} / {sess['tx']}</td>
        <td>{status}</td>
        <td><a href="/kick/{sess['public_key']}" class="btn btn-danger btn-sm">Kick</a></td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="7" style="text-align:center;color:#64748b">Нет активных сессий</td></tr>'

    html = f"""
    <div class="container">
    <div class="nav">
        <a href="/sessions" class="active">Сессии ({online_count})</a>
        <a href="/clients">Клиенты</a>
        <a href="/labels">Метки</a>
        <a href="/settings">Настройки</a>
        <a href="/logout">Выход</a>
    </div>
    <div class="card">
    <h2>Активные сессии</h2>
    <table>
    <tr><th>Клиент</th><th>VPN IP</th><th>Real IP</th><th>Handshake</th><th>RX / TX</th><th>Статус</th><th></th></tr>
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
                    msg = f'<div class="alert alert-ok">Клиент {name} создан</div>'
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

    clients = get_clients()
    peers = get_wg_peers()
    online_pubs = set()
    for pub, info in peers.items():
        if is_peer_online(info.get("latest handshake", "")):
            online_pubs.add(pub)

    rows = ""
    today = datetime.now().date()
    for c in clients:
        cid = c.get("client_id", "")
        pub = c.get("public_key", "")
        ip = c.get("tunnel_ip_v4", "")
        created = c.get("created_at", "")[:10]
        is_online = pub in online_pubs
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
                    expiry_badge = f'<span class="expiry-badge badge-lock">⛔ истёк</span>'
                elif days <= 1:
                    expiry_badge = f'<span class="expiry-badge badge-warn">⚠️ {days}д</span>'
                elif days <= 3:
                    expiry_badge = f'<span class="expiry-badge badge-warn">{days}д</span>'
                else:
                    expiry_badge = f'<span class="expiry-badge badge-on">{days}д</span>'
            except ValueError:
                pass

        rows += f"""<tr>
        <td>{cid}</td>
        <td>{ip}</td>
        <td>{created}</td>
        <td>{status}</td>
        <td>
            <form method="post" style="display:flex;gap:4px;align-items:center">
            <input type="hidden" name="action" value="set_expiry">
            <input type="hidden" name="client_id" value="{cid}">
            <input type="date" name="expiry" value="{expiry}" style="width:140px;padding:4px">
            <button class="btn btn-primary btn-sm" type="submit">✓</button>
            {expiry_badge}
            </form>
        </td>
        <td>
            <form method="post" onsubmit="return confirm('Удалить {cid}?')">
            <input type="hidden" name="action" value="delete">
            <input type="hidden" name="client_id" value="{cid}">
            <button class="btn btn-danger btn-sm" type="submit">✕</button>
            </form>
        </td>
        </tr>"""

    html = f"""
    <div class="container">
    <div class="nav">
        <a href="/sessions">Сессии</a>
        <a href="/clients" class="active">Клиенты</a>
        <a href="/labels">Метки</a>
        <a href="/settings">Настройки</a>
        <a href="/logout">Выход</a>
    </div>
    {msg}
    <div class="card">
    <h2>Клиенты VPN</h2>
    <form method="post" class="form-row" style="margin-bottom:16px">
    <input type="hidden" name="action" value="add">
    <input type="text" name="name" placeholder="Имя нового клиента" pattern="[a-zA-Z0-9_-]+" required>
    <button class="btn btn-primary" type="submit">Добавить</button>
    </form>
    <table>
    <tr><th>Клиент</th><th>VPN IP</th><th>Создан</th><th>Статус</th><th>Подписка</th><th></th></tr>
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
        <button class="btn btn-danger btn-sm" type="submit">✕</button>
        </form></td></tr>"""

    html = f"""
    <div class="container">
    <div class="nav">
        <a href="/sessions">Сессии</a>
        <a href="/clients">Клиенты</a>
        <a href="/labels" class="active">Метки</a>
        <a href="/settings">Настройки</a>
        <a href="/logout">Выход</a>
    </div>
    {msg}
    <div class="card">
    <h2>Метки по Real IP</h2>
    <form method="post" class="form-row" style="margin-bottom:16px">
    <input type="hidden" name="action" value="add">
    <input type="text" name="ip" placeholder="Real IP" required>
    <input type="text" name="label" placeholder="Имя объекта" required>
    <button class="btn btn-primary" type="submit">Добавить</button>
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
    <div class="nav">
        <a href="/sessions">Сессии</a>
        <a href="/clients">Клиенты</a>
        <a href="/labels">Метки</a>
        <a href="/settings" class="active">Настройки</a>
        <a href="/logout">Выход</a>
    </div>
    {msg}
    <div class="card">
    <h2>Настройки панели</h2>
    <form method="post">
    <div class="form-row"><label>Пароль панели</label><input type="password" name="admin_pass" placeholder="Оставьте пустым"></div>
    <div class="form-row"><label>Telegram Token</label><input type="text" name="tg_bot_token" value="{s.get('tg_bot_token','')}"></div>
    <div class="form-row"><label>Telegram Chat ID</label><input type="text" name="tg_chat_id" value="{s.get('tg_chat_id','')}"></div>
    <div class="form-row"><label>Интервал (сек)</label><input type="number" name="monitor_interval" value="{s.get('monitor_interval',30)}" min="10"></div>
    <div class="form-row"><label></label><button class="btn btn-primary" type="submit">Сохранить</button></div>
    </form>
    </div>

    <div class="card">
    <h2>Информация о сервере</h2>
    <table>
    <tr><td>VPS IP</td><td>{SERVER_IP}</td></tr>
    <tr><td>WireGuard порт</td><td>51820 (localhost)</td></tr>
    <tr><td>Обфускатор порт</td><td>51821</td></tr>
    <tr><td>Панель порт</td><td>8443</td></tr>
    <tr><td>Phobos клиенты</td><td>{CLIENTS_DIR}</td></tr>
    </table>
    </div>

    <div class="card">
    <h2>Установка на роутер</h2>
    <p style="color:#94a3b8;font-size:.85em;margin-bottom:8px">Keenetic/Netcraze с Entware — выполнить по SSH на роутере:</p>
    <code style="background:#0f172a;padding:8px 12px;border-radius:6px;display:block;font-size:.85em;word-break:break-all" id="install-cmd">curl -s http://{SERVER_IP}/init/TOKEN.sh | sh</code>
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
