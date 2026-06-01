# PCA Phobos — VPN, который не отключается

**Веб-панель и turnkey-установщик для [Phobos](https://github.com/Ground-Zerro/Phobos)** — обфусцированного WireGuard VPN. Ставится на чистый VPS одной командой, управляет десятками роутеров и устройств с одной страницы, сам переключается между серверами при сбоях и восстанавливается после перезагрузок.

<p align="center">
  <img src="docs/img/sessions.png" alt="Панель — активные сессии" width="850">
</p>

---

## Что это простыми словами

Обычный VPN падает, когда падает сервер или провайдер банит IP. **PCA Phobos — нет.**

- 🌐 **Маскированный WireGuard** — трафик не отличить от обычного, проходит мимо DPI и блокировок по сигнатуре.
- 🔁 **Авто-переключение между серверами** — сервер лёг или порт прикрыли → роутер сам прыгает на резервный за ~10–15 секунд, без твоего участия.
- 📡 **Работает за любым NAT** — серый IP, мобильный интернет, домашний роутер за провайдером. Управление едет внутри самого защищённого туннеля, поэтому бан публичного IP ему не страшен.
- 🖥️ **Одна команда — новое устройство** — создал клиента в панели → вставил команду на роутер → готово.
- ♻️ **Само-восстановление** — роутер перезагрузился, провайдер сменил IP, сервис отвалился → система поднимет всё обратно сама.
- 📱 **Телефоны** — Android (PhobosWG, с обфускацией) и iPhone (обычный WireGuard) через QR-код.

---

## Возможности

- **Активные сессии** — VPN IP, Real IP, метка, handshake, трафик RX/TX, Kick.
- **Клиенты VPN** — добавление/удаление, статус online (по handshake на любом сервере), назначение сервера.
- **Мульти-сервер + failover** — приоритеты, балансировка по нагрузке (CPU/RAM), резервные серверы; роутеры переключаются автоматически.
- **NAT-friendly управление** — роутеры сами тянут конфиг по туннелю (`10.25.0.1`), переживает бан публичного IP.
- **Авто-восстановление роутеров** — серверный сторож (watchdog) поднимает роутер после неудачной перезагрузки через KeenDNS.
- **Конфиги для телефона** — 📱 Android `phobos://` + QR, 🍎 iPhone обычный WireGuard + QR.
- **Метки** — имя объекта по IP (видно в сессиях и Telegram).
- **Срок подписки** — дата окончания, обратный отсчёт, автокик + предупреждения.
- **Telegram-уведомления** — подключение/отключение, сервер вверх/вниз, истечение подписки, авто-восстановление.
- **Двуязычный интерфейс** RU/EN + подсказки `?` у каждого действия.
- **Версии и обновления** — обновление и откат прямо из панели.
- **Уведомления per-client** — 🔔 у каждого клиента: включить/выключить Telegram-оповещения о подключении/отключении.
- **Telegram-бот** — управление клиентами командами прямо из чата:
  - `/add <имя>` — создать клиента (бот пришлёт команду установки)
  - `/del <имя>` — удалить клиента
  - `/list` — список клиентов
  - **Кнопки** — постоянная клавиатура: ➕ Создать клиента · 📋 Список · 🗑 Удалить (инлайн-выбор) · 📊 Статус · ❓ Помощь.
  (Работает только для чата из настроек `tg_chat_id`.)

---

## Скриншоты

| Сессии | Клиенты | Серверы |
|--------|---------|---------|
| ![Сессии](docs/img/sessions.png) | ![Клиенты](docs/img/clients.png) | ![Серверы](docs/img/servers.png) |

> Если картинки не отображаются — положи свои PNG в `docs/img/` (`sessions.png`, `clients.png`, `servers.png`).

---

## Быстрый старт (с чистого VPS, одной командой)

Ставит **всё** со всеми зависимостями: wg-obfuscator, WireGuard, обфускатор-сервисы, веб-панель, nginx, скрипты онбординга роутеров, сторож авто-восстановления. Предустановленный Phobos **не требуется**.

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/install.sh)
```

### С параметрами

```bash
PANEL_PASS=МойПароль TG_TOKEN=123:abc TG_CHAT=123456789 \
bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/install.sh)
```

| Переменная        | По умолчанию    | Описание |
|-------------------|-----------------|----------|
| `PANEL_PASS`      | `OcAdmin2026!`  | Пароль панели (логин `admin`) |
| `PANEL_PORT`      | случайный       | Порт веб-панели |
| `API_KEY`         | случайный       | Общий ключ (агенты + pull-токен роутеров) |
| `OBF_PORTS`       | `2083,5443,993` | Порты обфускатора |
| `TG_TOKEN`/`TG_CHAT` | пусто        | Telegram-уведомления |
| `ALLOW_PLAIN_WG`  | пусто           | `=1` открыть порт 51820 для iOS WireGuard (без обфускации) |
| `CHANNEL`         | `stable`        | канал: `stable` (открытый) · `beta`/`dev` (под ключом `PHOBOS_KEY`) |

После установки панель напечатает адрес, логин, пароль и **API key** — сохрани их.

> **Каналы:**
> - **stable** — стабильная (ветка `main`), ставится **без ключа**, по умолчанию.
> - **beta** — кандидат в релиз (тест), **под ключом** подписчика.
> - **dev** — активная разработка (нестабильно), **под ключом** подписчика.
>
> Ключ `PHOBOS_KEY` выдаётся по подписке [Boosty](https://boosty.to/andrey27). Закрытый канал:
> ```bash
> PHOBOS_KEY=ваш_ключ CHANNEL=dev bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/install.sh)
> ```
> Переключение после установки: `phobos-update stable` (без ключа) · `PHOBOS_KEY=... phobos-update beta|dev` · статус: `phobos-update --check`.

### Вторичный сервер (для failover / балансировки)

```bash
MAIN_SERVER=<ip_основного> MAIN_API_KEY=<API key из основного> \
bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/server/secondary-setup.sh)
```

Затем в панели → **Серверы** → добавь его в список.

---

## Клиенты для телефона

В таблице **Клиенты** у каждого клиента есть кнопки:

- 📱 **Android** — `phobos://`-ссылка + QR для приложения **PhobosWG** (с обфускацией). Скан QR → импорт.
- 🍎 **iPhone / iOS** — обычный WireGuard-конфиг + QR для официального WireGuard (**без** обфускации). Требует открытого порта 51820 (`ALLOW_PLAIN_WG=1` при установке).

