"""Админ-команды (ТЗ раздел 2). Доступ только для ID из ADMIN_USER_IDS.

Состав админов меняется ТОЛЬКО через .env + restart — runtime-команд добавления
админа нет намеренно (ТЗ раздел 2).

Все хендлеры закрыты фильтром IsAdmin. settings/repo прокидываются через DI.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

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

# (подпись в /admin_stats, значение колонки users.status) — берём из констант
# репозитория, чтобы строка статуса не расходилась с тем, что пишется в БД
_STATUS_ROWS = (
    ("active", repo_mod.STATUS_ACTIVE),
    ("pending", repo_mod.STATUS_PENDING),
    ("under_approve", repo_mod.STATUS_UNDER_APPROVE),
    ("revoked", repo_mod.STATUS_REVOKED),
    ("banned", repo_mod.STATUS_BANNED),
)
_KNOWN_STATUSES = {key for _, key in _STATUS_ROWS}

# (команда, описание) для персонального меню админов (app.setup_bot_commands).
ADMIN_COMMANDS: list[tuple[str, str]] = [
    ("admin_help", "Справка по админ-командам"),
    ("admin_stats", "Сводка по статусам/тарифам/трафику"),
    ("admin_stats_full", "Расширенная статистика"),
    ("admin_list", "Список юзеров с пагинацией"),
    ("admin_find", "Карточка пользователя"),
    ("admin_approve", "Апрув under_approve → pending"),
    ("admin_ban", "Забанить пользователя"),
    ("admin_unban", "Разбанить пользователя"),
    ("admin_revoke", "Отозвать VPN-клиента"),
    ("admin_delete", "Удалить пользователя из БД"),
    ("admin_grant_paid", "Выдать платный тариф"),
    ("admin_broadcast", "Рассылка (только active)"),
]


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        user_id = message.from_user.id if message.from_user else None
        is_admin = user_id is not None and settings.is_admin(user_id)
        log.info("admin command %s from %s, is_admin=%s", message.text, user_id, is_admin)
        return is_admin


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


async def _stats_text(repo: Repository) -> str:
    st = await asyncio.to_thread(repo.stats_counts)
    by_status = st["by_status"]
    by_tier = st["by_tier"]
    total_gb = st["total_traffic_bytes"] / GB

    lines = [f"<b>📊 Статистика</b> <i>(v{__version__})</i>", ""]
    lines += [f"{label}: {by_status.get(key, 0)}" for label, key in _STATUS_ROWS]
    # статусы из БД, которых нет в _STATUS_ROWS — иначе юзеры молча исчезают из сводки
    for status, count in sorted(by_status.items()):
        if status not in _KNOWN_STATUSES:
            lines.append(f"{status} (?): {count}")
    lines += [
        "",
        f"всего юзеров: {st['total_users']}",
        f"free: {by_tier.get('free', 0)} | paid: {by_tier.get('paid', 0)}",
        "",
        f"Суммарный трафик: {total_gb:.2f} GB",
        f"<i>БД: {st['db_path']}</i>",
    ]
    return "\n".join(lines)


@router.message(Command("admin_stats"))
async def admin_stats(message: Message, repo: Repository) -> None:
    await message.answer(await _stats_text(repo))


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


async def _do_admin_ban(
    telegram_id: int, reason: str, by: int, repo: Repository, settings: Settings
) -> str:
    """Общая бизнес-логика бана для /admin_ban и кнопки adm_ban. Возвращает отчёт."""
    # Revoke the Xray client BEFORE writing the ban so the user loses VPN
    # access immediately, not just on the next middleware check.
    user = await asyncio.to_thread(repo.get_user, telegram_id)
    if user and user.vpn_client_id:
        try:
            await asyncio.to_thread(vpn_client.delete_client, user.vpn_client_id, settings)
        except vpn_client.VpnEngineError as e:
            log.error("admin_ban: delete_client(%s) не удался: %s", telegram_id, e)
        await asyncio.to_thread(repo.set_vpn_client, telegram_id, None, None)
    await asyncio.to_thread(repo.add_ban, telegram_id, reason, None)  # permanent
    await asyncio.to_thread(repo.set_status, telegram_id, repo_mod.STATUS_BANNED)
    await asyncio.to_thread(
        repo.audit, "banned", telegram_id, {"by": by, "reason": reason}
    )
    return f"🚫 {telegram_id} забанен. Причина: {reason}"


@router.message(Command("admin_ban"))
async def admin_ban(
    message: Message, command: CommandObject, repo: Repository, settings: Settings
) -> None:
    parts = (command.args or "").split(maxsplit=1)
    tid = _parse_int(parts[0] if parts else None)
    if tid is None:
        await message.answer("Использование: <code>/admin_ban &lt;telegram_id&gt; [причина]</code>")
        return
    reason = parts[1] if len(parts) > 1 else "admin ban"
    await message.answer(
        await _do_admin_ban(tid, reason, message.from_user.id, repo, settings)
    )


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


async def _do_admin_approve(telegram_id: int, by: int, bot: Bot, repo: Repository) -> str:
    """under_approve → pending_subscription. Общая логика /admin_approve и кнопки
    adm_approve. Возвращает отчёт для админа."""
    user = await asyncio.to_thread(repo.get_user, telegram_id)
    if user is None:
        return "Пользователь не найден."
    if user.status != repo_mod.STATUS_UNDER_APPROVE:
        return f"Нельзя апрувить: статус <code>{user.status}</code> (нужен under_approve)."
    await asyncio.to_thread(repo.set_status, telegram_id, repo_mod.STATUS_PENDING)
    await asyncio.to_thread(repo.audit, "admin_approve", telegram_id, {"by": by})
    # уведомим юзера, если бот может ему писать
    try:
        await bot.send_message(
            telegram_id,
            "✅ Ваш доступ к HideWay VPN одобрен!\n"
            "Подпишитесь на наш канал и отправьте /get — выдам ссылку для подключения.",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    return (
        f"✅ {telegram_id} апрувнут → pending_subscription. "
        "Теперь он может пройти /start → подписка → /get."
    )


@router.message(Command("admin_approve"))
async def admin_approve(message: Message, command: CommandObject, bot: Bot, repo: Repository) -> None:
    """under_approve → pending_subscription. Дальше юзер сам проходит обычный флоу."""
    tid = _parse_int(command.args)
    if tid is None:
        await message.answer("Использование: <code>/admin_approve &lt;telegram_id&gt;</code>")
        return
    await message.answer(
        await _do_admin_approve(tid, message.from_user.id, bot, repo)
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


def _admin_help_text() -> str:
    return (
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


@router.message(Command("admin_help"))
async def admin_help(message: Message, settings: Settings) -> None:
    user_id = message.from_user.id if message.from_user else None
    log.info(
        "admin_help: telegram_id=%s is_admin=%s admin_user_ids=%s",
        user_id, settings.is_admin(user_id) if user_id is not None else False,
        settings.admin_user_ids,
    )
    await message.answer(_admin_help_text())


# ============================ inline админ-панель ============================
#
# Кнопки видны только админам, но callback_data подделывается — поэтому КАЖДЫЙ
# callback-хендлер ниже отдельно проверяет _is_admin (router.message.filter
# закрывает только message-хендлеры).

PANEL_PAGE_SIZE = 8  # на страницу панели: 8 юзеров = 16 кнопок + навигация


def _is_admin(call: CallbackQuery, settings: Settings) -> bool:
    user_id = call.from_user.id if call.from_user else None
    ok = user_id is not None and settings.is_admin(user_id)
    if not ok:
        log.warning("admin callback %r от не-админа %s — отклонено", call.data, user_id)
    return ok


def _panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список under_approve", callback_data="adm_page:1")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
            [InlineKeyboardButton(text="❓ Справка", callback_data="adm_help")],
        ]
    )


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
        ]
    )


async def _under_approve_page(repo: Repository, page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Строит текст + клавиатуру страницы списка under_approve."""
    status = repo_mod.STATUS_UNDER_APPROVE
    total = await asyncio.to_thread(repo.count_users, status)
    if total == 0:
        return "✅ Нет пользователей в статусе under_approve.", _back_kb()

    pages = (total + PANEL_PAGE_SIZE - 1) // PANEL_PAGE_SIZE
    page = min(max(1, page), pages)
    offset = (page - 1) * PANEL_PAGE_SIZE
    users = await asyncio.to_thread(repo.list_all_users, status, PANEL_PAGE_SIZE, offset)

    lines = [f"<b>📋 under_approve</b> — страница {page}/{pages}, всего {total}", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for u in users:
        lines.append(f"<code>{u.telegram_id}</code> (@{u.username or '—'})")
        rows.append([
            InlineKeyboardButton(text=f"✅ {u.telegram_id}", callback_data=f"adm_approve:{u.telegram_id}"),
            InlineKeyboardButton(text=f"❌ {u.telegram_id}", callback_data=f"adm_ban:{u.telegram_id}"),
        ])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_page:{page - 1}"))
    if page < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    """edit_text, игнорируя 'message is not modified'."""
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await call.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(call: CallbackQuery, settings: Settings) -> None:
    if not _is_admin(call, settings):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    await call.message.answer("<b>⚙️ Админ-панель</b>", reply_markup=_panel_kb())
    await call.answer()


@router.callback_query(F.data.startswith("adm_page:"))
async def cb_admin_page(call: CallbackQuery, repo: Repository, settings: Settings) -> None:
    if not _is_admin(call, settings):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    page = _parse_int(call.data.split(":", 1)[1]) or 1
    text, kb = await _under_approve_page(repo, page)
    await _edit(call, text, kb)
    await call.answer()


@router.callback_query(F.data == "adm_stats")
async def cb_admin_stats(call: CallbackQuery, repo: Repository, settings: Settings) -> None:
    if not _is_admin(call, settings):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    await _edit(call, await _stats_text(repo), _back_kb())
    await call.answer()


@router.callback_query(F.data == "adm_help")
async def cb_admin_help(call: CallbackQuery, settings: Settings) -> None:
    if not _is_admin(call, settings):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    await _edit(call, _admin_help_text(), _back_kb())
    await call.answer()


@router.callback_query(F.data.startswith("adm_approve:"))
async def cb_admin_approve(
    call: CallbackQuery, bot: Bot, repo: Repository, settings: Settings
) -> None:
    if not _is_admin(call, settings):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    tid = _parse_int(call.data.split(":", 1)[1])
    if tid is None:
        await call.answer("Некорректный ID.", show_alert=True)
        return
    result = await _do_admin_approve(tid, call.from_user.id, bot, repo)
    await call.answer("✅ Одобрен" if result.startswith("✅") else result[:200], show_alert=True)
    # список изменился — перерисовываем первую страницу
    text, kb = await _under_approve_page(repo, 1)
    await _edit(call, text, kb)


@router.callback_query(F.data.startswith("adm_ban:"))
async def cb_admin_ban(call: CallbackQuery, repo: Repository, settings: Settings) -> None:
    if not _is_admin(call, settings):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    tid = _parse_int(call.data.split(":", 1)[1])
    if tid is None:
        await call.answer("Некорректный ID.", show_alert=True)
        return
    await _do_admin_ban(tid, f"admin panel ban by {call.from_user.id}", call.from_user.id, repo, settings)
    await call.answer("🚫 Забанен", show_alert=True)
    text, kb = await _under_approve_page(repo, 1)
    await _edit(call, text, kb)
