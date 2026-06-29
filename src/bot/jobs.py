"""Фоновые задачи (ТЗ разделы 3, 6).

Запускаются как asyncio-таски при старте бота (см. app.py), без внешнего
планировщика. Каждая задача — бесконечный цикл с интервалом и защитой от
исключений (ошибка одной итерации не роняет цикл).

Задачи:
  - check_subscriptions  (24ч)   — отписался от канала → revoke + уведомление
  - check_paid_expiry    (24ч)   — истёк paid → откат на free (если подписан) либо revoke
  - sample_traffic       (interval) — снять дельту трафика, лимиты + уведомления
  - monthly_reset        (24ч)   — 1-го числа обнулить трафик периода + вернуть заблокированных

Логика лимитов трафика — здесь (на стороне бота), не в Xray (ТЗ раздел 1, 3).

Трафик считается дельтами: usage_tracker.sample_all(reset=True) обнуляет
счётчики Xray на каждом сэмпле, дельта прибавляется к users.traffic_used_bytes.
Так users.traffic_used_bytes = потрачено в текущем расчётном периоде.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot

from ..config import Settings
from ..db import repository as repo_mod
from ..db.repository import Repository, User
from ..vpn_engine import client as vpn_client
from ..vpn_engine import usage_tracker
from . import handlers as user_handlers

log = logging.getLogger(__name__)

GB = 1_000_000_000
MB = 1_000_000
DAY = 86_400


async def _notify(bot: Bot, telegram_id: int, text: str) -> None:
    try:
        await bot.send_message(telegram_id, text)
    except Exception as e:  # заблокировал бота / удалён — не критично
        log.debug("notify %s не удалось: %s", telegram_id, e)


# ============================ задачи ============================

async def check_subscriptions(bot: Bot, repo: Repository, settings: Settings) -> None:
    """Ре-проверка подписки на канал: отписавшимся active-free → revoke."""
    users = await asyncio.to_thread(repo.list_active_with_client)
    for u in users:
        if u.tier == repo_mod.TIER_PAID:
            continue  # paid не зависит от подписки
        if await user_handlers.is_subscribed(bot, settings, u.telegram_id):
            continue
        await user_handlers.revoke_user(repo, settings, u, reason="unsubscribed")
        await _notify(
            bot, u.telegram_id,
            "⚠️ Вы отписались от канала — бесплатный доступ отозван. "
            "Подпишитесь снова и нажмите /start для восстановления.",
        )
        log.info("revoke %s: отписался от канала", u.telegram_id)


async def check_paid_expiry(bot: Bot, repo: Repository, settings: Settings) -> None:
    """Истёкшие paid: если подписан на канал — откат на free, иначе revoke."""
    expired = await asyncio.to_thread(repo.list_paid_expired)
    for u in expired:
        if await user_handlers.is_subscribed(bot, settings, u.telegram_id):
            await asyncio.to_thread(
                repo.set_tier, u.telegram_id, repo_mod.TIER_FREE,
                settings.free_tier_gb, None,
            )
            await asyncio.to_thread(repo.reset_traffic_period, u.telegram_id)
            # клиент мог быть заблокирован по лимиту paid — вернём
            if u.vpn_client_id:
                await asyncio.to_thread(
                    vpn_client.restore_client, u.telegram_id, u.vpn_client_id, settings
                )
            await asyncio.to_thread(repo.audit, "paid_expired_to_free", u.telegram_id)
            await _notify(
                bot, u.telegram_id,
                f"⏳ Платный период истёк. Вы переведены на бесплатный тариф "
                f"({settings.free_tier_gb:.0f} GB). /upgrade — продлить.",
            )
        else:
            await user_handlers.revoke_user(repo, settings, u, reason="paid_expired_no_sub")
            await _notify(
                bot, u.telegram_id,
                "⏳ Платный период истёк, подписки на канал нет — доступ отозван. "
                "/start для восстановления.",
            )


async def sample_traffic(bot: Bot, repo: Repository, settings: Settings) -> None:
    """Снимает дельту трафика по всем клиентам, обновляет БД, применяет лимиты."""
    per_email = await asyncio.to_thread(usage_tracker.sample_all, settings, True)
    if not per_email:
        return
    warn_bytes = settings.low_traffic_warning_mb * MB
    for email, delta in per_email.items():
        tid = usage_tracker.telegram_id_from_email(email)
        if tid is None or delta <= 0:
            continue
        user = await asyncio.to_thread(repo.get_user, tid)
        if user is None:
            continue

        prev = user.traffic_used_bytes
        new = prev + delta
        await asyncio.to_thread(repo.add_traffic, tid, delta)
        await asyncio.to_thread(repo.log_connection, tid, delta, None)

        limit_bytes = int(user.traffic_limit_gb * GB)
        warn_at = limit_bytes - warn_bytes

        # достиг лимита (пересёк в этом сэмпле) → блок новых подключений
        if prev < limit_bytes <= new:
            if user.vpn_client_id:
                try:
                    await asyncio.to_thread(
                        vpn_client.delete_client, user.vpn_client_id, settings
                    )
                except vpn_client.VpnEngineError as e:
                    log.error("блокировка по лимиту %s не удалась: %s", tid, e)
            await asyncio.to_thread(
                repo.audit, "traffic_limit_blocked", tid, {"used": new, "limit": limit_bytes}
            )
            await _notify(
                bot, tid,
                "🚫 Лимит трафика исчерпан. Доступ заблокирован до следующего "
                "расчётного периода. /upgrade — больше трафика.",
            )
        # остался ~предупредительный порог → одно уведомление за период
        elif prev < warn_at <= new and not user.low_traffic_notified:
            await asyncio.to_thread(repo.set_low_traffic_notified, tid, 1)
            await _notify(
                bot, tid,
                f"⚠️ Осталось ~{settings.low_traffic_warning_mb:.0f}MB трафика. "
                "По достижении лимита доступ заблокируется. /upgrade — продлить.",
            )


async def monthly_reset(bot: Bot, repo: Repository, settings: Settings) -> None:
    """1-го числа месяца обнуляет трафик периода и возвращает заблокированных
    по лимиту клиентов. Защита от повтора в тот же день — через audit_log."""
    today = dt.datetime.utcnow().date()
    if today.day != 1:
        return
    # уже сбрасывали сегодня?
    for row in await asyncio.to_thread(repo.recent_audit, 20):
        if row["action"] == "monthly_reset":
            when = dt.datetime.utcfromtimestamp(row["created_at"]).date()
            if when == today:
                return

    users = await asyncio.to_thread(repo.list_active_with_client)
    restored = 0
    for u in users:
        limit_bytes = int(u.traffic_limit_gb * GB)
        was_blocked = u.traffic_used_bytes >= limit_bytes
        await asyncio.to_thread(repo.reset_traffic_period, u.telegram_id)
        if was_blocked and u.vpn_client_id:
            try:
                await asyncio.to_thread(
                    vpn_client.restore_client, u.telegram_id, u.vpn_client_id, settings
                )
                restored += 1
                await _notify(
                    bot, u.telegram_id,
                    "🔄 Новый расчётный период — трафик обнулён, доступ восстановлен.",
                )
            except vpn_client.VpnEngineError as e:
                log.error("restore_client %s не удался: %s", u.telegram_id, e)
    await asyncio.to_thread(
        repo.audit, "monthly_reset", None, {"users": len(users), "restored": restored}
    )
    log.info("monthly_reset: %d пользователей, восстановлено %d", len(users), restored)


# ============================ раннер ============================

async def _loop(name: str, coro, interval: int, *args) -> None:
    """Бесконечный цикл задачи с защитой от исключений."""
    while True:
        try:
            await coro(*args)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("задача %s упала на итерации", name)
        await asyncio.sleep(interval)


def start_jobs(bot: Bot, repo: Repository, settings: Settings) -> list[asyncio.Task]:
    """Запускает все фоновые задачи, возвращает список тасков (для отмены при стопе)."""
    tasks = [
        asyncio.create_task(_loop("check_subscriptions", check_subscriptions, DAY, bot, repo, settings)),
        asyncio.create_task(_loop("check_paid_expiry", check_paid_expiry, DAY, bot, repo, settings)),
        asyncio.create_task(_loop("monthly_reset", monthly_reset, DAY, bot, repo, settings)),
        asyncio.create_task(
            _loop("sample_traffic", sample_traffic,
                  settings.traffic_sample_interval_seconds, bot, repo, settings)
        ),
    ]
    log.info("фоновые задачи запущены: %d", len(tasks))
    return tasks
