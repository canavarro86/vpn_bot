"""Мидлвары: проверка бана и rate-limit / авто-бан (ТЗ раздел 3, антиабьюз).

Порядок регистрации (важен): сначала BanMiddleware (отсечь забаненных), затем
RateLimitMiddleware. Админы (ADMIN_USER_IDS) проходят обе без ограничений.

Параметры берутся из Settings (RATE_LIMIT_MAX/WINDOW, BAN_TRIGGER/WINDOW/SECONDS).
Состояние rate-limit — в памяти процесса (deque таймстампов); при рестарте
обнуляется, это приемлемо для антиабьюза. Постоянные баны — в БД.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from ..config import Settings
from ..db.repository import Repository

log = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def _user(data: dict[str, Any]) -> User | None:
    return data.get("event_from_user")


class BanMiddleware(BaseMiddleware):
    """Дропает апдейты от забаненных пользователей (БД bans, с учётом expires_at)."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        settings: Settings = data["settings"]
        repo: Repository = data["repo"]
        user = _user(data)
        if user is not None and not settings.is_admin(user.id):
            if await asyncio.to_thread(repo.is_banned, user.id):
                log.info("drop event от забаненного %s", user.id)
                return None
        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    """Лимит команд за окно; при повторных превышениях — авто-бан в БД."""

    def __init__(self, settings: Settings):
        self.max = settings.rate_limit_max
        self.window = settings.rate_limit_window
        self.ban_trigger = settings.ban_trigger
        self.ban_window = settings.ban_window
        self.ban_seconds = settings.ban_seconds
        self._hits: dict[int, deque[float]] = defaultdict(deque)
        self._violations: dict[int, deque[float]] = defaultdict(deque)

    @staticmethod
    def _purge(dq: deque[float], now: float, window: float) -> None:
        while dq and now - dq[0] > window:
            dq.popleft()

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        settings: Settings = data["settings"]
        repo: Repository = data["repo"]
        user = _user(data)
        if user is None or settings.is_admin(user.id):
            return await handler(event, data)

        now = time.monotonic()
        hits = self._hits[user.id]
        self._purge(hits, now, self.window)
        hits.append(now)

        if len(hits) <= self.max:
            return await handler(event, data)

        # превышение лимита — фиксируем нарушение
        viol = self._violations[user.id]
        self._purge(viol, now, self.ban_window)
        viol.append(now)
        log.info("rate-limit: user %s превысил (%d hits, %d нарушений)", user.id, len(hits), len(viol))

        if len(viol) >= self.ban_trigger:
            expires = int(time.time()) + self.ban_seconds
            await asyncio.to_thread(repo.add_ban, user.id, "rate-limit auto-ban", expires)
            await asyncio.to_thread(
                repo.audit, "auto_banned", user.id,
                {"reason": "rate-limit", "ban_seconds": self.ban_seconds},
            )
            viol.clear()
            hits.clear()
            log.warning("auto-ban user %s на %d сек", user.id, self.ban_seconds)
        # апдейт дропаем (не зовём handler)
        return None
