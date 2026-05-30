#!/bin/bash
# ============================================================
#  PCA Phobos — updater / rollback
#
#    phobos-update                 # обновить до последней версии (main)
#    phobos-update v1.1.0          # обновить до конкретной версии (git tag)
#    phobos-update --rollback      # откатить на предыдущую версию (из бэкапа)
#    phobos-update --version       # показать установленную и последнюю версию
#    phobos-update --list          # список бэкапов для отката
#
#  Обновляет ТОЛЬКО PCA-слой (панель + скрипты онбординга/служебные).
#  НЕ трогает ключи WireGuard, server.env, клиентов — они сохраняются.
#  Перед каждым обновлением делает бэкап; откат восстанавливает его.
# ============================================================
set -e
PHOBOS_DIR=/opt/Phobos
PANEL_DIR=/opt/phobos-panel
REPO=andrey271192/PCA_Phobos
RAW="https://raw.githubusercontent.com/$REPO"
BACKUPS="$PHOBOS_DIR/updates"
VERFILE="$PANEL_DIR/.version"
CUR=$(cat "$VERFILE" 2>/dev/null || echo "unknown")

managed_files() {
cat <<LIST
$PANEL_DIR/app.py|app.py
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

latest_version() { curl -fsSL -m10 "$RAW/main/VERSION" 2>/dev/null | tr -d ' \r\n'; }

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
    local ref="$1"
    managed_files | while IFS='|' read -r lp rp; do
        local tmp; tmp=$(mktemp)
        if curl -fsSL -m25 "$RAW/$ref/$rp" -o "$tmp" && [ -s "$tmp" ]; then
            mkdir -p "$(dirname "$lp")"; mv "$tmp" "$lp"
            case "$lp" in *.sh|*.py) chmod +x "$lp" 2>/dev/null || true;; esac
        else
            rm -f "$tmp"; echo "  WARN: не удалось скачать $rp (пропуск)"
        fi
    done
}

case "${1:-}" in
  --version|-v)
    echo "Установлено: $CUR"
    echo "Доступно:    $(latest_version)"
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
    ref="${1:-main}"; [ "$ref" = "latest" ] && ref="main"
    target=$(curl -fsSL -m10 "$RAW/$ref/VERSION" 2>/dev/null | tr -d ' \r\n'); [ -z "$target" ] && target="$ref"
    echo "Текущая: $CUR  ->  целевая: $target  ($ref)"
    b=$(do_backup); echo "Бэкап: $b"
    apply_ref "$ref"
    echo "$target" > "$VERFILE"
    systemctl restart phobos-panel 2>/dev/null || true
    echo "Обновлено до $target. Откат: phobos-update --rollback"
    ;;
esac
