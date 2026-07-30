# HideWay VPN Bot — Техническое задание (краткое)

> Восстановлен по коду. Исходный ТЗ-документ утрачен.  
> Все ссылки «ТЗ раздел N» в комментариях кода относятся к разделам этого файла.

## Раздел 1. VPN-движок (Xray)

Протокол: VLESS + Reality + XHTTP, порт 443, один inbound.

Управление клиентами — **переписывание JSON-конфига + `systemctl reload xray`** (не gRPC HandlerService, который требует серверного api-блока с HandlerService). Смена протокола при следующей блокировке затрагивает только `src/vpn_engine/`, бизнес-логика бота не меняется.

Статистика трафика — `xray api statsquery` (StatsService, read-only). Требует блока `api` в конфиге Xray и `policy.stats.statsUserUplink/Downlink: true`. При недоступности деградирует (возвращает 0).

Email-тег клиента в конфиге: `{telegram_id}@hideway`. По этому тегу StatsService отдаёт счётчики трафика.

Ограничение `xtls-rprx-vision` несовместимо с XHTTP — поле `flow` у клиентов не выставляется.

## Раздел 2. Бот (aiogram 3)

### Команды пользователя
- `/start` — регистрация / приветствие. Если подписан на канал — сразу выдаёт vless://. Мигрированные (`under_approve`) ждут ручного апрува.
- `/status` — текущий тариф, трафик, срок оплаты.
- `/get` — повторная выдача ссылки или попытка активации.
- `/upgrade` — запрос оплаты (инвойс через PaymentProvider).
- `/check_subscription` — явная проверка подписки + выдача.

### Команды администратора (только `ADMIN_USER_IDS`)
- `/admin_stats` / `/admin_stats_full` — статистика.
- `/admin_find <id>` — карточка пользователя.
- `/admin_list [статус] [стр]` — список с пагинацией.
- `/admin_approve <id>` — `under_approve` → `pending_subscription`.
- `/admin_ban <id> [причина]` — permanent ban + отзыв VPN.
- `/admin_unban <id>` — снять бан → `revoked`.
- `/admin_revoke <id>` — отозвать VPN-клиент.
- `/admin_delete <id> [confirm]` — полное удаление из БД.
- `/admin_grant_paid <id> <дни>` — выдать paid-тариф.
- `/admin_broadcast <текст>` — рассылка активным.
- `/admin_help` — справка.

Состав администраторов — только через `.env` (`ADMIN_USER_IDS`) + restart. Runtime-добавления нет.

## Раздел 3. Антиабьюз / rate-limit

`RateLimitMiddleware`: не более `RATE_LIMIT_MAX` запросов за `RATE_LIMIT_WINDOW` секунд. При `BAN_TRIGGER` нарушениях за `BAN_WINDOW` секунд — авто-бан на `BAN_SECONDS` (запись в таблицу `bans`).

`BanMiddleware` дропает апдейты от забаненных (проверка по `bans.expires_at`). Администраторы оба мидлвара пропускают.

Состояние `_hits` / `_violations` — в памяти, сбрасывается при рестарте. Баны в БД — persist.

## Раздел 4. Privacy

`connection_log.ip_address` — опциональное поле, пишется только если передан. Агрегированный сэмпл байт, без per-domain истории. Хранение по решению оператора.

## Раздел 5. Платёжный шлюз

Абстракция `PaymentProvider` (`src/payments/provider.py`). Текущая реализация: `StubProvider` (`PAYMENT_PROVIDER=stub`) — инвойс не создаётся, пользователь получает сообщение об оплате через администратора.

Подключение реального шлюза: создать `src/payments/cryptocloud.py` с тем же Protocol, выставить `PAYMENT_PROVIDER=cryptocloud`.

## Раздел 6. Фоновые задачи

- `check_subscriptions` (24 ч) — отписавшимся free-active → revoke + уведомление.
- `check_paid_expiry` (24 ч) — истёкшим paid: если подписан → free, иначе revoke.
- `sample_traffic` (`TRAFFIC_SAMPLE_INTERVAL_SECONDS`, по умолч. 300 с) — дельта трафика, лимиты, уведомления.
- `monthly_reset` (24 ч, 1-го числа) — обнуление счётчиков + восстановление заблокированных по лимиту. Защита от повтора через `audit_log`.

## Раздел 7. Конфигурация (.env)

Обязательные: `TELEGRAM_BOT_TOKEN`, `ADMIN_USER_IDS`, `SERVER_PUBLIC_IP`, `REALITY_PUBLIC_KEY`, `REALITY_SNI`, `REALITY_SHORT_ID`.

Опциональные с дефолтами: `DATA_DIR=/opt/hideway-bot/data`, `FREE_TIER_GB=5`, `PAID_TIER_GB=20`, `PAID_TIER_PRICE_USD=2.99`, `RATE_LIMIT_MAX=5`, `RATE_LIMIT_WINDOW=60`, `BAN_TRIGGER=3`, `BAN_WINDOW=60`, `BAN_SECONDS=3600`, `TRAFFIC_SAMPLE_INTERVAL_SECONDS=300`.

## Раздел 8. База данных

SQLite, WAL mode. Таблицы: `users`, `payments`, `connection_log`, `bans`, `audit_log`. Схема в `src/db/schema.sql`. `Repository` — единственный класс с raw SQL.

## Раздел 9. Деплой

Ручной: `git pull` → `pip install -r requirements.txt` → `systemctl restart hideway-bot`.

GitHub Actions отключён (CI не используется по проектному решению).

Миграция legacy JSON — одноразовый скрипт `scripts/migrate_legacy_json.py --apply` (идемпотентен).

## Раздел 10. Канал

`REQUIRED_CHANNEL_ID` — числовой ID или `@username`. Если не задан — проверка подписки пропускается (бот работает в режиме без канала).
