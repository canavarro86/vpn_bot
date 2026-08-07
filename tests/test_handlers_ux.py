"""Tests for the UX helpers in src/bot/handlers.py — traffic bar, menu, QR."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot import handlers
from src.bot.handlers import _main_menu_kb, _traffic_bar, _help_text, USER_COMMANDS
from src.db import repository as repo_mod


# ─────────────────────── _traffic_bar ───────────────────────

def test_traffic_bar_empty() -> None:
    assert _traffic_bar(0.0, 5.0) == "░" * 10


def test_traffic_bar_half() -> None:
    assert _traffic_bar(2.5, 5.0) == "▓" * 5 + "░" * 5


def test_traffic_bar_full() -> None:
    assert _traffic_bar(5.0, 5.0) == "▓" * 10


def test_traffic_bar_over_limit_caps_at_full() -> None:
    assert _traffic_bar(50.0, 5.0) == "▓" * 10


def test_traffic_bar_zero_limit_is_empty() -> None:
    """No division by zero when the limit is 0."""
    assert _traffic_bar(3.0, 0.0) == "░" * 10


def test_traffic_bar_custom_width() -> None:
    bar = _traffic_bar(1.0, 4.0, width=20)
    assert len(bar) == 20
    assert bar == "▓" * 5 + "░" * 15


# ─────────────────────── menu keyboard ───────────────────────

def _callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_main_menu_has_core_buttons() -> None:
    cbs = _callbacks(_main_menu_kb())
    assert cbs == ["get_key", "show_status", "upgrade", "help"]


def test_main_menu_admin_row() -> None:
    assert "admin_panel" in _callbacks(_main_menu_kb(is_admin=True))


def test_help_text_lists_every_user_command() -> None:
    text = _help_text()
    for cmd, _desc in USER_COMMANDS:
        assert f"/{cmd}" in text


# ─────────────────────── _do_get / QR ───────────────────────

ACCESS_URL = "vless://uuid@1.2.3.4:443?type=tcp&security=reality#hideway-42"


def _fake_message() -> MagicMock:
    msg = MagicMock()
    msg.answer = AsyncMock()
    msg.answer_photo = AsyncMock()
    return msg


def _active_user() -> MagicMock:
    user = MagicMock()
    user.status = repo_mod.STATUS_ACTIVE
    user.access_url = ACCESS_URL
    return user


@pytest.mark.asyncio
async def test_do_get_sends_qr_photo(minimal_settings) -> None:
    msg = _fake_message()
    repo = MagicMock()
    repo.get_user.return_value = _active_user()

    await handlers._do_get(msg, 42, MagicMock(), repo, minimal_settings)

    msg.answer_photo.assert_awaited_once()
    photo = msg.answer_photo.await_args.args[0]
    assert photo.data[:8] == b"\x89PNG\r\n\x1a\n"  # real PNG payload
    caption = msg.answer_photo.await_args.kwargs["caption"]
    assert ACCESS_URL in caption
    assert "v2rayNG" in caption


@pytest.mark.asyncio
async def test_do_get_falls_back_to_text_when_qr_fails(minimal_settings) -> None:
    """A broken qrcode library must not break the handler."""
    msg = _fake_message()
    repo = MagicMock()
    repo.get_user.return_value = _active_user()

    with patch.object(handlers, "_qr_png", side_effect=RuntimeError("no qrcode")):
        await handlers._do_get(msg, 42, MagicMock(), repo, minimal_settings)

    msg.answer_photo.assert_not_awaited()
    sent = [c.args[0] for c in msg.answer.await_args_list]
    assert ACCESS_URL in sent


@pytest.mark.asyncio
async def test_do_get_unregistered_user(minimal_settings) -> None:
    msg = _fake_message()
    repo = MagicMock()
    repo.get_user.return_value = None

    await handlers._do_get(msg, 42, MagicMock(), repo, minimal_settings)

    msg.answer_photo.assert_not_awaited()
    assert "/start" in msg.answer.await_args.args[0]


# ─────────────────────── bot menu (set_my_commands) ───────────────────────

@pytest.mark.asyncio
async def test_setup_bot_commands_default_plus_per_admin_scope(minimal_settings) -> None:
    from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault
    from src.bot import app

    bot = AsyncMock()
    await app.setup_bot_commands(bot, minimal_settings)  # admin_user_ids == [999]

    calls = bot.set_my_commands.await_args_list
    assert len(calls) == 2
    assert isinstance(calls[0].kwargs["scope"], BotCommandScopeDefault)
    assert [c.command for c in calls[0].args[0]] == [c for c, _ in USER_COMMANDS]

    admin_scope = calls[1].kwargs["scope"]
    assert isinstance(admin_scope, BotCommandScopeChat)
    assert admin_scope.chat_id == 999
    admin_cmds = [c.command for c in calls[1].args[0]]
    assert "admin_approve" in admin_cmds and "start" in admin_cmds


@pytest.mark.asyncio
async def test_setup_bot_commands_survives_admin_error(minimal_settings) -> None:
    """An admin who never messaged the bot must not break startup."""
    from aiogram.exceptions import TelegramForbiddenError
    from src.bot import app

    bot = AsyncMock()
    bot.set_my_commands.side_effect = [
        None,
        TelegramForbiddenError(method=MagicMock(), message="blocked"),
    ]
    await app.setup_bot_commands(bot, minimal_settings)  # must not raise


@pytest.mark.asyncio
async def test_do_get_under_approve_is_not_given_a_key(minimal_settings) -> None:
    msg = _fake_message()
    repo = MagicMock()
    user = _active_user()
    user.status = repo_mod.STATUS_UNDER_APPROVE
    repo.get_user.return_value = user

    await handlers._do_get(msg, 42, MagicMock(), repo, minimal_settings)

    msg.answer_photo.assert_not_awaited()
