# HideWay VPN Bot

Telegram-бот управления Outline VPN с биллингом:
- **Free tier** — 5GB/мес в обмен на подписку на Telegram-канал (автопроверка + автоотзыв при отписке)
- **Paid tier** — $2.99/мес за 20GB трафика

## Структура

src/
- bot_outline.py — основной бот (команды, audit, rate-limit, биллинг)
- outline_api.py — клиент Outline Management API

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

- Debian 12 (bookworm), VPS hide-vpn (бывш. rebis-hub)
- Outline VPN в Docker (shadowbox)
- Бот — systemd service hideway-bot.service