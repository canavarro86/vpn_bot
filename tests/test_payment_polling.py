"""Tests for jobs.check_pending_payments (polling-подтверждение оплаты)."""

from dataclasses import replace

import pytest

from src.bot import jobs
from src.db import repository as repo_mod


class FakeBot:
    """Ловит send_message вместо похода в Telegram."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, telegram_id: int, text: str) -> None:
        self.sent.append((telegram_id, text))


class FakeProvider:
    name = "cryptocloud"

    def __init__(self, statuses: dict[str, str | None]) -> None:
        self.statuses = statuses
        self.calls: list[str] = []

    def create_invoice(self, telegram_id: int, amount_usd: float):  # pragma: no cover
        raise NotImplementedError

    def get_invoice_status(self, invoice_id: str):
        self.calls.append(invoice_id)
        return self.statuses.get(invoice_id)


@pytest.fixture
def paid_settings(minimal_settings):
    return replace(minimal_settings, payment_provider="cryptocloud", paid_tier_gb=20.0)


@pytest.fixture
def user_with_invoice(repo):
    """Пользователь на free + выставленный счёт cryptocloud."""
    repo.upsert_user(555, "u", repo_mod.STATUS_ACTIVE, repo_mod.TIER_FREE, 5.0)
    repo.create_payment(555, "cryptocloud", "INV-1", 2.99, "created")
    return 555


@pytest.fixture(autouse=True)
def no_vpn_calls(monkeypatch):
    """restore_client не должен трогать реальный Xray."""
    monkeypatch.setattr(jobs.vpn_client, "restore_client", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_paid_invoice_grants_paid_tier(repo, paid_settings, user_with_invoice):
    bot = FakeBot()
    provider = FakeProvider({"INV-1": "paid"})

    await jobs.check_pending_payments(bot, repo, paid_settings, provider)

    user = repo.get_user(555)
    assert user.tier == repo_mod.TIER_PAID
    assert user.traffic_limit_gb == 20.0
    now = repo_mod.now_ts()
    assert user.paid_until is not None
    # ~30 дней вперёд (допуск на время выполнения теста)
    assert abs(user.paid_until - (now + jobs.PAID_PERIOD_DAYS * jobs.DAY)) < 60

    row = repo.list_payments(555)[0]
    assert row["status"] == "confirmed"
    assert row["confirmed_at"] is not None
    assert bot.sent and bot.sent[0][0] == 555
    assert "Оплата получена" in bot.sent[0][1]
    assert any(r["action"] == "payment_confirmed" for r in repo.recent_audit(10))


@pytest.mark.asyncio
async def test_confirmed_payment_not_processed_twice(repo, paid_settings, user_with_invoice):
    bot = FakeBot()
    provider = FakeProvider({"INV-1": "paid"})

    await jobs.check_pending_payments(bot, repo, paid_settings, provider)
    paid_until_first = repo.get_user(555).paid_until

    # второй проход: счёт уже confirmed → провайдер даже не опрашивается
    await jobs.check_pending_payments(bot, repo, paid_settings, provider)

    assert provider.calls == ["INV-1"]
    assert repo.get_user(555).paid_until == paid_until_first
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_paid_extends_existing_period(repo, paid_settings, user_with_invoice):
    """Продление считается от текущего paid_until, а не от now (как admin_grant_paid)."""
    future = repo_mod.now_ts() + 10 * jobs.DAY
    repo.set_tier(555, repo_mod.TIER_PAID, 20.0, future)

    await jobs.check_pending_payments(FakeBot(), repo, paid_settings, FakeProvider({"INV-1": "paid"}))

    assert abs(repo.get_user(555).paid_until - (future + jobs.PAID_PERIOD_DAYS * jobs.DAY)) < 60


@pytest.mark.asyncio
async def test_expired_invoice_closed_without_grant(repo, paid_settings, user_with_invoice):
    bot = FakeBot()

    await jobs.check_pending_payments(bot, repo, paid_settings, FakeProvider({"INV-1": "expired"}))

    assert repo.list_payments(555)[0]["status"] == "expired"
    assert repo.get_user(555).tier == repo_mod.TIER_FREE
    assert "закрыт" in bot.sent[0][1]


@pytest.mark.asyncio
async def test_unknown_status_leaves_invoice_open(repo, paid_settings, user_with_invoice):
    """Сеть недоступна (None) → счёт остаётся created, ничего не начисляем."""
    bot = FakeBot()

    await jobs.check_pending_payments(bot, repo, paid_settings, FakeProvider({"INV-1": None}))

    assert repo.list_payments(555)[0]["status"] == "created"
    assert repo.get_user(555).tier == repo_mod.TIER_FREE
    assert bot.sent == []


@pytest.mark.asyncio
async def test_created_status_leaves_invoice_open(repo, paid_settings, user_with_invoice):
    bot = FakeBot()

    await jobs.check_pending_payments(bot, repo, paid_settings, FakeProvider({"INV-1": "created"}))

    assert repo.list_payments(555)[0]["status"] == "created"
    assert bot.sent == []


@pytest.mark.asyncio
async def test_stub_provider_is_noop(repo, minimal_settings, user_with_invoice):
    """У stub нет get_invoice_status — задача просто ничего не делает."""
    from src.payments.stub import StubProvider

    await jobs.check_pending_payments(FakeBot(), repo, minimal_settings, StubProvider())

    assert repo.list_payments(555)[0]["status"] == "created"


def test_list_open_payments_filters_by_status_and_provider(repo):
    repo.upsert_user(1, None, repo_mod.STATUS_ACTIVE)
    repo.create_payment(1, "cryptocloud", "INV-open", 2.99, "created")
    repo.create_payment(1, "cryptocloud", "INV-pending", 2.99, "pending")
    repo.create_payment(1, "cryptocloud", "INV-done", 2.99, "confirmed")
    repo.create_payment(1, "stub", "stub-1", 2.99, "created")

    ids = [r["provider_invoice_id"] for r in repo.list_open_payments("cryptocloud")]
    assert ids == ["INV-open", "INV-pending"]