> Для каждого устройства создавай **отдельного клиента** (свой ключ и IP). Один конфиг на двух устройствах = конфликт ключей.

**Приложения:**
- **Android (PhobosWG):** на странице Android-клиента в панели есть кнопка **«Скачать приложение PhobosWG (APK)»** — приложение скачивается автоматически из public stable release (доступно всем каналам: stable/beta/dev). Установи APK, затем импортируй `phobos://`-ссылку или QR.
- **iPhone (iOS):** официальный [WireGuard из App Store](https://apps.apple.com/app/wireguard/id1441195209) — кнопка есть на странице iOS-клиента.

---

## Обновление и откат

Проект версионируется (см. файл `VERSION` и git-теги `vX.Y.Z`).

**Из панели:** Настройки → «Версия и обновления» → **Обновить** / **Откатить**.

**Из консоли:**

```bash
phobos-update              # обновить до последней версии
phobos-update v1.1.0       # обновить до конкретной версии (git tag)
phobos-update --rollback   # откатить на предыдущую версию
phobos-update --version    # показать установленную и доступную версию
phobos-update --list       # список бэкапов
```

Обновляется только слой панели и скриптов. **Ключи WireGuard, `server.env` и клиенты не трогаются.** Перед каждым обновлением делается бэкап → откат восстанавливает его.

---

## Полное удаление сервера

Одной командой (спросит подтверждение):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/uninstall.sh)
```

Без подтверждения: `PURGE=1 bash <(curl -fsSL https://raw.githubusercontent.com/andrey271192/PCA_Phobos/main/uninstall.sh)`

Удаляет: веб-панель, обфускатор-сервисы, WireGuard `wg0`, агент, nginx-сайт, watchdog-cron, iptables-правила, бинарники (`wg-obfuscator`, `phobos-update`) и каталоги `/opt/Phobos`, `/opt/phobos-panel`. Общие пакеты (wireguard, nginx, cron) НЕ трогает.

**Удаление с роутера** (на самом роутере): `/opt/etc/Phobos/phobos-uninstall.sh`

---

## Возможные проблемы и решения

Подробный разбор вынесен в отдельный файл:

**[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — установка сервера, роутеры, Android/iOS, secondary API, failover, Telegram, обновление, удаление, логи и что писать в issue.

Самые частые быстрые проверки:

```bash
phobos-update stable
systemctl status phobos-panel --no-pager
journalctl -u phobos-panel -n 80 --no-pager
cat /opt/phobos-panel/.port
```

На роутере:

```sh
/opt/etc/init.d/S49wg-obfuscator status
tail -n 80 /opt/etc/Phobos/health.log
grep '^SERVER_' /opt/etc/Phobos/failover.conf
```

---

## Архитектура

```
Телефон/Роутер → wg-obfuscator (клиент) ──обфускация──► сервер :2083/5443/993
                                                              │ де-обфускация
                                                         WireGuard :51820
                                                              │ 10.25.0.x
                                                   ┌──────────┴──────────┐
                                              Веб-панель            Failover на
                                              (Flask+Gunicorn)      резервные серверы
```

- Протокол: **WireGuard + wg-obfuscator** (обфускация от DPI).
- Управление роутерами — по **туннелю** (`10.25.0.1`), устойчиво к бану публичного IP.
- Состояние: JSON-файлы (без БД).

---

## Поддержка проекта

Если PCA Phobos оказался полезен — буду благодарен за поддержку:

- 💖 **Boosty:** [boosty.to/andrey27/donate](https://boosty.to/andrey27/donate)
- 💳 **Ozon Bank (СБП):** [ссылка](https://finance.ozon.ru/apps/sbp/ozonbankpay/019dc200-2a5d-7931-a619-782d285f6798)
- ✉️ **Telegram:** [@PCAdministration](https://t.me/PCAdministration) — вопросы, баги, идеи

## На основе и благодарности

PCA Phobos — это веб-панель и turnkey-инсталлятор поверх отличных открытых проектов:

- [**Phobos**](https://github.com/Ground-Zerro/Phobos) — обфусцированный WireGuard, автор **Ground_Zerro**. Поддержать: [Boosty](https://boosty.to/ground_zerro) ❤️
- [**WireGuard Easy**](https://github.com/wg-easy/wg-easy) — веб-панель WireGuard (AGPL-3.0), автор **Emile Nijssen**. Поддержать: [GitHub Sponsors](https://github.com/sponsors/WeeJeWel) ❤️
- [**wg-obfuscator**](https://github.com/ClusterM/wg-obfuscator) — обфускация WireGuard-трафика (GPL-3.0), автор **ClusterM**. Поддержать: [Boosty](https://boosty.to/cluster) ❤️

Спасибо авторам за отличные инструменты.
