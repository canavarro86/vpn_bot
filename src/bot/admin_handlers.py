"""Админ-команды (ТЗ раздел 2). Доступ только для ID из ADMIN_USER_IDS.

Состав админов меняется ТОЛЬКО через .env + restart — runtime-команд добавления
админа нет намеренно (ТЗ раздел 2).

Все хендлеры закрыты фильтром IsAdmin. settings/repo прокидываются через DI.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import Message

from .. import __version__
from ..config import Settings
from ..db import repository as repo_mod
from ..db.repository import Repository
from ..vpn_engine import client as vpn_client
from . import handlers as user_handlers

log = logging.getLogger(__name__)
router = Router(name="admin")

GB = 1_000_000_000
PAGE_SIZE = 25  # юзеров на страницу /admin_list (Telegram лимит ~4096 символов)


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        return message.from_user is not None and settings.is_admin(message.from_user.id)


# фильтр на весь роутер — ни один admin-хендлер не сработает для не-админа
router.message.filter(IsAdmin())


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "—"
    return dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M UTC")


def _parse_int(arg: str | None) -> int | None:
    if not arg:
        return None
    try:
        return int(arg.strip())
    except ValueError:
        return None


@router.message(Command("admin_stats"))
async def admin_stats(message: Message, repo: Repository) -> None:
    st = await asyncio.to_thread(repo.stats_counts)
    by_status = st["by_status"]
    by_tier = st["by_tier"]
    total_gb = st["total_traffic_bytes"] / GB
    text = (
        f"<b>📊 Статистика</b> <i>(v{__version__})</i>\n\n"
        f"active: {by_status.get('active', 0)}\n"
        f"pending: {by_status.get('pending_subscription', 0)}\n"
        f"under_approve: {by_status.get('under_approve', 0)}\n"
        f"revoked: {by_status.get('revoked', 0)}\n"
        f"banned: {by_status.get('banned', 0)}\n\n"
        f"free: {by_tier.get('free', 0)} | paid: {by_tier.get('paid', 0)}\n\n"
        f"Суммарный трафик: {total_gb:.2f} GB"
    )
    await message.answer(text)


@router.message(Command("admin_find"))
async def admin_find(message: Message, command: CommandObject, repo: Repository) -> None:
    tid = _parse_int(command.args)
    if tid is None:
        await message.answer("Использование: <code>/admin_find &lt;telegram_id&gt;</code>")
        return
    user = await asyncio.to_thread(repo.get_user, tid)
    if user is None:
        await message.answer("Пользователь не найден.")
        return
    last_ip = await asyncio.to_thread(repo.last_ip, tid)
    ban = await asyncio.to_thread(repo.get_ban, tid)
    pays = await asyncio.to_thread(repo.list_payments, tid)
    used_gb = user.traffic_used_bytes / GB

    lines = [
        f"<b>👤 {tid}</b> @{user.username or '—'}",
        f"статус: {user.status} | тариф: {user.tier}",
        f"трафик: {used_gb:.2f} / {user.traffic_limit_gb:.0f} GB",
        f"vpn_client_id: <code>{user.vpn_client_id or '—'}</code>",
        f"создан: {_fmt_ts(user.created_at)}",
        f"paid_until: {_fmt_ts(user.paid_until)}",
        f"последний IP: {last_ip or '—'}",
    ]
    if ban:
        lines.append(f"🚫 БАН: {ban['reason'] or '—'} (до {_fmt_ts(ban['expires_at']) if ban['expires_at'] else 'навсегда'})")
    if pays:
        lines.append("\n<b>Платежи:</b>")
        for p in pays[:10]:
            lines.append(f"  {_fmt_ts(p['created_at'])} ${p['amount_usd']:.2f} {p['status']}")
    await message.answer("\n".join(lines))


@router.message(Command("admin_ban"))
async def admin_ban(message: Message, command: CommandObject, repo: Repository) -> None:
    parts = (command.args or "").split(maxsplit=1)
    tid = _parse_int(parts[0] if parts else None)
    if tid is None:
        await message.answer("Использование: <code>/admin_ban &lt;telegram_id&gt; [причина]</code>")
        return
    reason = parts[1] if len(parts) > 1 else "admin ban"
    await asyncio.to_thread(repo.add_ban, tid, reason, None)  # permanent
    await asyncio.to_thread(repo.set_status, tid, repo_mod.STATUS_BANNED)
    await asyncio.to_thread(repo.audit, "banned", tid, {"by": message.from_user.id, "reason": reason})
    await message.answer(f"🚫 {tid} забанен. Причина: {reason}")


@router.message(Command("admin_unban"))
async def admin_unban(message: Message, command: CommandObject, repo: Repository) -> None:
    tid = _parse_int(command.args)
    if tid is None:
        await message.answer("Использование: <code>/admin_unban &lt;telegram_id&gt;</code>")
        return
    await asyncio.to_thread(repo.remove_ban, tid)
    user = await asyncio.to_thread(repo.get_user, tid)
    if user and user.status == repo_mod.STATUS_BANNED:
        await asyncio.to_thread(repo.set_status, tid, repo_mod.STATUS_REVOKED)
    await asyncio.to_thread(repo.audit, "unbanned", tid, {"by": message.from_user.id})
    await message.answer(f"✅ {tid} разбанен (статус → revoked, нужно заново активироваться).")


@router.message(Command("admin_revoke"))
async def admin_revoke(message: Message, command: CommandObject, repo: Repository, settings: Settings) -> None:
    tid = _parse_int(command.args)
    if tid is None:
        await message.answer("Использование: <code>/admin_revoke &lt;telegram_id&gt;</code>")
        return
    user = await asyncio.to_thread(repo.get_user, tid)
    if user is None:
        await message.answer("Пользователь не найден.")
        return
    await user_handlers.revoke_user(repo, settings, user, reason=f"admin_revoke by {message.from_user.id}")
    await message.answer(f"♻️ VPN-клиент {tid} удалён, статус → revoked.")


@router.message(Command("admin_grant_paid"))
async def admin_grant_paid(message: Message, command: CommandObject, repo: Repository, settings: Settings) -> None:
    parts = (command.args or "").split()
    tid = _parse_int(parts[0] if parts else None)
    days = _parse_int(parts[1] if len(parts) > 1 else None)
    if tid is None or days is None or days <= 0:
        await message.answer("Использование: <code>/admin_grant_paid &lt;telegram_id&gt; &lt;дни&gt;</code>")
        return
    user = await asyncio.to_thread(repo.get_user, tid)
    if user is None:
        await message.answer("Пользователь не найден. Сначала он должен запустить /start.")
        return
    now = repo_mod.now_ts()
    base = user.paid_until if (user.paid_until and user.paid_until > now) else now
    paid_until = base + days * 86400
    await asyncio.to_thread(
        repo.set_tier, tid, repo_mod.TIER_PAID, settings.paid_tier_gb, paid_until
    )
    await asyncio.to_thread(
        repo.audit, "admin_grant", tid,
        {"by": message.from_user.id, "days": days, "paid_until": paid_until},
    )
    await message.answer(f"💎 {tid}: paid на {days} дн., до {_fmt_ts(paid_until)}.")


@router.message(Command("admin_broadcast"))
async def admin_broadcast(message: Message, command: CommandObject, bot: Bot, repo: Repository) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.answer("Использование: <code>/admin_broadcast &lt;текст&gt;</code>")
        return
    # Защитное правило ТЗ: рассылка ТОЛЬКО status=active (не pending/revoked).
    targets = await asyncio.to_thread(repo.list_users_by_status, repo_mod.STATUS_ACTIVE)
    sent = failed = 0
    for u in targets:
        try:
            await bot.send_message(u.telegram_id, text)
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1  # заблокировал бота / удалён — пропускаем
        await asyncio.sleep(0.05)  # мягкий троттлинг под лимиты Telegram
    await asyncio.to_thread(
        repo.audit, "admin_broadcast", message.from_user.id, {"sent": sent, "failed": failed}
    )
    await message.answer(f"📣 Рассылка: отправлено {sent}, ошибок {failed} (только active).")


@router.message(Command("admin_approve"))
async def admin_approve(message: Message, command: CommandObject, bot: Bot, repo: Repository) -> None:
    """under_approve → pending_subscription. Дальше юзер сам проходит обычный флоу."""
    tid = _parse_int(command.args)
    if tid is None:
        await message.answer("Использование: <code>/admin_approve &lt;telegram_id&gt;</code>")
        return
    user = await asyncio.to_thread(repo.get_user, tid)
    if user is None:
        await message.answer("Пользователь не найден.")
        return
    if user.status != repo_mod.STATUS_UNDER_APPROVE:
        await message.answer(
            f"Нельзя апрувить: статус <code>{user.status}</code> (нужен under_approve)."
        )
        return
    await asyncio.to_thread(repo.set_status, tid, repo_mod.STATUS_PENDING)
    await asyncio.to_thread(
        repo.audit, "admin_approve", tid, {"by": message.from_user.id}
    )
    # уведомим юзера, если бот может ему писать
    try:
        await bot.send_message(
            tid,
            "✅ Ваш доступ к HideWay VPN одобрен!\n"
            "Подпишитесь на наш канал и отправьте /get — выдам ссылку для подключения.",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    await message.answer(
        f"✅ {tid} апрувнут → pending_subscription. "
        "Теперь он может пройти /start → подписка → /get."
    )


@router.message(Command("admin_list"))
async def admin_list(message: Message, command: CommandObject, repo: Repository) -> None:
    """Список юзеров с пагинацией. /admin_list [статус] [страница]."""
    parts = (command.args or "").split()
    status = None
    page = 1
    for part in parts:
        n = _parse_int(part)
        if n is not None:
            page = max(1, n)
        else:
            status = part
    total = await asyncio.to_thread(repo.count_users, status)
    if total == 0:
        await message.answer(
            f"Нет пользователей{f' со статусом {status}' if status else ''}."
        )
        return
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = min(page, pages)
    offset = (page - 1) * PAGE_SIZE
    users = await asyncio.to_thread(repo.list_all_users, status, PAGE_SIZE, offset)

    header = f"<b>👥 Пользователи</b> ({status or 'все'}) — страница {page}/{pages}, всего {total}\n"
    lines = [header]
    for u in users:
        used_gb = u.traffic_used_bytes / GB
        paid = _fmt_ts(u.paid_until) if u.paid_until else "—"
        lines.append(
            f"<code>{u.telegram_id}</code> @{u.username or '—'} | {u.status} | "
            f"{u.tier} | {used_gb:.2f}/{u.traffic_limit_gb:.0f}GB | {paid}"
        )
    if page < pages:
        nav = f"/admin_list {status + ' ' if status else ''}{page + 1}"
        lines.append(f"\n→ дальше: <code>{nav}</code>")
    await message.answer("\n".join(lines))


@router.message(Command("admin_delete"))
async def admin_delete(message: Message, command: CommandObject, repo: Repository, settings: Settings) -> None:
    """Полное удаление юзера из БД. Требует подтверждения:
    первый вызов показывает данные, второй с 'confirm' — удаляет."""
    parts = (command.args or "").split()
    tid = _parse_int(parts[0] if parts else None)
    confirm = len(parts) > 1 and parts[1].lower() == "confirm"
    if tid is None:
        await message.answer("Использование: <code>/admin_delete &lt;telegram_id&gt; [confirm]</code>")
        return
    user = await asyncio.to_thread(repo.get_user, tid)
    if user is None:
        await message.answer("Пользователь не найден.")
        return
    if not confirm:
        used_gb = user.traffic_used_bytes / GB
        await message.answer(
            f"⚠️ Удалить НАВСЕГДА?\n"
            f"<code>{tid}</code> @{user.username or '—'} | {user.status} | {user.tier} | "
            f"{used_gb:.2f}GB\n"
            f"Будут удалены: users, payments, connection_log, bans (audit_log сохранится).\n\n"
            f"Подтвердите: <code>/admin_delete {tid} confirm</code>"
        )
        return
    # отзываем VPN-клиента из Xray перед удалением записи
    if user.vpn_client_id:
        try:
            await asyncio.to_thread(vpn_client.delete_client, user.vpn_client_id, settings)
        except vpn_client.VpnEngineError as e:
            log.error("admin_delete: revoke %s не удался: %s", tid, e)
    await asyncio.to_thread(
        repo.audit, "admin_delete", tid,
        {"by": message.from_user.id, "username": user.username, "status": user.status},
    )
    await asyncio.to_thread(repo.delete_user, tid)
    await message.answer(f"🗑 {tid} удалён полностью (audit-история сохранена).")


@router.message(Command("admin_stats_full"))
async def admin_stats_full(message: Message, repo: Repository) -> None:
    """Расширенная статистика: топ по трафику + сводка по paid + under_approve."""
    st = await asyncio.to_thread(repo.stats_counts)
    top = await asyncio.to_thread(repo.traffic_top, 10)
    paid = await asyncio.to_thread(repo.paid_summary)
    by_status = st["by_status"]
    total_gb = st["total_traffic_bytes"] / GB

    lines = [
        f"<b>📈 Полная статистика</b> <i>(v{__version__})</i>",
        "",
        f"Всего юзеров: {sum(by_status.values())}",
        f"under_approve (ждут апрува): {by_status.get('under_approve', 0)}",
        f"Активные paid: {paid['active_paid']} | суммарно дней оплачено: {paid['remaining_days']}",
        f"Суммарный трафик: {total_gb:.2f} GB",
        "",
        "<b>Топ-10 по трафику:</b>",
    ]
    for i, u in enumerate(top, 1):
        if u.traffic_used_bytes <= 0:
            break
        used_gb = u.traffic_used_bytes / GB
        lines.append(f"{i}. <code>{u.telegram_id}</code> @{u.username or '—'} — {used_gb:.2f} GB")
    await message.answer("\n".join(lines))


@router.message(Command("admin_help"))
async def admin_help(message: Message) -> None:
    text = (
        f"<b>🛠 Админ-команды HideWay</b> <i>(v{__version__})</i>\n\n"
        "<code>/admin_stats</code> — сводка по статусам/тарифам/трафику\n"
        "<code>/admin_stats_full</code> — + топ по трафику и сводка paid\n"
        "<code>/admin_find &lt;id&gt;</code> — карточка пользователя\n"
        "<code>/admin_list [статус] [стр]</code> — список с пагинацией\n"
        "<code>/admin_approve &lt;id&gt;</code> — апрув under_approve → pending\n"
        "<code>/admin_ban &lt;id&gt; [причина]</code> — бан (permanent)\n"
        "<code>/admin_unban &lt;id&gt;</code> — разбан → revoked\n"
        "<code>/admin_revoke &lt;id&gt;</code> — отозвать VPN, запись остаётся\n"
        "<code>/admin_delete &lt;id&gt; [confirm]</code> — полное удаление из БД\n"
        "<code>/admin_grant_paid &lt;id&gt; &lt;дни&gt;</code> — выдать paid\n"
        "<code>/admin_broadcast &lt;текст&gt;</code> — рассылка (только active)\n"
        "<code>/admin_help</code> — эта справка"
    )
    await message.answer(text)
