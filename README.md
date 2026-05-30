# PCA Phobos — Web Panel

Веб-панель управления для [Phobos](https://git.zerrolabs.org/Ground-Zerro/Phobos) (обфусцированный WireGuard VPN).

## Быстрый старт (turnkey — с чистого VPS, одной командой)

Ставит ВСЁ со всеми зависимостями: wg-obfuscator, WireGuard, обфускатор-сервисы,
веб-панель, nginx, скрипты онбординга роутеров и сторож авто-восстановления.
Предустановленный Phobos НЕ требуется.

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/install.sh)
```

> Если репозиторий приватный — добавь `GH_TOKEN`:
> ```bash
> GH_TOKEN=ghp_xxx bash <(curl -fsSL -H "Authorization: token ghp_xxx" \
>   https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/install.sh)
> ```

### Вторичный сервер (для failover/балансировки)

```bash
MAIN_SERVER=<ip_основного> MAIN_API_KEY=<API key из основного> \
bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/server/secondary-setup.sh)
```

### С кастомными параметрами

```bash
PANEL_PASS=AdminPass456 \
TG_TOKEN=1234567890:AABBCCDDaabbccdd \
TG_CHAT=123456789 \
bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/install.sh)
```

| Переменная   | По умолчанию   | Описание                        |
|--------------|----------------|---------------------------------|
| `PANEL_PASS` | `OcAdmin2026!` | Пароль веб-панели (admin)       |
| `TG_TOKEN`   | пусто          | Telegram bot token              |
| `TG_CHAT`    | пусто          | Telegram chat ID для уведомлений|
| `PANEL_PORT` | `8443`         | Порт веб-панели                 |

---

## Возможности

- **Активные сессии** — VPN IP, Real IP, handshake, трафик RX/TX, Kick
- **Клиенты VPN** — добавить/удалить через Phobos, статус online/offline
- **Именование объектов** — привязать имя к Real IP (отображается в сессиях и Telegram)
- **Срок подписки** — дата окончания для каждого клиента:
  - Date picker в таблице клиентов
  - Обратный отсчёт (18д, 3д⚠️, завтра⚠️, истёк⛔)
  - При истечении: автокик + Telegram уведомление
  - Предупреждения за 3 дня и 1 день
- **Telegram уведомления** — 🟢 подключение, 🔴 отключение, ⚠️ за 3 дня, ⛔ истёк
- **Настройки** — смена пароля панели, Telegram bot token + chat ID, интервал мониторинга
- **Инфо о сервере** — порты, пути, команда установки на роутер

---

## Архитектура

```
Keenetic Router → wg-obfuscator (client) → :51821 → wg-obfuscator (server) → :51820 WireGuard → Internet
                                                                                    │
                                                                               10.25.0.x
                                                                                    │
                                                                          Web Panel :8443
                                                                          (Flask + Gunicorn)
```

- Протокол: WireGuard + wg-obfuscator (обфускация от DPI)
- Аутентификация: ключевые пары (нет паролей — только WireGuard ключи)
- Подсеть VPN: `10.25.0.0/16`
- Мониторинг сессий: каждые 30 сек (настраивается)

---

## Управление

```bash
# Phobos (VPN)
phobos                          # Интерактивное меню
systemctl status wg-quick@wg0   # WireGuard
systemctl status wg-obfuscator  # Обфускатор
wg show wg0                     # Активные peers

# Веб-панель
systemctl status phobos-panel
systemctl restart phobos-panel
journalctl -u phobos-panel -f
```

---

## Структура файлов

```
/opt/Phobos/
├── clients/           # Клиенты VPN (ключи, конфиги)
│   └── {name}/
│       ├── metadata.json
│       ├── {name}.conf
│       └── wg-obfuscator.conf
├── server/
│   ├── server.env     # Конфигурация сервера
│   └── wg-obfuscator.conf
└── repo/server/scripts/
    └── phobos-client.sh  # Управление клиентами

/opt/phobos-panel/
├── app.py             # Flask веб-панель
├── settings.json      # Настройки (пароль, Telegram, метки, сроки)
└── .secret_key        # Ключ сессии

/etc/wireguard/
└── wg0.conf           # WireGuard конфигурация
```

---

## Обновление панели

```bash
curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/app.py \
  > /opt/phobos-panel/app.py
systemctl restart phobos-panel
```


---

## Поддержка проекта

Если PCA Phobos оказался полезен — буду благодарен за поддержку:

- 💖 **Boosty:** [boosty.to/andrey27/donate](https://boosty.to/andrey27/donate)
- 💳 **Ozon Bank (СБП):** [ссылка](https://finance.ozon.ru/apps/sbp/ozonbankpay/019dc200-2a5d-7931-a619-782d285f6798)
- ✉️ **Telegram:** [@Iot_andrey](https://t.me/Iot_andrey) — вопросы, баги, идеи

## На основе и благодарности

PCA Phobos — это веб-панель и turnkey-инсталлятор поверх:

- [**Phobos**](https://github.com/Ground-Zerro/Phobos) (Ground-Zerro) — обфусцированный WireGuard. Поддержать автора: [Boosty](https://boosty.to/ground_zerro) ❤️
- [**WireGuard Easy**](https://github.com/wg-easy/wg-easy) (Emile Nijssen, AGPL-3.0) — веб-панель WireGuard, на которой основан Phobos. Поддержать автора: [GitHub Sponsors](https://github.com/sponsors/WeeJeWel) ❤️
- [**wg-obfuscator**](https://github.com/ClusterM/wg-obfuscator) (ClusterM) — обфускация WireGuard-трафика. Поддержать автора: [Boosty](https://boosty.to/cluster) ❤️

Спасибо авторам за отличные инструменты.
