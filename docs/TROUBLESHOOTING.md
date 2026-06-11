# PCA Phobos: проблемы, диагностика и решения

Этот документ для живых ситуаций: установка не пошла, роутер не поднялся, Android не импортирует QR, secondary отвечает 401, Telegram молчит. Ниже симптомы, причины, проверки и безопасные решения.

Не публикуй в issue и чатах пароли, токены, приватные ключи WireGuard, `server_api_key`, `pull_token`, `GH_TOKEN`/`PHOBOS_KEY`, `PrivateKey`, содержимое `settings.json` целиком. Если нужен лог или конфиг, замени секреты на `***`.

## Быстрый чек-лист

На сервере:

```bash
phobos-update stable
cat /opt/phobos-panel/.version
cat /opt/phobos-panel/.port
systemctl status phobos-panel --no-pager
journalctl -u phobos-panel -n 80 --no-pager
curl -s http://127.0.0.1:$(cat /opt/phobos-panel/.port)/api/obf-health
```

На роутере:

```sh
/opt/etc/init.d/S49wg-obfuscator status
tail -n 80 /opt/etc/Phobos/health.log
grep '^SERVER_' /opt/etc/Phobos/failover.conf
grep '^target' /opt/etc/Phobos/wg-obfuscator.conf
crontab -l | grep phobos
```

На secondary:

```bash
systemctl status phobos-api --no-pager
journalctl -u phobos-api -n 80 --no-pager
curl -i http://SECONDARY_IP:8444/api/health
```

## Установка сервера

### Команда установки пишет `403`, `404`, `Bad credentials`

Чаще всего ставится закрытый канал без ключа или ключ устарел.

Stable ставится без ключа:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/install.sh)
```

Beta/dev требуют ключ:

```bash
PHOBOS_KEY=ваш_ключ CHANNEL=beta \
bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/install.sh)
```

Проверка доступа к public stable:

```bash
curl -I https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/install.sh
```

Проверка закрытого канала:

```bash
curl -I -H "Authorization: token ваш_ключ" \
https://raw.githubusercontent.com/andrey271192/PCA_Phobos-dev/beta/install.sh
```

### `[2/9] wg-obfuscator`: `ERROR: obfuscator binary for x86_64 missing`

Это ошибка скачивания бинарника обфускатора под архитектуру VPS. Обычно сервер `x86_64`, а в скачанном наборе нет файла с ожидаемым именем.

Проверка:

```bash
uname -m
ls -lah /opt/Phobos/bin 2>/dev/null
```

Решение:

```bash
phobos-update stable
```

Если установка ещё не дошла до `phobos-update`, запусти свежий installer:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/install.sh)
```

В актуальной версии installer берёт `linux-x64` из ClusterM releases и понимает `amd64 -> x86_64`.

### `[6/9] PCA patches`: `curl: (23) Failure writing output to destination`

Это не ошибка firewall и не порт WireGuard. `curl` не смог записать файл в путь назначения. Частая причина: папки `/opt/Phobos/repo/server/scripts` или `/opt/Phobos/repo/client/templates` ещё не были созданы.

Решение:

```bash
phobos-update stable
```

Если сервер ставится с нуля и ещё нет `phobos-update`, повтори установку свежим installer:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/install.sh)
```

В актуальной версии `fetch()` сам создаёт папку назначения перед `curl -o`.

### `/clients`: `lib-core.sh: No such file or directory`, `check_root: command not found`

Так выглядит неполная установка client helper. Файл `phobos-client.sh` уже есть, но рядом нет обязательного `lib-core.sh`. После этого скрипт теряет функции `check_root`, `load_env`, `ensure_dirs`, `die`, `log_info`, и появляются каскадные ошибки.

Быстрое лечение без переустановки:

```bash
mkdir -p /opt/Phobos/repo/server/scripts
curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/overlay/lib-core.sh \
  -o /opt/Phobos/repo/server/scripts/lib-core.sh
