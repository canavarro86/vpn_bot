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

from ..config import Settings, get_payment_provider, load_settings
from ..db.repository import Repository
from . import admin_handlers, handlers, jobs
from .middleware import BanMiddleware, RateLimitMiddleware


def build_dispatcher(settings: Settings, repo: Repository) -> Dispatcher:
    """Собирает Dispatcher и кладёт зависимости в workflow data —
    aiogram прокидывает их в хендлеры по имени параметра."""
    dp = Dispatcher()
    dp["repo"] = repo
    dp["settings"] = settings
    dp["payments"] = get_payment_provider(settings)

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
    dp = build_dispatcher(settings, repo)

    logging.getLogger(__name__).info("HideWay bot запускается (polling)…")
    job_tasks = jobs.start_jobs(bot, repo, settings)
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
