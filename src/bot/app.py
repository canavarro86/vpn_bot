"""Точка входа бота: сборка Bot + Dispatcher, DI, запуск polling.

Запуск: `python -m src.bot` (см. __main__.py) или `python -m src.bot.app`.
systemd ExecStart использует этот модуль (фаза 7).

Роутеры/мидлвары/джобы подключаются по мере готовности фаз:
  - user router (handlers)        — есть
  - admin router (admin_handlers) — фаза 5
  - middleware (rate-limit/ban)   — фаза 5
  - jobs (фоновые задачи)         — фаза 6
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from ..config import Settings, get_payment_provider, load_settings
from ..db.repository import Repository
from . import admin_handlers, handlers, jobs
from .middleware import BanMiddleware, RateLimitMiddleware

log = logging.getLogger(__name__)

# Меню бота (кнопка "/" в клиенте Telegram). Списки команд живут рядом
# с хендлерами, здесь только превращаем их в BotCommand.
USER_COMMANDS = [BotCommand(command=c, description=d) for c, d in handlers.USER_COMMANDS]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command=c, description=d) for c, d in admin_handlers.ADMIN_COMMANDS
]


async def setup_bot_commands(bot: Bot, settings: Settings) -> None:
    """Ставит общее меню команд и расширенное — персонально каждому админу.

    Личный scope админа перекрывает default только в его чате с ботом.
    Если админ ещё не писал боту, Telegram отвечает ошибкой — она не должна
    ронять старт, поэтому ловим и логируем."""
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in settings.admin_user_ids:
        try:
            await bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            log.warning("set_my_commands для админа %s не удался: %s", admin_id, e)


def build_dispatcher(settings: Settings, repo: Repository, payments=None) -> Dispatcher:
    """Собирает Dispatcher и кладёт зависимости в workflow data —
    aiogram прокидывает их в хендлеры по имени параметра.

    `payments` можно передать снаружи, чтобы хендлеры и фоновая задача
    check_pending_payments работали с одним экземпляром провайдера."""
    dp = Dispatcher()
    dp["repo"] = repo
    dp["settings"] = settings
    dp["payments"] = payments if payments is not None else get_payment_provider(settings)

    # мидлвары: сначала бан, затем rate-limit (порядок важен)
    for observer in (dp.message, dp.callback_query):
        observer.middleware(BanMiddleware())
        observer.middleware(RateLimitMiddleware(settings))

    # admin router первым: его IsAdmin-фильтр отсекает не-админов,
    # пользовательские команды (/status и т.п.) у админа тоже работают через user router
    dp.include_router(admin_handlers.router)
    dp.include_router(handlers.router)
    return dp


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    repo = Repository(settings.db_path)
    repo.init_schema()

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    payments = get_payment_provider(settings)
    dp = build_dispatcher(settings, repo, payments)

    log.info(
        "HideWay bot запускается (polling), платёжный провайдер: %s", payments.name
    )
    await setup_bot_commands(bot, settings)
    job_tasks = jobs.start_jobs(bot, repo, settings, payments)
    try:
        await dp.start_polling(bot)
    finally:
        for t in job_tasks:
            t.cancel()
        await asyncio.gather(*job_tasks, return_exceptions=True)
        await bot.session.close()
        repo.close()


if __name__ == "__main__":
    asyncio.run(main())