chmod +x /opt/Phobos/repo/server/scripts/lib-core.sh
phobos-update stable
systemctl restart phobos-panel
```

Проверка:

```bash
test -f /opt/Phobos/repo/server/scripts/lib-core.sh && echo LIB_CORE_OK
/opt/Phobos/repo/server/scripts/phobos-client.sh list
```

### В Chrome консоли: `Pattern attribute value ... is not a valid regular expression`

Старый HTML pattern для имени клиента мог ругаться в новых версиях Chrome.

Решение:

```bash
phobos-update stable
systemctl restart phobos-panel
```

После обновления в `/opt/phobos-panel/app.py` должно быть:

```bash
grep 'pattern=' /opt/phobos-panel/app.py
```

Ожидаемо:

```text
pattern="[A-Za-z0-9_\\-]+"
```

### Панель не открывается

Проверь реальный порт панели:

```bash
cat /opt/phobos-panel/.port
```

Проверь сервис:

```bash
systemctl status phobos-panel --no-pager
journalctl -u phobos-panel -n 120 --no-pager
ss -lntp | grep "$(cat /opt/phobos-panel/.port)"
```

Если сервис упал:

```bash
systemctl restart phobos-panel
journalctl -u phobos-panel -n 120 --no-pager
```

Если сервис живой, но браузер не открывает панель, проверь firewall/VPS-панель: порт из `/opt/phobos-panel/.port` должен быть открыт.

### Забыт порт, пароль или API key

Порт:

```bash
cat /opt/phobos-panel/.port
```

Пароль:

```bash
grep admin_pass /opt/phobos-panel/settings.json
```

API key:

```bash
grep server_api_key /opt/phobos-panel/settings.json
```

Не отправляй эти значения в публичный issue.

### Обновление сломало панель

Откат:

```bash
phobos-update --rollback
```

Список бэкапов:

```bash
phobos-update --list
```

Обновление трогает панель и скрипты, но не должно удалять WireGuard-ключи, клиентов и `/opt/Phobos/server/server.env`.

## Установка на роутер

### Команда на роутере пишет `ERROR_install_script_download_failed`

Причины:

- install token старый или просрочен;
- роутер не достучался до порта панели;
- вместо shell скачался HTML;
- пакет клиента собран старой версией скриптов.

Проверка с роутера:

```sh
u="http://SERVER_IP:PANEL_PORT/init/TOKEN.sh"
wget -q -O /tmp/phobos-init-test.sh "$u"
head -n 3 /tmp/phobos-init-test.sh
```

Хорошо:

```sh
#!/bin/sh
```

Плохо:

```html
<!DOCTYPE html>
```

Решение:

```bash
phobos-update stable
/opt/Phobos/repo/server/scripts/phobos-client.sh package CLIENT_ID
/opt/Phobos/repo/server/scripts/phobos-client.sh link CLIENT_ID
```

Потом вставь новую команду установки на роутере.

### Пакет битый: `Bad package`, `not in gzip format`, HTML вместо tar.gz

Роутер скачал не пакет, а страницу ошибки.

Проверка:

```sh
file /tmp/phobos_install_*/package.tar.gz
tar tzf /tmp/phobos_install_*/package.tar.gz | head
```

Решение:

- скопируй новую install-команду из панели;
- проверь, что в команде правильный порт панели;
- пересобери пакет клиента командой `package CLIENT_ID`;
- если VPS firewall закрывает порт панели, открой его.

### Установка прошла, но WireGuard интерфейс не появился

Проверка:

```sh
ls -l /opt/etc/Phobos
/opt/etc/init.d/S49wg-obfuscator status
ndmc -c "show interface" | grep -i Phobos
tail -n 120 /opt/etc/Phobos/health.log
```

Причины:

- нет свободного `Wireguard0..Wireguard9`;
- старый Phobos-интерфейс остался после ручных тестов;
- RCI API роутера вернул ошибку.

Безопасное решение:

```sh
/opt/etc/Phobos/phobos-uninstall.sh
```

Потом вставь свежую команду установки. Если удаляешь интерфейсы вручную, трогай только те, где description начинается с `Phobos-`.

### SSH test роутера в панели показывает timeout

Это часто нормально. Роутеры обычно за NAT, мобильным интернетом или KeenDNS. PCA Phobos не требует входящий SSH на роутер: основной способ управления - pull, роутер сам тянет конфиг.

Проверяй pull:

```sh
crontab -l | grep phobos-pull
tail -n 80 /opt/etc/Phobos/health.log
```

Если `phobos-pull.sh` есть в cron, timeout SSH не значит, что система сломана.

### После reboot у роутера сменился внешний IP

Это нормально для домашнего провайдера. Phobos должен подняться сам.

Проверка:

```sh
/opt/etc/init.d/S49wg-obfuscator status
ndmc -c "show interface Wireguard3"
grep '^target' /opt/etc/Phobos/wg-obfuscator.conf
crontab -l | grep phobos
```

Если не поднялся:

```sh
/opt/etc/init.d/S49wg-obfuscator restart
sh /opt/etc/Phobos/phobos-health.sh
tail -n 120 /opt/etc/Phobos/health.log
```

### После reboot Phobos не стартует

Частые причины:

- Entware не смонтировался;
- `/opt` поднялся поздно;
- cron не стартовал;
- init-скрипт потерял execute-bit.

Проверка:

```sh
mount | grep /opt
ls -l /opt/etc/init.d/S49wg-obfuscator
crontab -l | grep phobos
```

Решение:

```sh
chmod +x /opt/etc/init.d/S49wg-obfuscator
/opt/etc/init.d/S49wg-obfuscator restart
```

Если `/opt` пустой или не смонтирован, это проблема Entware/USB/накопителя.

### На роутере VPN есть, но LAN-устройства не ходят через него

Для Keenetic/Netcraze важны:

- Phobos WireGuard interface должен быть `security-level public`;
- LAN-устройства должны быть привязаны к нужной policy.

Проверка:

```sh
ndmc -c "show interface Wireguard3" | grep security-level
ndmc -c "show running-config" | grep -E 'ip hotspot host|policy'
```

Health-скрипт пытается чинить это сам. Запусти вручную:

```sh
sh /opt/etc/Phobos/phobos-health.sh
tail -n 120 /opt/etc/Phobos/health.log
```

### Удалить Phobos с роутера

На роутере:

```sh
/opt/etc/Phobos/phobos-uninstall.sh
```

Скрипт останавливает obfuscator, удаляет Phobos-интерфейс WireGuard, cron-строки и файлы `/opt/etc/Phobos`.

## Failover и pull-модель

### В логах `STALE`, `PORT HOP`, `FAILOVER`

Пример:

```text
STALE: handshake=9999s
ACTION: restart obfuscator
PORT HOP -> port idx 1
FAILOVER -> server 2
```

Это не всегда ошибка. Так роутер лечит отсутствие handshake:

1. restart obfuscator;
2. другой порт на том же сервере;
3. другой сервер.

Если потом есть `OK: tunnel up`, failover работает штатно.

### Роутер не получает новый `failover.conf`

Проверка:

```sh
grep '^SERVER_' /opt/etc/Phobos/failover.conf
crontab -l | grep phobos-pull
sh /opt/etc/Phobos/phobos-pull.sh
tail -n 80 /opt/etc/Phobos/health.log
```

Если публичный адрес панели недоступен, но туннель поднят, проверь tunnel pull:

```sh
ip -o addr show | grep '10.25.'
grep -- '--interface' /opt/etc/Phobos/phobos-pull.sh
PANEL_URLS="http://10.25.0.1:10514" POLL_PASSES=1 sh /opt/etc/Phobos/phobos-pull.sh
```

Если в `phobos-pull.sh` нет `--interface`, обнови сервер, пересобери пакет клиента и поставь свежую команду на роутер.

### `/tmp/phobos-pull.lock` висит долго

Lock нужен, чтобы два pull-процесса не писали конфиг одновременно.

Проверка:

```sh
cat /tmp/phobos-pull.lock
ps | grep phobos-pull
```

Если процесса с этим PID нет:

```sh
rm -f /tmp/phobos-pull.lock
sh /opt/etc/Phobos/phobos-pull.sh
```

## Secondary servers

### `Инфо`, `Синхр.` или назначение клиента дает 401

Причина: у secondary старый или другой API key.

Что делать:

```bash
phobos-update stable
```

Потом в панели:

1. `Серверы`;
2. нажми `Инфо` на secondary;
3. если добавляешь secondary вручную, API key можно оставить пустым - панель использует главный `server_api_key`.

Если ставишь новый secondary, команда должна содержать реальный порт панели:

```bash
MAIN_SERVER=<main_ip> MAIN_PORT=<panel_port> MAIN_API_KEY=<api_key> \
bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/server/secondary-setup.sh)
```

Порт панели:

```bash
cat /opt/phobos-panel/.port
```

### Secondary не регистрируется

На secondary:

```bash
systemctl status phobos-api --no-pager
systemctl status wg-quick@wg0 --no-pager
ss -lntp | grep 8444
journalctl -u phobos-api -n 100 --no-pager
```

С основного сервера:

```bash
curl -i http://SECONDARY_IP:8444/api/health
```

Если `8444` закрыт, проверь firewall/VPS-панель и сервис `phobos-api`.

### Deploy Secondary использует не тот порт панели

Если панель не на `8443`, обязательно укажи `MAIN_PORT`.

Пример:

```bash
MAIN_SERVER=212.118.52.193 MAIN_PORT=10514 MAIN_API_KEY=*** \
bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/server/secondary-setup.sh)
```

## Android, iPhone и QR

### Android PhobosWG: `Невозможно импортировать туннель`

Проверь:

1. Импортируешь именно `phobos://` или QR с Android-страницы клиента.
2. Не импортируешь обычный `.conf` в PhobosWG.
3. Ссылка скопирована полностью, не обрезана Telegram/мессенджером.
4. Панель обновлена, QR открыт заново после обновления.

