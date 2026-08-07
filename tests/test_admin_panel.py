"""Tests for the inline admin panel in src/bot/admin_handlers.py."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot import admin_handlers as ah
from src.db import repository as repo_mod


def _make_call(data: str, user_id: int) -> MagicMock:
    call = MagicMock()
    call.data = data
    call.from_user = MagicMock()
    call.from_user.id = user_id
    call.answer = AsyncMock()
    call.message = MagicMock()
    call.message.answer = AsyncMock()
    call.message.edit_text = AsyncMock()
    return call


def _seed_under_approve(repo, count: int) -> None:
    for i in range(count):
        tid = 1000 + i
        repo.upsert_user(
            tid, f"user{i}", repo_mod.STATUS_UNDER_APPROVE, repo_mod.TIER_FREE, 5.0
        )


# ─────────────────────── access control ───────────────────────

ADMIN_CALLBACKS = [
    ("admin_panel", ah.cb_admin_panel),
    ("adm_page:1", ah.cb_admin_page),
    ("adm_stats", ah.cb_admin_stats),
    ("adm_help", ah.cb_admin_help),
    ("adm_approve:1000", ah.cb_admin_approve),
    ("adm_ban:1000", ah.cb_admin_ban),
]


@pytest.mark.parametrize("data,_handler", ADMIN_CALLBACKS)
def test_is_admin_rejects_non_admin(data, _handler, minimal_settings) -> None:
    """admin_user_ids == [999]; a forged callback from 111 must not pass."""
    assert ah._is_admin(_make_call(data, 111), minimal_settings) is False
    assert ah._is_admin(_make_call(data, 999), minimal_settings) is True


@pytest.mark.asyncio
async def test_forged_approve_callback_changes_nothing(repo, minimal_settings) -> None:
    _seed_under_approve(repo, 1)
    call = _make_call("adm_approve:1000", user_id=111)  # not an admin

    await ah.cb_admin_approve(call, AsyncMock(), repo, minimal_settings)

    assert repo.get_user(1000).status == repo_mod.STATUS_UNDER_APPROVE
    call.answer.assert_awaited_once()
    assert call.answer.await_args.args[0] == "Недостаточно прав."


@pytest.mark.asyncio
async def test_forged_ban_callback_changes_nothing(repo, minimal_settings) -> None:
    _seed_under_approve(repo, 1)
    call = _make_call("adm_ban:1000", user_id=111)

    await ah.cb_admin_ban(call, repo, minimal_settings)

    assert repo.get_user(1000).status == repo_mod.STATUS_UNDER_APPROVE
    assert repo.is_banned(1000) is False


# ─────────────────────── under_approve page ───────────────────────

@pytest.mark.asyncio
async def test_page_empty_list(repo) -> None:
    text, kb = await ah._under_approve_page(repo, 1)
    assert "Нет пользователей" in text
    assert kb.inline_keyboard[0][0].callback_data == "admin_panel"


@pytest.mark.asyncio
async def test_page_lists_users_with_action_buttons(repo) -> None:
    _seed_under_approve(repo, 3)
    text, kb = await ah._under_approve_page(repo, 1)

    assert "1000" in text and "@user0" in text
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "adm_approve:1000" in cbs
    assert "adm_ban:1002" in cbs
    # callback_data must fit into Telegram's 64-byte limit
    assert all(len(c.encode()) <= 64 for c in cbs)


@pytest.mark.asyncio
async def test_page_paginates(repo) -> None:
    _seed_under_approve(repo, ah.PANEL_PAGE_SIZE + 3)

    text1, kb1 = await ah._under_approve_page(repo, 1)
    assert "страница 1/2" in text1
    assert "adm_page:2" in [b.callback_data for row in kb1.inline_keyboard for b in row]

    text2, kb2 = await ah._under_approve_page(repo, 2)
    assert "страница 2/2" in text2
    assert "adm_page:1" in [b.callback_data for row in kb2.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_page_clamps_out_of_range(repo) -> None:
    _seed_under_approve(repo, 2)
    text, _kb = await ah._under_approve_page(repo, 99)
    assert "страница 1/1" in text


# ─────────────────────── approve / ban via buttons ───────────────────────

@pytest.mark.asyncio
async def test_admin_approve_callback_sets_pending(repo, minimal_settings) -> None:
    _seed_under_approve(repo, 1)
    bot = AsyncMock()
    call = _make_call("adm_approve:1000", user_id=999)

    await ah.cb_admin_approve(call, bot, repo, minimal_settings)

    assert repo.get_user(1000).status == repo_mod.STATUS_PENDING
    bot.send_message.assert_awaited_once()  # user notified
    call.answer.assert_awaited_once()
    assert call.answer.await_args.args[0] == "✅ Одобрен"
    call.message.edit_text.assert_awaited_once()  # list re-rendered


@pytest.mark.asyncio
async def test_admin_ban_callback_bans(repo, minimal_settings) -> None:
    _seed_under_approve(repo, 1)
    call = _make_call("adm_ban:1000", user_id=999)

    await ah.cb_admin_ban(call, repo, minimal_settings)

    assert repo.get_user(1000).status == repo_mod.STATUS_BANNED
    assert repo.is_banned(1000) is True
    assert call.answer.await_args.args[0] == "🚫 Забанен"


@pytest.mark.asyncio
async def test_approve_wrong_status_is_reported(repo, minimal_settings) -> None:
    repo.upsert_user(2000, "act", repo_mod.STATUS_ACTIVE, repo_mod.TIER_FREE, 5.0)
    call = _make_call("adm_approve:2000", user_id=999)

    await ah.cb_admin_approve(call, AsyncMock(), repo, minimal_settings)

    assert repo.get_user(2000).status == repo_mod.STATUS_ACTIVE
    assert "Нельзя апрувить" in call.answer.await_args.args[0]


# ─────────────────────── panel entry / stats / help ───────────────────────

@pytest.mark.asyncio
async def test_panel_shows_three_buttons(minimal_settings) -> None:
    call = _make_call("admin_panel", user_id=999)
    await ah.cb_admin_panel(call, minimal_settings)

    kb = call.message.answer.await_args.kwargs["reply_markup"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert cbs == ["adm_page:1", "adm_stats", "adm_help"]


@pytest.mark.asyncio
async def test_stats_callback_reuses_stats_text(repo, minimal_settings) -> None:
    call = _make_call("adm_stats", user_id=999)
    await ah.cb_admin_stats(call, repo, minimal_settings)

    assert call.message.edit_text.await_args.args[0] == await ah._stats_text(repo)


@pytest.mark.asyncio
async def test_help_callback_reuses_admin_help_text(minimal_settings) -> None:
    call = _make_call("adm_help", user_id=999)
    await ah.cb_admin_help(call, minimal_settings)

    assert call.message.edit_text.await_args.args[0] == ah._admin_help_text()
