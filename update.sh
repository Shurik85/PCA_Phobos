#!/bin/bash
# ============================================================
#  PCA Phobos — updater / rollback
#
#    phobos-update                 # обновить до стабильной (канал stable / main)
#    phobos-update stable          # стабильный канал (публичный, без токена)
#    GH_TOKEN=... phobos-update beta   # бета-канал (приватный, по токену подписчика)
#    GH_TOKEN=... phobos-update dev    # канал разработки (приватный, по токену)
#    phobos-update v1.2.4          # конкретная версия (git tag)
#    phobos-update --rollback      # откатить на предыдущую версию (из бэкапа)
#    phobos-update --check         # установленная + версии всех каналов
#    phobos-update --list          # список бэкапов для отката
#
#  Каналы:
#    stable -> публичный репозиторий andrey271192/PCA_Phobos  (открыт)
#    beta / dev -> приватный andrey271192/PCA_Phobos-dev      (только подписчики)
#  Для beta/dev нужен read-only токен подписчика: env GH_TOKEN или файл
#  $PANEL_DIR/.gh_token (сохраняется при установке закрытого канала).
#
#  Обновляет ТОЛЬКО PCA-слой (панель + скрипты). НЕ трогает ключи WireGuard,
#  server.env, клиентов. Перед каждым обновлением — бэкап; откат восстанавливает.
# ============================================================
set -e
PHOBOS_DIR=/opt/Phobos
PANEL_DIR=/opt/phobos-panel
PUB_REPO=andrey271192/PCA_Phobos
DEV_REPO=andrey271192/PCA_Phobos-dev
BACKUPS="$PHOBOS_DIR/updates"
VERFILE="$PANEL_DIR/.version"
CUR=$(cat "$VERFILE" 2>/dev/null || echo "unknown")
# Токен подписчика для приватных каналов: env > сохранённый файл
GH_TOKEN="${GH_TOKEN:-$(cat "$PANEL_DIR/.gh_token" 2>/dev/null || true)}"

# repo for a ref/channel: beta/dev/*-beta/*-dev -> private, остальное -> public
repo_for() { case "$1" in beta|dev|*-beta|*-dev) echo "$DEV_REPO";; *) echo "$PUB_REPO";; esac; }
is_private() { case "$1" in beta|dev|*-beta|*-dev) return 0;; *) return 1;; esac; }
# curl with token header only when ref is a private channel
gh_curl() {  # $1=ref, rest=curl args
    local ref="$1"; shift
    if is_private "$ref" && [ -n "$GH_TOKEN" ]; then
        curl -H "Authorization: token $GH_TOKEN" "$@"
    else
        curl "$@"
    fi
}

managed_files() {
cat <<LIST
$PANEL_DIR/app.py|app.py
$PHOBOS_DIR/repo/server/scripts/lib-core.sh|overlay/lib-core.sh
$PHOBOS_DIR/repo/server/scripts/phobos-client.sh|overlay/phobos-client.sh
$PHOBOS_DIR/repo/client/templates/install-router.sh.template|overlay/install-router.sh.template
$PHOBOS_DIR/repo/client/templates/router-configure-wireguard.sh|overlay/router-configure-wireguard.sh
$PHOBOS_DIR/repo/client/templates/phobos-pull.sh|overlay/phobos-pull.sh
$PHOBOS_DIR/server/phobos-health.sh|server/phobos-health.sh
$PHOBOS_DIR/server/phobos-pull.sh|server/phobos-pull.sh
$PHOBOS_DIR/server/phobos-router-watchdog.py|server/phobos-router-watchdog.py
$PHOBOS_DIR/server/update.sh|update.sh
LIST
}

# fetch VERSION of a channel (public repos / or private if token present)
chan_version() {  # $1 = ref(channel)
    local repo; repo=$(repo_for "$1"); local repo_ref="$1"
    if is_private "$1" && [ -z "$GH_TOKEN" ]; then echo ""; return; fi
    gh_curl "$1" -fsSL -m8 "https://raw.githubusercontent.com/$repo/$1/VERSION" 2>/dev/null | tr -d ' \r\n'
}
latest_version() { curl -fsSL -m10 "https://raw.githubusercontent.com/$PUB_REPO/main/VERSION" 2>/dev/null | tr -d ' \r\n'; }

do_backup() {
    local dir="$BACKUPS/${CUR}-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$dir"
    managed_files | while IFS='|' read -r lp rp; do
        [ -f "$lp" ] && install -D "$lp" "$dir/$rp" 2>/dev/null || true
    done
    echo "$CUR" > "$dir/.fromversion"
    echo "$dir"
}

