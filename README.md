# HideWay VPN Bot

Telegram-бот управления Xray (VLESS+Reality+XHTTP) VPN с биллингом:
- **Free tier** — 5GB/мес в обмен на подписку на Telegram-канал (автопроверка + автоотзыв при отписке)
- **Paid tier** — $2.99/мес за 20GB трафика

## Changelog

### 0.0.2-beta-1 (pre-release)
- Новый статус `under_approve`: мигрированные из legacy юзеры ждут ручного апрува
  админом перед выдачей VPN (выдача заблокирована до `/admin_approve`).
- Переписана одноразовая миграция `scripts/migrate_legacy_json.py`:
  разворачивает `{"users": {...}}`, ставит `under_approve` (админ → `active`),
  корректно парсит `bans.json` (`until==0` → permanent), идемпотентна
  (непустая БД → skip, exit 0).
- Миграция интегрирована в `deploy.yml` (от `hidewaybot`, self-guard, не валит деплой).
- Новые админ-команды: `/admin_approve`, `/admin_list` (пагинация),
  `/admin_delete` (с подтверждением), `/admin_stats_full`, `/admin_help`.
- Версия проекта в `src/__init__.py` (`__version__`), показывается в `/admin_*`.

## Структура

src/
- bot_online.py — основной бот (команды, audit, rate-limit, биллинг)
- online_api.py — клиент Management API

systemd/
- hideway-bot.service

.github/workflows/
- deploy.yml — автодеплой на push в main

## Локальная разработка

1. cp .env.example .env и заполнить реальными значениями
2. python3 -m venv venv && source venv/bin/activate
3. pip install -r requirements.txt
4. python3 src/bot_outline.py

## Деплой

Push в main → GitHub Actions подключается по SSH к серверу → git pull → restart systemd service.

Требуемые GitHub Secrets:
- SERVER_HOST
- SERVER_USER
- SERVER_SSH_KEY
- SERVER_PORT

## Сервер

- Debian 12, VPS hide-vpn
- Бот — systemd service hideway-bot.service