"""Хендлеры обычного пользователя (ТЗ раздел 2, free-тир).

Команды: /start, /status, /get, /check_subscription (+ inline-кнопка).
/upgrade добавляется вместе с paid-флоу; здесь — заглушечный ответ через
PaymentProvider (реального шлюза нет, ТЗ раздел 5).

Провижн-хелперы (`is_subscribed`, `provision_client`, `activate_free`,
`revoke_user`) вынесены module-level и переиспользуются фоновыми задачами
(bot/jobs.py, фаза 6) — единая логика выдачи/отзыва клиента.

DI: aiogram прокидывает в хендлеры `repo`, `settings`, `payments` по имени
(положены в dispatcher data при старте, см. app.py). Repository синхронный —
вызовы обёрнуты в asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..config import Settings, get_payment_provider
from ..db import repository as repo_mod
from ..db.repository import Repository, User
from ..payments.provider import INVOICE_UNAVAILABLE, PaymentProvider
from ..vpn_engine import client as vpn_client

log = logging.getLogger(__name__)
router = Router(name="user")

GB = 1_000_000_000


# ============================ keyboards ============================

def _check_sub_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я подписался, проверить", callback_data="check_sub")]
        ]
    )


def _upgrade_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти на платный (20 GB)", callback_data="upgrade")]
        ]
    )


# ============================ helpers (reused by jobs) ============================

def _channel_chat_id(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("@"):
        return raw
    try:
        return int(raw)
    except ValueError:
        return raw


async def is_subscribed(bot: Bot, settings: Settings, telegram_id: int) -> bool:
    """Подписан ли пользователь на REQUIRED_CHANNEL_ID.

    Если канал не задан (плейсхолдер, ТЗ раздел 10) — НЕ блокируем: считаем
    подписанным, чтобы бот работал до подстановки реального канала.
    """
    chat = _channel_chat_id(settings.required_channel_id)
    if chat is None:
        log.warning("REQUIRED_CHANNEL_ID не задан — пропускаю проверку подписки")
        return True
    try:
        member = await bot.get_chat_member(chat, telegram_id)
    except TelegramBadRequest as e:
        log.warning("get_chat_member(%s, %s) ошибка: %s", chat, telegram_id, e.message)
        return False
    return member.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    )


async def provision_client(repo: Repository, settings: Settings, telegram_id: int) -> str:
    """Создаёт (или переиспользует) VPN-клиента в Xray, пишет в БД, audit.
    Возвращает access_url. Идемпотентно: повторный вызов вернёт ту же ссылку."""
    cred = await asyncio.to_thread(vpn_client.create_client, telegram_id, settings)
    await asyncio.to_thread(
        repo.set_vpn_client, telegram_id, cred.uuid, cred.access_url
    )
    await asyncio.to_thread(repo.set_status, telegram_id, repo_mod.STATUS_ACTIVE)
    await asyncio.to_thread(
        repo.audit, "client_created", telegram_id, {"uuid": cred.uuid}
    )
    return cred.access_url


async def revoke_user(repo: Repository, settings: Settings, user: User, reason: str) -> None:
    """Удаляет VPN-клиента из Xray и переводит пользователя в revoked.
    Используется при отписке (jobs) и админ-revoke."""
    if user.vpn_client_id:
        try:
            await asyncio.to_thread(vpn_client.delete_client, user.vpn_client_id, settings)
        except vpn_client.VpnEngineError as e:
            log.error("delete_client(%s) не удался: %s", user.vpn_client_id, e)
    await asyncio.to_thread(repo.set_vpn_client, user.telegram_id, None, None)
    await asyncio.to_thread(repo.set_status, user.telegram_id, repo_mod.STATUS_REVOKED)
    await asyncio.to_thread(
        repo.audit, "client_revoked", user.telegram_id, {"reason": reason}
    )


async def activate_free(bot: Bot, repo: Repository, settings: Settings, telegram_id: int):
    """Проверяет подписку и при успехе выдаёт клиента. Возвращает (ok, access_url|None)."""
    if not await is_subscribed(bot, settings, telegram_id):
        await asyncio.to_thread(repo.set_status, telegram_id, repo_mod.STATUS_PENDING)
        return False, None
    url = await provision_client(repo, settings, telegram_id)
    return True, url


# ============================ presentation ============================

def _status_text(user: User) -> str:
    used_gb = user.traffic_used_bytes / GB
    lines = [
        f"<b>Тариф:</b> {'Платный' if user.tier == repo_mod.TIER_PAID else 'Бесплатный'}",
        f"<b>Статус:</b> {user.status}",
        f"<b>Трафик:</b> {used_gb:.2f} / {user.traffic_limit_gb:.0f} GB",
    ]
    if user.tier == repo_mod.TIER_PAID and user.paid_until:
        import datetime as _dt
        until = _dt.datetime.utcfromtimestamp(user.paid_until).strftime("%Y-%m-%d")
        lines.append(f"<b>Оплачено до:</b> {until} (UTC)")
    return "\n".join(lines)


async def _send_link(message: Message, access_url: str) -> None:
    await message.answer(
        "🔑 Ваша ссылка для подключения (импортируйте в v2rayNG / Hiddify / Streisand / FoXray):"
    )
    # отдельным сообщением без parse_mode — ссылка с & не ломается экранированием
    await message.answer(access_url)


# ============================ commands ============================

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, repo: Repository, settings: Settings) -> None:
    tid = message.from_user.id
    username = message.from_user.username
    user = await asyncio.to_thread(repo.get_user, tid)
    if user is None:
        await asyncio.to_thread(
            repo.upsert_user, tid, username, repo_mod.STATUS_PENDING,
            repo_mod.TIER_FREE, settings.free_tier_gb,
        )
        await asyncio.to_thread(repo.audit, "user_registered", tid, {"username": username})
    else:
        # обновим username, не трогая статус/тариф
        await asyncio.to_thread(
            repo.upsert_user, tid, username, user.status, user.tier, None
        )

    await message.answer(
        "👋 <b>HideWay VPN</b>\n\n"
        f"Бесплатный тариф: <b>{settings.free_tier_gb:.0f} GB/мес</b> за подписку на наш канал.\n"
        "Подпишитесь и нажмите кнопку проверки — выдам ссылку для подключения.",
        reply_markup=_check_sub_kb(),
    )

    ok, url = await activate_free(bot, repo, settings, tid)
    if ok and url:
        await _send_link(message, url)


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, bot: Bot, repo: Repository, settings: Settings) -> None:
    tid = call.from_user.id
    ok, url = await activate_free(bot, repo, settings, tid)
    if ok and url:
        await call.message.answer("✅ Подписка подтверждена. Доступ выдан.")
        await _send_link(call.message, url)
        await call.answer()
    else:
        await call.answer("Подписка не найдена. Подпишитесь на канал и повторите.", show_alert=True)


@router.message(Command("check_subscription"))
async def cmd_check_subscription(message: Message, bot: Bot, repo: Repository, settings: Settings) -> None:
    ok, url = await activate_free(bot, repo, settings, message.from_user.id)
    if ok and url:
        await message.answer("✅ Подписка подтверждена. Доступ выдан.")
        await _send_link(message, url)
    else:
        await message.answer(
            "❌ Подписка на канал не найдена. Подпишитесь и нажмите проверку.",
            reply_markup=_check_sub_kb(),
        )


@router.message(Command("status"))
async def cmd_status(message: Message, repo: Repository, settings: Settings) -> None:
    user = await asyncio.to_thread(repo.get_user, message.from_user.id)
    if user is None:
        await message.answer("Вы ещё не зарегистрированы. Отправьте /start.")
        return
    kb = _upgrade_kb() if user.tier == repo_mod.TIER_FREE else None
    await message.answer(_status_text(user), reply_markup=kb)


@router.message(Command("get"))
async def cmd_get(message: Message, bot: Bot, repo: Repository, settings: Settings) -> None:
    user = await asyncio.to_thread(repo.get_user, message.from_user.id)
    if user is None:
        await message.answer("Сначала отправьте /start.")
        return
    if user.status == repo_mod.STATUS_ACTIVE and user.access_url:
        await _send_link(message, user.access_url)
        return
    # не активен — пробуем активировать (вдруг уже подписан)
    ok, url = await activate_free(bot, repo, settings, message.from_user.id)
    if ok and url:
        await _send_link(message, url)
    else:
        await message.answer(
            "Доступ не активен. Подпишитесь на канал и нажмите проверку.",
            reply_markup=_check_sub_kb(),
        )


@router.message(Command("upgrade"))
async def cmd_upgrade(message: Message, repo: Repository, settings: Settings, payments: PaymentProvider) -> None:
    await _handle_upgrade(message, message.from_user.id, repo, settings, payments)


@router.callback_query(F.data == "upgrade")
async def cb_upgrade(call: CallbackQuery, repo: Repository, settings: Settings, payments: PaymentProvider) -> None:
    await _handle_upgrade(call.message, call.from_user.id, repo, settings, payments)
    await call.answer()


async def _handle_upgrade(
    message: Message, telegram_id: int, repo: Repository, settings: Settings, payments: PaymentProvider
) -> None:
    invoice = await asyncio.to_thread(
        payments.create_invoice, telegram_id, settings.paid_tier_price_usd
    )
    await asyncio.to_thread(
        repo.create_payment, telegram_id, payments.name,
        invoice.invoice_id, invoice.amount_usd, invoice.status,
    )
    await asyncio.to_thread(
        repo.audit, "invoice_created", telegram_id,
        {"invoice_id": invoice.invoice_id, "status": invoice.status},
    )
    if invoice.status == INVOICE_UNAVAILABLE or not invoice.pay_url:
        await message.answer(invoice.message or "Оплата временно недоступна.")
    else:
        await message.answer(
            f"💳 Счёт на ${invoice.amount_usd:.2f} создан.\nОплатите по ссылке:\n{invoice.pay_url}"
        )