apply_ref() {
    local ref="$1"; local repo; repo=$(repo_for "$ref")
    local repo_ref="$ref"
    local RAW="https://raw.githubusercontent.com/$repo/$ref"
    managed_files | while IFS='|' read -r lp rp; do
        local tmp; tmp=$(mktemp)
        if gh_curl "$ref" -fsSL -m25 "$RAW/$rp" -o "$tmp" && [ -s "$tmp" ]; then
            mkdir -p "$(dirname "$lp")"; mv "$tmp" "$lp"
            case "$lp" in *.sh|*.py) chmod +x "$lp" 2>/dev/null || true;; esac
        else
            rm -f "$tmp"; echo "  WARN: не удалось скачать $rp (пропуск)"
        fi
    done
}

ref_exists() {
    local repo; repo=$(repo_for "$1"); local repo_ref="$1"
    gh_curl "$1" -fsSL -m12 "https://raw.githubusercontent.com/$repo/$1/app.py" -o /dev/null 2>/dev/null
}

case "${1:-}" in
  --version|-v|--check)
    echo "Установлено:    $CUR"
    echo "Стабильная:     $(latest_version)   [открытый]"
    b=$(chan_version beta); d=$(chan_version dev)
    if [ -n "$GH_TOKEN" ]; then
        [ -n "$b" ] && echo "Бета (beta):    $b   [приватный, токен ок]"
        [ -n "$d" ] && echo "Разработка(dev):$d   [приватный, токен ок]"
    else
        echo "Бета (beta):    —   [приватный: нужен GH_TOKEN подписчика]"
        echo "Разработка(dev):—   [приватный: нужен GH_TOKEN подписчика]"
    fi
    ;;
  --versions|--tags)
    echo "Стабильные версии (публичные теги):"
    curl -fsSL -m10 "https://api.github.com/repos/$PUB_REPO/tags?per_page=100" 2>/dev/null \
      | grep -oE '"name": *"[^"]+"' | sed 's/.*"name": *"\(.*\)"/  \1/' || echo "  (нет данных)"
    if [ -n "$GH_TOKEN" ]; then
        echo "Закрытые версии (приватные теги, beta/dev):"
        curl -fsSL -m10 -H "Authorization: token $GH_TOKEN" "https://api.github.com/repos/$DEV_REPO/tags?per_page=100" 2>/dev/null \
          | grep -oE '"name": *"[^"]+"' | sed 's/.*"name": *"\(.*\)"/  \1/' || echo "  (нет данных)"
    fi
    ;;
  --list)
    echo "Бэкапы (для отката):"
    ls -dt "$BACKUPS"/*/ 2>/dev/null | sed 's#.*/updates/##' || echo "  нет"
    ;;
  --rollback)
    last=$(ls -dt "$BACKUPS"/*/ 2>/dev/null | head -1)
    [ -z "$last" ] && { echo "Нет бэкапа для отката."; exit 1; }
    fromver=$(cat "$last/.fromversion" 2>/dev/null || echo "?")
    echo "Откат на $fromver  ($last)"
    managed_files | while IFS='|' read -r lp rp; do
        [ -f "$last/$rp" ] && { mkdir -p "$(dirname "$lp")"; cp "$last/$rp" "$lp"; chmod +x "$lp" 2>/dev/null || true; }
    done
    echo "$fromver" > "$VERFILE"
    systemctl restart phobos-panel 2>/dev/null || true
    echo "Откат выполнен -> $fromver"
    ;;
  *)
    ref="${1:-main}"
    case "$ref" in latest|stable) ref="main";; esac
    # Каналы beta/dev — приватный репозиторий, нужен токен подписчика
    if is_private "$ref" && [ -z "$GH_TOKEN" ]; then
        echo "Канал '$ref' закрыт (бета/разработка) — приватный репозиторий."
        echo "Нужен read-only токен подписчика:"
        echo "  GH_TOKEN=ваш_токен phobos-update $ref"
        echo "Токен — по подписке: https://boosty.to/andrey27 . Стабильная (без токена): phobos-update stable"
        exit 1
    fi
    if ! ref_exists "$ref"; then
        echo "Версия/ветка '$ref' не найдена (или токен неверный). Обновление отменено (текущая $CUR не тронута)."
        echo "Доступные: phobos-update --versions  ·  стабильная: $(latest_version)"
        exit 1
    fi
    # сохранить токен для будущих обновлений приватного канала
    if is_private "$ref" && [ -n "$GH_TOKEN" ]; then
        ( umask 077; printf %s "$GH_TOKEN" > "$PANEL_DIR/.gh_token" )
    fi
    repo_ref="$ref"
    target=$(gh_curl "$ref" -fsSL -m10 "https://raw.githubusercontent.com/$(repo_for "$ref")/$ref/VERSION" 2>/dev/null | tr -d ' \r\n'); [ -z "$target" ] && target="$ref"
    echo "Текущая: $CUR  ->  целевая: $target  ($ref)"
    b=$(do_backup); echo "Бэкап: $b"
    apply_ref "$ref"
    echo "$target" > "$VERFILE"
    systemctl restart phobos-panel 2>/dev/null || true
    echo "Обновлено до $target. Откат: phobos-update --rollback"
    ;;
esac