Решение:

```bash
phobos-update stable
```

Потом заново открой Android-страницу клиента и сканируй QR прямо с панели. Если не помогло, создай нового клиента для телефона. Один клиент = одно устройство; роутер и телефон должны иметь разные клиенты.

### Android импорт прошел, но интернета нет

В `[Peer]` должны быть маршруты:

```ini
AllowedIPs = 0.0.0.0/0, ::/0
```

Проверь DNS и MTU:

```ini
DNS = 8.8.8.8, 2001:4860:4860::8888
MTU = 1420
```

Если мобильная сеть режет MTU, попробуй `1280`.

### iPhone не открывает `phobos://`

На iOS используется обычный WireGuard, не `phobos://`. В панели нажми кнопку iPhone/iOS. Для iOS сервер должен иметь открытый plain WireGuard:

```bash
ALLOW_PLAIN_WG=1 bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/install.sh)
```

Если сервер уже установлен, проверь порт `51820/udp`.

### QR не сканируется

- Открой страницу клиента заново.
- Увеличь QR.
- Используй кнопку копирования ссылки.
- Не пересылай QR через мессенджер с сильным сжатием.

## Telegram

### Нет сообщений

В панели должны быть заполнены Telegram Token и Chat ID.

Проверка с сервера:

```bash
python3 - <<'PY'
import json, urllib.request
s=json.load(open('/opt/phobos-panel/settings.json'))
token=s.get('tg_bot_token','')
chat=s.get('tg_chat_id','')
print('token set:', bool(token), 'chat set:', bool(chat))
if token and chat:
    data=json.dumps({'chat_id':chat,'text':'PCA Phobos test message'}).encode()
    req=urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage',
        data=data, headers={'Content-Type':'application/json'})
    print(urllib.request.urlopen(req, timeout=15).read().decode())
PY
```

