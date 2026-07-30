"""Tests for src/bot/middleware.py — BanMiddleware + RateLimitMiddleware."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.middleware import BanMiddleware, RateLimitMiddleware
from src.config import Settings


# ─────────────────────── helpers ───────────────────────

class FakeUser:
    def __init__(self, uid: int):
        self.id = uid


def _make_data(user_id: int | None, repo: MagicMock, settings: Settings) -> dict:
    return {
        "settings": settings,
        "repo": repo,
        "event_from_user": FakeUser(user_id) if user_id is not None else None,
    }


def _noop_handler(*_) -> None:
    """Handler that always returns "ok"."""
    async def _h(event, data):
        return "ok"
    return _h()


async def _call_handler(_event, _data):
    return "ok"


def _make_settings(**kwargs) -> Settings:
    defaults = dict(
        telegram_bot_token="test",
        admin_user_ids=[999],
        rate_limit_max=5,
        rate_limit_window=60,
        ban_trigger=3,
        ban_window=60,
        ban_seconds=3600,
    )
    defaults.update(kwargs)
    return Settings(**defaults)


# ─────────────────────── BanMiddleware ───────────────────────

@pytest.mark.asyncio
async def test_ban_middleware_drops_banned_user() -> None:
    settings = _make_settings()
    repo = MagicMock()
    repo.is_banned.return_value = True

    mw = BanMiddleware()
    data = _make_data(user_id=111, repo=repo, settings=settings)
    handler_called = False

    async def handler(_e, _d):
        nonlocal handler_called
        handler_called = True
        return "ok"

    result = await mw(handler, MagicMock(), data)
    assert result is None
    assert not handler_called


@pytest.mark.asyncio
async def test_ban_middleware_passes_non_banned_user() -> None:
    settings = _make_settings()
    repo = MagicMock()
    repo.is_banned.return_value = False

    mw = BanMiddleware()
    data = _make_data(user_id=222, repo=repo, settings=settings)

    result = await mw(_call_handler, MagicMock(), data)
    assert result == "ok"


@pytest.mark.asyncio
async def test_ban_middleware_passes_admin() -> None:
    """Admin ID 999 bypasses ban check entirely."""
    settings = _make_settings()
    repo = MagicMock()
    repo.is_banned.return_value = True  # would be banned if checked

    mw = BanMiddleware()
    data = _make_data(user_id=999, repo=repo, settings=settings)

    result = await mw(_call_handler, MagicMock(), data)
    assert result == "ok"
    repo.is_banned.assert_not_called()


@pytest.mark.asyncio
async def test_ban_middleware_passes_no_user() -> None:
    """Events without a user (e.g., channel posts) pass through."""
    settings = _make_settings()
    repo = MagicMock()

    mw = BanMiddleware()
    data = _make_data(user_id=None, repo=repo, settings=settings)

    result = await mw(_call_handler, MagicMock(), data)
    assert result == "ok"


# ─────────────────────── RateLimitMiddleware ───────────────────────

@pytest.mark.asyncio
async def test_rate_limit_allows_within_limit() -> None:
    settings = _make_settings(rate_limit_max=3)
    repo = MagicMock()
    mw = RateLimitMiddleware(settings)

    for _ in range(3):
        data = _make_data(user_id=300, repo=repo, settings=settings)
        result = await mw(_call_handler, MagicMock(), data)
        assert result == "ok"


@pytest.mark.asyncio
async def test_rate_limit_blocks_on_exceed() -> None:
    settings = _make_settings(rate_limit_max=2, ban_trigger=10)  # high ban_trigger so no auto-ban
    repo = MagicMock()
    mw = RateLimitMiddleware(settings)

    for _ in range(2):
        await mw(_call_handler, MagicMock(), _make_data(301, repo, settings))

    # 3rd request should be blocked
    result = await mw(_call_handler, MagicMock(), _make_data(301, repo, settings))
    assert result is None


@pytest.mark.asyncio
async def test_rate_limit_auto_bans_on_violations() -> None:
    settings = _make_settings(
        rate_limit_max=1,
        rate_limit_window=60,
        ban_trigger=2,  # auto-ban after 2 violations
        ban_window=60,
        ban_seconds=3600,
    )
    repo = MagicMock()
    repo.add_ban.return_value = None
    repo.audit.return_value = None
    mw = RateLimitMiddleware(settings)

    # First allowed request
    await mw(_call_handler, MagicMock(), _make_data(302, repo, settings))
    # Trigger violations
    for _ in range(2):
        await mw(_call_handler, MagicMock(), _make_data(302, repo, settings))

    # After ban_trigger violations, add_ban should be called
    repo.add_ban.assert_called_once()
    call_args = repo.add_ban.call_args
    assert call_args[0][0] == 302  # telegram_id
    assert "rate-limit" in call_args[0][1]  # reason


@pytest.mark.asyncio
async def test_rate_limit_admin_bypasses() -> None:
    settings = _make_settings(rate_limit_max=1)  # very strict limit
    repo = MagicMock()
    mw = RateLimitMiddleware(settings)

    for _ in range(10):
        data = _make_data(999, repo, settings)  # 999 is admin
        result = await mw(_call_handler, MagicMock(), data)
        assert result == "ok"


@pytest.mark.asyncio
async def test_rate_limit_tracks_users_independently() -> None:
    settings = _make_settings(rate_limit_max=2, ban_trigger=10)
    repo = MagicMock()
    mw = RateLimitMiddleware(settings)

    for _ in range(2):
        await mw(_call_handler, MagicMock(), _make_data(400, repo, settings))
    for _ in range(2):
        await mw(_call_handler, MagicMock(), _make_data(401, repo, settings))

    # Both users at their limit — next request from each is blocked
    r400 = await mw(_call_handler, MagicMock(), _make_data(400, repo, settings))
    r401 = await mw(_call_handler, MagicMock(), _make_data(401, repo, settings))
    assert r400 is None
    assert r401 is None
