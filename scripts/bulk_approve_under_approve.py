#!/usr/bin/env python3
"""Разовый массовый апрув legacy-пользователей (status = under_approve).

НЕ часть постоянно работающего бота — запускается вручную один раз после
миграции legacy JSON (scripts/migrate_legacy_json.py).

Для каждого under_approve пользователя ПОСЛЕДОВАТЕЛЬНО:
  1. status → pending_subscription (иначе activate_free откажет: under_approve
     не входит в handlers._PROVISIONABLE);
  2. handlers.activate_free() — создаёт VLESS-клиента в Xray, пишет БД,
     переводит в active и возвращает access_url;
  3. уведомление пользователю: текст + ссылка/QR тем же handlers._send_link,
     что и у /get.

Последовательно (не gather) намеренно: Xray-конфиг переписывается целиком при
каждом create_client — параллельные вставки затирали бы друг друга; плюс
не упираемся в flood-лимиты Telegram.

Обработка ошибок:
  - провижининг не удался → пользователю НИЧЕГО не шлём, статус возвращаем
    в under_approve (останется в админ-панели, скрипт можно перезапустить),
    audit "bulk_approve_failed", stdout "FAIL {tid}: причина";
  - провижининг ок, но уведомление не доставлено (юзер заблокировал бота и
    т.п.) → провижининг НЕ откатываем (клиент уже создан, ссылку он получит
    по /get), audit "notify_failed", stdout "NOTIFY_FAIL {tid}: причина".

Идемпотентность: работает только по status = under_approve. После успешного
прогона таких пользователей не остаётся → повторный запуск ничего не делает
и повторных уведомлений не шлёт.

Запуск:
    python scripts/bulk_approve_under_approve.py --dry-run
    python scripts/bulk_approve_under_approve.py --limit 3
    python scripts/bulk_approve_under_approve.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Позволяет запуск как `python scripts/bulk_approve_under_approve.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.exceptions import TelegramAPIError  # noqa: E402

from src.bot import handlers as user_handlers  # noqa: E402
from src.config import Settings, load_settings  # noqa: E402
from src.db import repository as repo_mod  # noqa: E402
from src.db.repository import Repository  # noqa: E402

log = logging.getLogger("bulk_approve")

AUDIT_SOURCE = "bulk_approve_under_approve"
SEND_DELAY_SEC = 0.15  # пауза между пользователями


class _BotChat:
    """Мини-адаптер `Message` для одного чата.

    handlers._send_link принимает Message и зовёт .answer / .answer_photo;
    у скрипта Message нет, только Bot + chat_id. Адаптер даёт ровно эти два
    метода, чтобы переиспользовать _send_link (QR + фолбэк на текст) как есть,
    не дублируя его логику.
    """

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def answer(self, text: str, **kwargs):
        return await self._bot.send_message(self._chat_id, text, **kwargs)

    async def answer_photo(self, photo, **kwargs):
        return await self._bot.send_photo(self._chat_id, photo, **kwargs)


def _approved_text(settings: Settings) -> str:
    return (
        "🎉 Ваша заявка на HideWay VPN одобрена! Вам доступен бесплатный тариф "
        f"{settings.free_tier_gb:.0f} GB/мес. Ваш ключ доступа ниже 👇"
    )


async def bulk_approve(
    bot: Bot,
    repo: Repository,
    settings: Settings,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Массовый апрув. Возвращает счётчики {ok, fail, notify_fail, total}."""
    targets = await asyncio.to_thread(
        repo.list_users_by_status, repo_mod.STATUS_UNDER_APPROVE
    )
    if not targets:
        print("Пользователей под approve не найдено.")
        return {"ok": 0, "fail": 0, "notify_fail": 0, "total": 0}

    if limit is not None:
        targets = targets[:limit]

    if dry_run:
        print(f"DRY-RUN: будет обработано {len(targets)} пользователей:")
        for u in targets:
            print(u.telegram_id)
        print("DRY-RUN: БД не изменена, сообщения не отправлены.")
        return {"ok": 0, "fail": 0, "notify_fail": 0, "total": len(targets)}

    ok = fail = notify_fail = 0

    for u in targets:
        tid = u.telegram_id

        # under_approve не провижинится — переводим в pending перед activate_free
        await asyncio.to_thread(repo.set_status, tid, repo_mod.STATUS_PENDING)
        try:
            provisioned, url = await user_handlers.activate_free(
                bot, repo, settings, tid
            )
        except Exception as e:  # VpnEngineError, ошибки записи конфига и т.п.
            provisioned, url = False, None
            reason = f"{type(e).__name__}: {e}"
        else:
            reason = "activate_free вернул ok=False" if not provisioned else ""
            if provisioned and not url:
                provisioned, reason = False, "activate_free не вернул access_url"

        if not provisioned or not url:
            # клиент не создан — статус обратно в under_approve, чтобы юзер
            # остался в админ-панели и скрипт можно было перезапустить
            await asyncio.to_thread(
                repo.set_status, tid, repo_mod.STATUS_UNDER_APPROVE
            )
            await asyncio.to_thread(
                repo.audit, "bulk_approve_failed", tid,
                {"source": AUDIT_SOURCE, "reason": reason},
            )
            print(f"FAIL {tid}: {reason}")
            fail += 1
            await asyncio.sleep(SEND_DELAY_SEC)
            continue

        # клиент создан; дальше только доставка — откатов уже не делаем
        try:
            chat = _BotChat(bot, tid)
            await chat.answer(_approved_text(settings))
            await user_handlers._send_link(chat, url)
        except TelegramAPIError as e:
            # заблокировал бота / чат не найден / flood — как в admin_broadcast
            reason = f"{type(e).__name__}: {e}"
            await asyncio.to_thread(
                repo.audit, "notify_failed", tid,
                {"source": AUDIT_SOURCE, "reason": reason},
            )
            print(f"NOTIFY_FAIL {tid}: {reason}")
            notify_fail += 1
        else:
            await asyncio.to_thread(
                repo.audit, "bulk_approved", tid, {"source": AUDIT_SOURCE}
            )
            print(f"OK {tid}")
            ok += 1

        await asyncio.sleep(SEND_DELAY_SEC)

    print(
        f"Готово: {ok} одобрено и уведомлено, {fail} провижининг не удался, "
        f"{notify_fail} провижининг ок но уведомление не доставлено."
    )
    return {"ok": ok, "fail": fail, "notify_fail": notify_fail, "total": len(targets)}


async def _main(args: argparse.Namespace) -> int:
    settings = load_settings()
    repo = Repository(settings.db_path)
    repo.init_schema()
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        await bulk_approve(
            bot, repo, settings, dry_run=args.dry_run, limit=args.limit
        )
    finally:
        await bot.session.close()
        repo.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Массовый апрув under_approve → active + рассылка ключей"
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="только показать telegram_id, ничего не менять и не отправлять",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="обработать только первых N (для тестового прогона)",
    )
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
