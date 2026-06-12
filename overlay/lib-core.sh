#!/usr/bin/env bash

set -euo pipefail

PHOBOS_DIR="${PHOBOS_DIR:-/opt/Phobos}"
SERVER_ENV="${SERVER_ENV:-$PHOBOS_DIR/server/server.env}"
CLIENTS_DIR="${CLIENTS_DIR:-$PHOBOS_DIR/clients}"
PACKAGES_DIR="${PACKAGES_DIR:-$PHOBOS_DIR/packages}"
WWW_DIR="${WWW_DIR:-$PHOBOS_DIR/www}"
TOKENS_DIR="${TOKENS_DIR:-$PHOBOS_DIR/tokens}"
TOKENS_FILE="${TOKENS_FILE:-$TOKENS_DIR/tokens.json}"
REPO_DIR="${REPO_DIR:-$PHOBOS_DIR/repo}"
WG_CONFIG="${WG_CONFIG:-/etc/wireguard/wg0.conf}"
TOKEN_TTL="${TOKEN_TTL:-86400}"
CLIENT_WG_PORT="${CLIENT_WG_PORT:-13255}"

log_info() { echo "[INFO] $*"; }
log_warn() { echo "[WARN] $*" >&2; }
log_success() { echo "[OK] $*"; }
die() { echo "[ERROR] $*" >&2; exit 1; }

check_root() {
  [ "$(id -u)" = "0" ] || die "run as root"
}

load_env() {
  if [ -f "$SERVER_ENV" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        ''|\#*) continue ;;
      esac
      k="${line%%=*}"
      v="${line#*=}"
      case "$k" in
        ''|\#*) continue ;;
      esac
      export "$k=$v"
    done < "$SERVER_ENV"
  fi

  if [ -z "${SERVER_PUBLIC_IP_V4:-}" ]; then
    SERVER_PUBLIC_IP_V4="$(curl -4 -fsS --max-time 6 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
    export SERVER_PUBLIC_IP_V4
  fi

  if [ -z "${SERVER_WG_PUBLIC_KEY:-}" ]; then
    SERVER_WG_PUBLIC_KEY="$(wg show wg0 public-key 2>/dev/null || true)"
    export SERVER_WG_PUBLIC_KEY
  fi

  if [ -n "${OBFUSCATOR_PORTS:-}" ]; then
    OBFUSCATOR_PORT="$(echo "$OBFUSCATOR_PORTS" | cut -d',' -f1 | tr -d ' ')"
    export OBFUSCATOR_PORT
  fi
}

ensure_dirs() {
  mkdir -p \
    "$CLIENTS_DIR" \
    "$PACKAGES_DIR" \
    "$WWW_DIR/init" \
    "$WWW_DIR/packages" \
    "$TOKENS_DIR" \
    "$REPO_DIR/server/scripts" \
    "$REPO_DIR/client/templates"

  [ -f "$TOKENS_FILE" ] || echo "[]" > "$TOKENS_FILE"
}