Не отправляй вывод с токеном в публичный issue.

### Сервер падал, но Telegram не прислал DOWN

Панель не объявляет сервер упавшим после одного таймаута, чтобы не спамить при коротких сетевых сбоях. Нужно несколько неудачных циклов подряд.

Проверь интервал:

```bash
grep monitor_interval /opt/phobos-panel/settings.json
```

Проверь secondary:

```bash
curl -i http://SECONDARY_IP:8444/api/health
```

Если UI стал Offline, но сообщения нет, проверь Bot API тестом выше.

## Скорость и стабильность

### Скорость низкая

Проверь, куда сейчас смотрит клиент:

```sh
grep '^target' /opt/etc/Phobos/wg-obfuscator.conf
tail -n 120 /opt/etc/Phobos/health.log
```

На сервере:

```bash
wg show
curl -s http://127.0.0.1:$(cat /opt/phobos-panel/.port)/api/obf-health
```

Если в логах постоянный `PORT HOP` или `FAILOVER`, провайдер может плохо пропускать текущий порт/IP. Поменяй приоритет серверов или проверь другой порт.

### Ping большой

Частые причины:

- выбран дальний secondary;
- роутер ушел в failover;
- перегружен VPS;
- провайдер плохо маршрутизирует конкретный порт.

Смотри страницу `Серверы`: CPU/RAM, Online/Offline, назначение клиента.

## Безопасная переустановка

Только роутер:

```sh
/opt/etc/Phobos/phobos-uninstall.sh
```

Потом вставь свежую команду установки из панели. Клиент на сервере при этом не удаляется.

Полностью пересоздать клиента:

1. Удали клиента в панели.
2. Создай нового клиента.
3. Установи новый конфиг на устройство.

Так надо делать, если один и тот же клиент случайно использовался на двух устройствах.

Полностью удалить сервер:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/uninstall.sh)
```

Без подтверждения:

```bash
PURGE=1 bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/uninstall.sh)
```

## Что писать в issue

Приложи:

- версия: `cat /opt/phobos-panel/.version`;
- канал: stable/beta/dev;
- что именно ставишь: основной сервер, secondary, роутер, Android, iOS;
- текст ошибки;
- что уже пробовал;
- последние 80-120 строк логов.

Сервер:

```bash
journalctl -u phobos-panel -n 120 --no-pager
systemctl status phobos-panel --no-pager
```

Роутер:

```sh
tail -n 120 /opt/etc/Phobos/health.log
/opt/etc/init.d/S49wg-obfuscator status
```

Secondary:

```bash
journalctl -u phobos-api -n 120 --no-pager
systemctl status phobos-api --no-pager
```

Перед отправкой замени секреты:

```text
server_api_key = ***
PrivateKey = ***
PresharedKey = ***
pull_token = ***
GH_TOKEN = ***
PHOBOS_KEY = ***
```
