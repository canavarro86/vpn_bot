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
import io
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    BufferedInputFile,
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

# (команда, описание) — источник правды и для меню бота (app.setup_bot_commands),
# и для текста /help. Порядок = порядок в меню.
USER_COMMANDS: list[tuple[str, str]] = [
    ("start", "Начать / перезапустить"),
    ("status", "Статус подписки и трафика"),
    ("get", "Получить VPN-ключ"),
    ("upgrade", "Перейти на платный тариф"),
    ("check_subscription", "Проверить подписку на канал"),
    ("help", "Список команд"),
]


def _help_text() -> str:
    lines = ["<b>❓ Команды HideWay VPN</b>", ""]
    lines += [f"/{cmd} — {desc}" for cmd, desc in USER_COMMANDS]
    return "\n".join(lines)


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


def _main_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🔑 Мой ключ", callback_data="get_key"),
            InlineKeyboardButton(text="📊 Статус", callback_data="show_status"),
        ],
        [InlineKeyboardButton(text="💳 Оплата", callback_data="upgrade")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
    ]
    if is_admin:
        rows.append(
            [InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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

    # TEMPORARILY DISABLED — bot not admin in channel yet.
    # Free 5GB VPN granted without subscription check until the bot is made
    # an admin in the channel and this is re-enabled.
    """
    return True
    # chat = _channel_chat_id(settings.required_channel_id)
    # if chat is None:
    #     log.warning("REQUIRED_CHANNEL_ID не задан — пропускаю проверку подписки")
    #     return True
    # try:
    #     member = await bot.get_chat_member(chat, telegram_id)
    # except TelegramBadRequest as e:
    #     # Fail-open on API errors (wrong channel ID, bot not admin, etc.).
    #     # Returning False here would trigger mass revocations if the channel
    #     # temporarily misbehaves. Users who actually left show up as
    #     # ChatMemberStatus.LEFT without raising an exception.
    #     log.warning(
    #         "get_chat_member(%s, %s) API error — treating as subscribed (fail-open): %s",
    #         chat, telegram_id, e.message,
    #     )
    #     return True
    # return member.status in (
    #     ChatMemberStatus.MEMBER,
    #     ChatMemberStatus.ADMINISTRATOR,
    #     ChatMemberStatus.CREATOR,
    # )


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


# статусы, которым разрешена выдача VPN. under_approve ждёт ручного апрува,
# banned — забанен; обоим клиент не выдаём (ТЗ задача 1).
_PROVISIONABLE = (repo_mod.STATUS_PENDING, repo_mod.STATUS_ACTIVE, repo_mod.STATUS_REVOKED)


async def activate_free(bot: Bot, repo: Repository, settings: Settings, telegram_id: int):
    """Проверяет подписку и при успехе выдаёт клиента. Возвращает (ok, access_url|None).

    Не выдаёт VPN, если статус не из _PROVISIONABLE (under_approve/banned) —
    мигрированные ждут /admin_approve, забаненные не обслуживаются."""
    user = await asyncio.to_thread(repo.get_user, telegram_id)
    if user is not None and user.status not in _PROVISIONABLE:
        return False, None
    if not await is_subscribed(bot, settings, telegram_id):
        await asyncio.to_thread(repo.set_status, telegram_id, repo_mod.STATUS_PENDING)
        return False, None
    url = await provision_client(repo, settings, telegram_id)
    return True, url


# ============================ presentation ============================

def _traffic_bar(used_gb: float, limit_gb: float, width: int = 10) -> str:
    """Визуальный индикатор расхода трафика. Перерасход рисуется как 100%."""
    ratio = min(used_gb / limit_gb, 1.0) if limit_gb > 0 else 0
    filled = round(ratio * width)
    return "▓" * filled + "░" * (width - filled)


def _status_text(user: User) -> str:
    used_gb = user.traffic_used_bytes / GB
    limit_gb = user.traffic_limit_gb
    lines = [
        f"<b>Тариф:</b> {'Платный' if user.tier == repo_mod.TIER_PAID else 'Бесплатный'}",
        f"<b>Статус:</b> {user.status}",
        f"<b>Трафик:</b> {_traffic_bar(used_gb, limit_gb)} "
        f"{used_gb:.1f}/{limit_gb:.0f} GB",
    ]
    if user.tier == repo_mod.TIER_PAID and user.paid_until:
        import datetime as _dt
        until = _dt.datetime.utcfromtimestamp(user.paid_until).strftime("%Y-%m-%d")
        lines.append(f"<b>Оплачено до:</b> {until} (UTC)")
    return "\n".join(lines)


QR_HINT = (
    "Отсканируйте QR в приложении v2rayNG / Streisand / Shadowrocket, "
    "либо скопируйте ссылку вручную."
)
# Telegram ограничивает подпись к фото 1024 символами; длинную ссылку шлём текстом
_CAPTION_LIMIT = 1024


def _qr_png(url: str) -> bytes:
    """PNG с QR-кодом ссылки. Вынесено отдельно, чтобы мокать в тестах."""
    import qrcode  # локальный импорт: бот работает и без библиотеки

    buf = io.BytesIO()
    qrcode.make(url).save(buf, format="PNG")
    return buf.getvalue()


async def _send_link(message: Message, access_url: str) -> None:
    await message.answer(
        "🔑 Ваша ссылка для подключения (импортируйте в v2rayNG / Hiddify / Streisand / FoXray):"
    )
    caption = f"{access_url}\n\n{QR_HINT}"
    try:
        png = await asyncio.to_thread(_qr_png, access_url)
        if len(caption) > _CAPTION_LIMIT:
            raise ValueError("caption too long for send_photo")
        # answer_photo == bot.send_photo в этот чат; BufferedInputFile — способ
        # отдать сырые байты в aiogram 3 без временного файла
        await message.answer_photo(
            BufferedInputFile(png, filename="hideway_key.png"),
            caption=caption,
            parse_mode=None,  # ссылка с & не ломается HTML-экранированием
        )
        return
    except Exception as e:  # битая ссылка, нет qrcode, ошибка отправки фото
        log.warning("QR-код не сгенерирован/не отправлен: %s", e)

    # фолбэк: отдельным сообщением без parse_mode — ссылка не экранируется
    await message.answer(access_url, parse_mode=None)
    await message.answer(QR_HINT)


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

    # мигрированные из legacy ждут ручного апрува — не зовём в подписку
    if user is not None and user.status == repo_mod.STATUS_UNDER_APPROVE:
        await message.answer(
            "👋 <b>HideWay VPN</b>\n\n"
            "Ваша заявка на доступ получена и ожидает подтверждения администратора. "
            "Как только её одобрят, вы сможете получить ссылку командой /get.",
            reply_markup=_main_menu_kb(settings.is_admin(tid)),
        )
        return

    ok, url = await activate_free(bot, repo, settings, tid)
    if ok and url:
        await message.answer(
            "👋 <b>HideWay VPN</b>\n\nДоступ активен. Меню ниже:",
            reply_markup=_main_menu_kb(settings.is_admin(tid)),
        )
        await _send_link(message, url)
    else:
        await message.answer(
            "👋 <b>HideWay VPN</b>\n\n"
            f"Бесплатный тариф: <b>{settings.free_tier_gb:.0f} GB/мес</b> за подписку на наш канал.\n"
            "Подпишитесь и нажмите кнопку проверки — выдам ссылку для подключения.",
            reply_markup=_check_sub_kb(),
        )


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


# общая логика /status и /get — вызывается и из Command-, и из callback-хендлеров
async def _do_status(message: Message, telegram_id: int, repo: Repository) -> None:
    user = await asyncio.to_thread(repo.get_user, telegram_id)
    if user is None:
        await message.answer("Вы ещё не зарегистрированы. Отправьте /start.")
        return
    kb = _upgrade_kb() if user.tier == repo_mod.TIER_FREE else None
    await message.answer(_status_text(user), reply_markup=kb)


async def _do_get(
    message: Message, telegram_id: int, bot: Bot, repo: Repository, settings: Settings
) -> None:
    user = await asyncio.to_thread(repo.get_user, telegram_id)
    if user is None:
        await message.answer("Сначала отправьте /start.")
        return
    if user.status == repo_mod.STATUS_ACTIVE and user.access_url:
        await _send_link(message, user.access_url)
        return
    if user.status == repo_mod.STATUS_UNDER_APPROVE:
        await message.answer("⏳ Ваша заявка ещё не подтверждена администратором.")
        return
    # не активен — пробуем активировать (вдруг уже подписан)
    ok, url = await activate_free(bot, repo, settings, telegram_id)
    if ok and url:
        await _send_link(message, url)
    else:
        await message.answer(
            "Доступ не активен. Подпишитесь на канал и нажмите проверку.",
            reply_markup=_check_sub_kb(),
        )


@router.message(Command("status"))
async def cmd_status(message: Message, repo: Repository, settings: Settings) -> None:
    await _do_status(message, message.from_user.id, repo)


@router.message(Command("get"))
async def cmd_get(message: Message, bot: Bot, repo: Repository, settings: Settings) -> None:
    await _do_get(message, message.from_user.id, bot, repo, settings)


@router.message(Command("help"))
async def cmd_help(message: Message, settings: Settings) -> None:
    await message.answer(
        _help_text(), reply_markup=_main_menu_kb(settings.is_admin(message.from_user.id))
    )


@router.callback_query(F.data == "show_status")
async def cb_show_status(call: CallbackQuery, repo: Repository) -> None:
    await _do_status(call.message, call.from_user.id, repo)
    await call.answer()


@router.callback_query(F.data == "get_key")
async def cb_get_key(call: CallbackQuery, bot: Bot, repo: Repository, settings: Settings) -> None:
    await _do_get(call.message, call.from_user.id, bot, repo, settings)
    await call.answer()


@router.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery, settings: Settings) -> None:
    await call.message.answer(
        _help_text(), reply_markup=_main_menu_kb(settings.is_admin(call.from_user.id))
    )
    await call.answer()


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
        # Вебхука нет: оплату подтверждает фоновый поллинг
        # (jobs.check_pending_payments), уведомление придёт отдельным сообщением.
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Оплатить", url=invoice.pay_url)]
            ]
        )
        await message.answer(
            f"💳 Счёт на ${invoice.amount_usd:.2f} создан.\n"
            f"Оплатите по ссылке — тариф продлится автоматически "
            f"в течение минуты после подтверждения платежа:\n{invoice.pay_url}",
            reply_markup=kb,
        )
