"""Tests for scripts/bulk_approve_under_approve.py.

bulk_approve() is called in-process (not via subprocess); activate_free is
patched so no Xray config is touched, the Bot is a mock so nothing is sent.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramForbiddenError

from scripts.bulk_approve_under_approve import bulk_approve
from src.bot import handlers as user_handlers
from src.db import repository as repo_mod

ACCESS_URL = "vless://uuid@1.2.3.4:443?type=tcp&security=reality#hideway-%s"
IDS = [111111111, 222222222, 333333333]


@pytest.fixture
def under_approve_repo(repo):
    for tid in IDS:
        repo.upsert_user(tid, f"user{tid}", repo_mod.STATUS_UNDER_APPROVE)
    return repo


def _fake_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    return bot


def _ok_activate() -> AsyncMock:
    return AsyncMock(side_effect=lambda bot, repo, st, tid: (True, ACCESS_URL % tid))


def _statuses(repo) -> dict[int, str]:
    return {tid: repo.get_user(tid).status for tid in IDS}


# ─────────────────────── dry-run ───────────────────────

@pytest.mark.asyncio
async def test_dry_run_changes_nothing(under_approve_repo, minimal_settings) -> None:
    bot = _fake_bot()
    activate = _ok_activate()

    with patch.object(user_handlers, "activate_free", activate):
        counts = await bulk_approve(
            bot, under_approve_repo, minimal_settings, dry_run=True
        )

    assert counts == {"ok": 0, "fail": 0, "notify_fail": 0, "total": 3}
    activate.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    bot.send_photo.assert_not_awaited()
    assert set(_statuses(under_approve_repo).values()) == {repo_mod.STATUS_UNDER_APPROVE}
    assert under_approve_repo.recent_audit(100) == []


# ─────────────────────── limit ───────────────────────

@pytest.mark.asyncio
async def test_limit_processes_exactly_one(under_approve_repo, minimal_settings) -> None:
    bot = _fake_bot()
    activate = _ok_activate()

    with patch.object(user_handlers, "activate_free", activate):
        counts = await bulk_approve(
            bot, under_approve_repo, minimal_settings, limit=1
        )

    assert counts["ok"] == 1
    assert counts["total"] == 1
    activate.assert_awaited_once()
    assert activate.await_args.args[3] == IDS[0]
    # остальные не тронуты
    statuses = _statuses(under_approve_repo)
    assert statuses[IDS[1]] == repo_mod.STATUS_UNDER_APPROVE
    assert statuses[IDS[2]] == repo_mod.STATUS_UNDER_APPROVE


@pytest.mark.asyncio
async def test_notification_carries_text_and_key(under_approve_repo, minimal_settings) -> None:
    bot = _fake_bot()

    with patch.object(user_handlers, "activate_free", _ok_activate()):
        await bulk_approve(bot, under_approve_repo, minimal_settings, limit=1)

    texts = [c.args[1] for c in bot.send_message.await_args_list]
    assert any("одобрена" in t for t in texts)
    # ссылка ушла QR-фото тем же путём, что и /get
    bot.send_photo.assert_awaited_once()
    assert (ACCESS_URL % IDS[0]) in bot.send_photo.await_args.kwargs["caption"]


# ─────────────────────── full run + idempotency ───────────────────────

@pytest.mark.asyncio
async def test_full_run_then_rerun_is_noop(under_approve_repo, minimal_settings) -> None:
    bot = _fake_bot()

    async def _activate(bot_, repo_, settings_, tid):
        repo_.set_status(tid, repo_mod.STATUS_ACTIVE)
        return True, ACCESS_URL % tid

    with patch.object(user_handlers, "activate_free", AsyncMock(side_effect=_activate)):
        first = await bulk_approve(bot, under_approve_repo, minimal_settings)

    assert first["ok"] == 3
    assert set(_statuses(under_approve_repo).values()) == {repo_mod.STATUS_ACTIVE}

    sent_after_first = bot.send_message.await_count
    activate2 = _ok_activate()
    with patch.object(user_handlers, "activate_free", activate2):
        second = await bulk_approve(bot, under_approve_repo, minimal_settings)

    assert second == {"ok": 0, "fail": 0, "notify_fail": 0, "total": 0}
    activate2.assert_not_awaited()
    assert bot.send_message.await_count == sent_after_first  # без повторных уведомлений


# ─────────────────────── failures ───────────────────────

@pytest.mark.asyncio
async def test_provisioning_failure_does_not_notify(under_approve_repo, minimal_settings) -> None:
    bot = _fake_bot()
    activate = AsyncMock(return_value=(False, None))

    with patch.object(user_handlers, "activate_free", activate):
        counts = await bulk_approve(bot, under_approve_repo, minimal_settings, limit=1)

    assert counts["fail"] == 1 and counts["ok"] == 0
    bot.send_message.assert_not_awaited()
    # статус возвращён — юзер остаётся в under_approve, прогон можно повторить
    assert _statuses(under_approve_repo)[IDS[0]] == repo_mod.STATUS_UNDER_APPROVE
    actions = {r["action"] for r in under_approve_repo.recent_audit(100)}
    assert "bulk_approve_failed" in actions


@pytest.mark.asyncio
async def test_provisioning_exception_is_caught(under_approve_repo, minimal_settings) -> None:
    """Одна упавшая выдача не роняет весь прогон."""
    bot = _fake_bot()

    async def _activate(bot_, repo_, settings_, tid):
        if tid == IDS[0]:
            raise RuntimeError("xray insert failed")
        return True, ACCESS_URL % tid

    with patch.object(user_handlers, "activate_free", AsyncMock(side_effect=_activate)):
        counts = await bulk_approve(bot, under_approve_repo, minimal_settings)

    assert counts["fail"] == 1
    assert counts["ok"] == 2


@pytest.mark.asyncio
async def test_notify_failure_keeps_provisioning(under_approve_repo, minimal_settings) -> None:
    bot = _fake_bot()
    bot.send_message = AsyncMock(
        side_effect=TelegramForbiddenError(method=MagicMock(), message="bot was blocked")
    )

    async def _activate(bot_, repo_, settings_, tid):
        repo_.set_status(tid, repo_mod.STATUS_ACTIVE)
        return True, ACCESS_URL % tid

    with patch.object(user_handlers, "activate_free", AsyncMock(side_effect=_activate)):
        counts = await bulk_approve(bot, under_approve_repo, minimal_settings, limit=1)

    assert counts["notify_fail"] == 1 and counts["ok"] == 0
    # клиент создан — откатывать нечего
    assert _statuses(under_approve_repo)[IDS[0]] == repo_mod.STATUS_ACTIVE
    actions = {r["action"] for r in under_approve_repo.recent_audit(100)}
    assert "notify_failed" in actions
