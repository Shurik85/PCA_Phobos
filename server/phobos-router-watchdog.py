#!/usr/bin/env python3
"""
Phobos Router Watchdog (server-side).

Problem it solves: on some Keenetic firmware (seen on 5.1 Beta), after a reboot
the Entware /opt disk mounts but the init hook (rc.unslung) does NOT run, so the
obfuscator + cron + dropbear never start and the router's Phobos tunnel stays
down. Because cron itself didn't start, the on-router self-heal can't help.

This watchdog runs on the primary server (cron, every few minutes). For each
router that has KeenDNS web access configured, it checks whether the client has
a fresh WG handshake on ANY server. If a router has been offline past a grace
period, it logs into the router's web UI over KeenDNS (ndm challenge auth) and
re-triggers the opkg init (which runs rc.unslung -> starts everything). Sends a
Telegram note on down / recovery / action.

Per-router config lives in /opt/phobos-panel/settings.json under
router_access[<client_id>]:
    keendns_host : e.g. "homesmart.netcraze.pro"
    web_login    : Keenetic web user
    web_pass     : Keenetic web password
    opkg_disk    : opkg disk id, e.g. "EXT4-XXXX:/" (default below)
Routers without these fields are skipped (watchdog is opt-in per router).
"""
import json, os, time, ssl, hashlib, http.cookiejar, urllib.request, urllib.error, subprocess

SETTINGS = "/opt/phobos-panel/settings.json"
SERVERS_FILE = "/opt/phobos-panel/servers.json"
CLIENTS_DIR = "/opt/Phobos/clients"
STATE_FILE = "/opt/Phobos/server/watchdog-state.json"
LOG = "/opt/Phobos/server/watchdog.log"

OFFLINE_SECS = int(os.environ.get("WD_OFFLINE_SECS", "300"))  # offline if newest handshake older than this
COOLDOWN = int(os.environ.get("WD_COOLDOWN", "600"))          # min seconds between recovery attempts per router
DEFAULT_DISK = "EXT4-V88axM0d:/"


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def tg(token, chat, text):
    if not token or not chat:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}), timeout=8)
    except Exception:
        pass


def client_pub(cid):
    try:
        return json.load(open(f"{CLIENTS_DIR}/{cid}/metadata.json")).get("public_key", "")
    except Exception:
        return ""


def newest_handshake_age(pub, servers):
    """Smallest handshake age (s) for pub across local wg0 + secondary agents."""
    best = 99999
    try:
        out = subprocess.check_output(["wg", "show", "wg0", "dump"], text=True, timeout=5)
        for ln in out.strip().split("\n")[1:]:
            f = ln.split("\t")
            if f and f[0] == pub and len(f) >= 5 and f[4].isdigit() and int(f[4]) > 0:
                best = min(best, int(time.time()) - int(f[4]))
    except Exception:
        pass
    for srv in servers:
        try:
            req = urllib.request.Request(f"http://{srv['ip']}:8444/api/health",
                                         headers={"X-API-Key": srv.get("api_key", "")})
            d = json.loads(urllib.request.urlopen(req, timeout=5).read())
            ts = d.get("handshakes", {}).get(pub, 0)
            if ts:
                best = min(best, int(time.time()) - int(ts))
        except Exception:
            pass
    return best


def rci_session(host, login, pw):
    """Keenetic ndm challenge auth over KeenDNS. Returns (opener, base) or (None, None)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                     urllib.request.HTTPSHandler(context=ctx))
    base = f"https://{host}"
    realm = chal = None
    try:
        op.open(base + "/auth", timeout=10)
    except urllib.error.HTTPError as e:
        realm = e.headers.get("X-NDM-Realm")
        chal = e.headers.get("X-NDM-Challenge")
    except Exception:
        return None, None
    if not realm or not chal:
        return None, None
    md5 = hashlib.md5(f"{login}:{realm}:{pw}".encode()).hexdigest()
    sha = hashlib.sha256((chal + md5).encode()).hexdigest()
    body = json.dumps({"login": login, "password": sha}).encode()
    try:
        op.open(urllib.request.Request(base + "/auth", data=body,
                headers={"Content-Type": "application/json"}, method="POST"), timeout=10)
    except Exception:
        return None, None
    return op, base


def retrigger_opkg(op, base, disk):
    """Force opkg 'disk changed' so Keenetic re-runs initrc (rc.unslung)."""
    cur = ""
    try:
        cur = json.loads(op.open(base + "/rci/show/rc/opkg", timeout=8).read()).get("disk", {}).get("disk", "")
    except Exception:
        pass
    newdisk = disk
    if cur.strip() == disk.strip():
        newdisk = disk.rstrip("/") if disk.endswith("/") else disk + "/"
    body = json.dumps([{"opkg": {"disk": newdisk}},
                       {"system": {"configuration": {"save": {}}}}]).encode()
    try:
        op.open(urllib.request.Request(base + "/rci/", data=body,
                headers={"Content-Type": "application/json"}, method="POST"), timeout=20)
        return True
    except Exception:
        return False


def main():
    s = load(SETTINGS, {})
    st = load(STATE_FILE, {})
    token = s.get("tg_bot_token")
    chat = s.get("tg_chat_id")
    servers = load(SERVERS_FILE, [])
    ra = s.get("router_access", {})
    now = int(time.time())
    changed = False

    for cid, acc in ra.items():
        host = acc.get("keendns_host")
        login = acc.get("web_login")
        pw = acc.get("web_pass")
        disk = acc.get("opkg_disk", DEFAULT_DISK)
        if not (host and login and pw):
            continue
        pub = client_pub(cid)
        if not pub:
            continue
        age = newest_handshake_age(pub, servers)
        rec = st.get(cid, {})

        if age <= OFFLINE_SECS:
            if rec.get("offline"):
                log(f"{cid}: recovered (handshake {age}s)")
                tg(token, chat, f"✅ Router {cid} recovered (handshake {age}s).")
            st[cid] = {"offline": False, "last_recover": rec.get("last_recover", 0)}
            changed = True
            continue

        # offline
        if now - rec.get("last_recover", 0) < COOLDOWN:
            continue
        op, base = rci_session(host, login, pw)
        if not op:
            if not rec.get("offline"):
                log(f"{cid}: offline, web unreachable")
                tg(token, chat, f"\U0001F534 Router {cid} OFFLINE, web unreachable (powered off / no internet?).")
            st[cid] = {"offline": True, "last_recover": rec.get("last_recover", 0)}
            changed = True
            continue
        ok = retrigger_opkg(op, base, disk)
        log(f"{cid}: offline ({age}s), re-triggered opkg via RCI -> {'ok' if ok else 'FAIL'}")
        tg(token, chat, f"\U0001F6E0 Router {cid} Entware down (reboot didn't autostart) — re-triggered via RCI ({'ok' if ok else 'FAILED'}).")
        st[cid] = {"offline": True, "last_recover": now}
        changed = True

    if changed:
        try:
            json.dump(st, open(STATE_FILE, "w"))
        except Exception:
            pass


if __name__ == "__main__":
    main()
