#!/bin/sh
# ============================================================
#  Phobos Health Monitor — Auto-heal + Failover for routers
#  Runs via cron every 60 seconds on Keenetic/Entware
#
#  Install: crontab -e → */1 * * * * /opt/etc/Phobos/phobos-health.sh
# ============================================================

PHOBOS_DIR="/opt/etc/Phobos"
CONF="$PHOBOS_DIR/failover.conf"
STATE="$PHOBOS_DIR/state"
LOG="$PHOBOS_DIR/health.log"
WG_CONF="$PHOBOS_DIR/wg0.conf"
OBF_CONF="$PHOBOS_DIR/wg-obfuscator.conf"
LOCKFILE="/tmp/phobos-health.lock"

MAX_LOG_LINES=200
HANDSHAKE_WARN=180
HANDSHAKE_RESTART_OBF=300
HANDSHAKE_RESTART_WG=600
HANDSHAKE_FAILOVER=900

log() {
    echo "$(date '+%H:%M:%S') $1" >> "$LOG"
    # Trim log
    if [ "$(wc -l < "$LOG" 2>/dev/null)" -gt "$MAX_LOG_LINES" ]; then
        tail -n 100 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
}

# Prevent concurrent runs
if [ -f "$LOCKFILE" ]; then
    pid=$(cat "$LOCKFILE" 2>/dev/null)
    if kill -0 "$pid" 2>/dev/null; then
        exit 0
    fi
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT

# Create state dir
mkdir -p "$STATE"

# ── Read failover config ──
if [ ! -f "$CONF" ]; then
    log "ERROR: no failover.conf"
    exit 1
fi

# Parse servers: SERVER_1=ip:port1,port2,port3
# Parse current state
CURRENT_SERVER=$(cat "$STATE/current_server" 2>/dev/null || echo "1")
CURRENT_PORT_IDX=$(cat "$STATE/current_port_idx" 2>/dev/null || echo "0")
RESTART_COUNT=$(cat "$STATE/restart_count" 2>/dev/null || echo "0")
PRIMARY_CHECK_TS=$(cat "$STATE/primary_check_ts" 2>/dev/null || echo "0")

# Load server list
SERVER_COUNT=0
idx=1
while true; do
    val=$(grep "^SERVER_${idx}=" "$CONF" 2>/dev/null | cut -d= -f2-)
    if [ -z "$val" ]; then
        break
    fi
    eval "SERVER_${idx}_HOST=$(echo "$val" | cut -d: -f1)"
    eval "SERVER_${idx}_PORTS=$(echo "$val" | cut -d: -f2-)"
    eval "SERVER_${idx}_KEY=$(grep "^KEY_${idx}=" "$CONF" 2>/dev/null | cut -d= -f2-)"
    SERVER_COUNT=$idx
    idx=$((idx + 1))
done

if [ "$SERVER_COUNT" -eq 0 ]; then
    log "ERROR: no servers in failover.conf"
    exit 1
fi

# ── Get current handshake age ──
get_handshake_age() {
    hs=$(wg show wg0 latest-handshakes 2>/dev/null | awk '{print $2}')
    if [ -z "$hs" ] || [ "$hs" = "0" ]; then
        echo "9999"
        return
    fi
    now=$(date +%s)
    age=$((now - hs))
    echo "$age"
}

# ── Get port by index from comma-separated list ──
get_port() {
    server_idx=$1
    port_idx=$2
    eval "ports=\$SERVER_${server_idx}_PORTS"
    echo "$ports" | tr ',' '\n' | sed -n "$((port_idx + 1))p"
}

get_port_count() {
    server_idx=$1
    eval "ports=\$SERVER_${server_idx}_PORTS"
    echo "$ports" | tr ',' '\n' | wc -l
}

