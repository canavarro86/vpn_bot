# HideWay VPN Bot

Telegram-бот управления Xray (VLESS + Reality + XHTTP) VPN с биллингом.

- **Free tier** — 5 GB/мес в обмен на подписку на Telegram-канал (автопроверка + автоотзыв при отписке)
- **Paid tier** — $2.99/мес за 20 GB трафика

## Структура проекта

```
src/
├── config.py              # загрузка .env → Settings, фабрика платёжного провайдера
├── db/
│   ├── repository.py      # единственный файл с raw SQL (SQLite WAL)
│   └── schema.sql         # CREATE TABLE IF NOT EXISTS ...
├── vpn_engine/
│   ├── client.py          # create/delete/restore клиента Xray (config rewrite + reload)
│   └── usage_tracker.py   # xray api statsquery — дельты трафика
├── payments/
│   ├── provider.py        # абстрактный Protocol: create_invoice / verify_webhook
│   └── stub.py            # заглушка (PAYMENT_PROVIDER=stub)
└── bot/
    ├── app.py             # Bot + Dispatcher, DI, polling
    ├── handlers.py        # /start /status /get /upgrade + check_sub callback
    ├── admin_handlers.py  # /admin_* команды (фильтр IsAdmin)
    ├── middleware.py      # BanMiddleware + RateLimitMiddleware
    └── jobs.py            # фоновые задачи: подписки, paid_expiry, трафик, monthly_reset

scripts/
└── migrate_legacy_json.py # однократная миграция outline_keys.json + bans.json → SQLite

systemd/
└── hideway-bot.service    # unit-файл для production-деплоя

tests/
└── ...                    # pytest — repository, client, tracker, middleware, migrate
```

## Локальная разработка

```bash
cp .env.example .env          # заполнить TOKEN, ADMIN_USER_IDS, SERVER_PUBLIC_IP и т.д.
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt   # pytest, pytest-asyncio

python -m src.bot             # запустить бота
```

### Тесты

```bash
pytest tests/ -v
```

## Деплой (ручной)

**Деплой через GitHub Actions отключён по решению проекта.** Используется ручной деплой:

```bash
# На сервере:
cd /opt/vpn_bot
git pull origin main
./venv/bin/pip install -q --upgrade -r requirements.txt
sudo systemctl restart hideway-bot
sudo systemctl status hideway-bot
```

### Миграция legacy данных (один раз)

```bash
python scripts/migrate_legacy_json.py --data-dir /opt/vpn_bot/DB_ARH --dry-run
python scripts/migrate_legacy_json.py --data-dir /opt/vpn_bot/DB_ARH --apply
```

Скрипт идемпотентен: непустая БД → пропускает, exit 0.

## Конфигурация

Скопируйте `.env.example` в `.env` и заполните:

| Переменная | Обязательна | Описание |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✓ | токен от @BotFather |
| `ADMIN_USER_IDS` | ✓ | Telegram ID через запятую |
| `REQUIRED_CHANNEL_ID` | ✓ | `@username` или числовой ID |
| `SERVER_PUBLIC_IP` | ✓ | IP сервера для vless:// ссылки |
| `REALITY_PUBLIC_KEY` | ✓ | публичный ключ Reality |
| `REALITY_SNI` | ✓ | SNI (по умолч. `www.swift.com`) |
| `REALITY_SHORT_ID` | ✓ | short ID из xray keygen |
| `DATA_DIR` | — | `/opt/hideway-bot/data` |
| `PAYMENT_PROVIDER` | — | `stub` \| `cryptocloud` |

Полный список — в `.env.example`.

## Архитектурные ограничения

- **VPN-движок**: управление клиентами через переписывание JSON-конфига Xray + `systemctl reload xray`. gRPC HandlerService не используется (api-блок только для StatsService/трафика).
- **БД**: синхронный `sqlite3` (WAL). `Repository` — единственное место с SQL. Вызывается из хендлеров через `asyncio.to_thread`.
- **Статистика трафика**: `xray api statsquery` (subprocess). При недоступности api-блока деградирует: `get_usage` возвращает 0.
- **Rate limit**: в памяти процесса — сбрасывается при рестарте. Баны — в БД (persist).

## Changelog

### 0.0.2-beta-1
- Статус `under_approve`: мигрированные пользователи ждут ручного `/admin_approve` перед выдачей VPN.
- Реализована миграция `scripts/migrate_legacy_json.py` (идемпотентная, dry-run/--apply).
- Новые команды: `/admin_approve`, `/admin_list`, `/admin_delete`, `/admin_stats_full`, `/admin_help`.
- Версия в `src/__init__.py`, отображается в `/admin_*`.