# ── Switch to specific server:port ──
switch_endpoint() {
    server_idx=$1
    port_idx=$2

    eval "host=\$SERVER_${server_idx}_HOST"
    eval "obf_key=\$SERVER_${server_idx}_KEY"
    port=$(get_port "$server_idx" "$port_idx")

    if [ -z "$host" ] || [ -z "$port" ]; then
        log "ERROR: invalid server $server_idx port_idx $port_idx"
        return 1
    fi

    log "SWITCH → server $server_idx ($host:$port)"

    # Update obfuscator config target
    if [ -f "$OBF_CONF" ]; then
        sed -i "s|^target = .*|target = ${host}:${port}|" "$OBF_CONF"
        if [ -n "$obf_key" ]; then
            sed -i "s|^key = .*|key = ${obf_key}|" "$OBF_CONF"
        fi
    fi

    # Restart obfuscator
    killall wg-obfuscator 2>/dev/null
    sleep 1
    wg-obfuscator --config "$OBF_CONF" &

    # Save state
    echo "$server_idx" > "$STATE/current_server"
    echo "$port_idx" > "$STATE/current_port_idx"
    echo "0" > "$STATE/restart_count"
}

# ── Try next port on current server ──
try_next_port() {
    port_count=$(get_port_count "$CURRENT_SERVER")
    next_idx=$(( (CURRENT_PORT_IDX + 1) % port_count ))

    if [ "$next_idx" -eq 0 ]; then
        return 1
    fi

    log "PORT HOP → port idx $next_idx on server $CURRENT_SERVER"
    switch_endpoint "$CURRENT_SERVER" "$next_idx"
    return 0
}

# ── Try next server ──
try_next_server() {
    next=$((CURRENT_SERVER + 1))
    if [ "$next" -gt "$SERVER_COUNT" ]; then
        next=1
    fi

    if [ "$next" -eq "$CURRENT_SERVER" ]; then
        log "WARN: only one server, can't failover"
        return 1
    fi

    log "FAILOVER → server $next"
    switch_endpoint "$next" "0"
    return 0
}

# ── Check if primary is back (every 5 min when on secondary) ──
check_primary() {
    if [ "$CURRENT_SERVER" -eq 1 ]; then
        return
    fi

    now=$(date +%s)
    elapsed=$((now - PRIMARY_CHECK_TS))
    if [ "$elapsed" -lt 300 ]; then
        return
    fi
    echo "$now" > "$STATE/primary_check_ts"

    eval "host=\$SERVER_1_HOST"
    port=$(get_port 1 0)

    # Quick UDP probe — send a byte, see if port responds
    if ping -c 1 -W 2 "$host" >/dev/null 2>&1; then
        log "PRIMARY alive, switching back"
        switch_endpoint 1 0
    fi
}

# ── Main logic ──
AGE=$(get_handshake_age)

if [ "$AGE" -lt "$HANDSHAKE_WARN" ]; then
    # All good, reset counters
    if [ "$RESTART_COUNT" -gt 0 ]; then
        log "OK: connection restored (age=${AGE}s)"
        echo "0" > "$STATE/restart_count"
    fi
    check_primary
    exit 0
fi

log "STALE handshake: ${AGE}s (server=$CURRENT_SERVER port_idx=$CURRENT_PORT_IDX restarts=$RESTART_COUNT)"

if [ "$AGE" -lt "$HANDSHAKE_RESTART_OBF" ]; then
    # Restart obfuscator only
    RESTART_COUNT=$((RESTART_COUNT + 1))
    echo "$RESTART_COUNT" > "$STATE/restart_count"
    log "ACTION: restart obfuscator (attempt $RESTART_COUNT)"
    killall wg-obfuscator 2>/dev/null
    sleep 1
    wg-obfuscator --config "$OBF_CONF" &
    exit 0
fi

if [ "$AGE" -lt "$HANDSHAKE_RESTART_WG" ]; then
    # Restart WireGuard + obfuscator
    RESTART_COUNT=$((RESTART_COUNT + 1))
    echo "$RESTART_COUNT" > "$STATE/restart_count"
    log "ACTION: restart WG + obfuscator (attempt $RESTART_COUNT)"
    wg-quick down wg0 2>/dev/null
    killall wg-obfuscator 2>/dev/null
    sleep 2
    wg-quick up wg0 2>/dev/null
    sleep 1
    wg-obfuscator --config "$OBF_CONF" &
    exit 0
fi

if [ "$AGE" -lt "$HANDSHAKE_FAILOVER" ]; then
    # Try next port
    log "ACTION: try port hop"
    if ! try_next_port; then
        log "ACTION: all ports exhausted, try next server"
        try_next_server
    fi
    exit 0
fi

# Full failover
log "ACTION: failover (handshake ${AGE}s)"
if ! try_next_server; then
    # Only one server, cycle ports
    try_next_port
fi
