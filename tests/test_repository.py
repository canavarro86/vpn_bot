"""Tests for src/db/repository.py — the only place with raw SQL."""

import time
import pytest

from src.db.repository import (
    Repository,
    User,
    STATUS_ACTIVE,
    STATUS_PENDING,
    STATUS_REVOKED,
    STATUS_BANNED,
    STATUS_UNDER_APPROVE,
    TIER_FREE,
    TIER_PAID,
)


# ─────────────────────── upsert / get ───────────────────────

def test_upsert_creates_new_user(repo: Repository) -> None:
    u = repo.upsert_user(100, "alice", STATUS_PENDING)
    assert u.telegram_id == 100
    assert u.username == "alice"
    assert u.status == STATUS_PENDING
    assert u.tier == TIER_FREE
    assert u.traffic_limit_gb == 5.0  # default
    assert u.vpn_client_id is None
    assert u.access_url is None


def test_upsert_updates_existing_does_not_overwrite_vpn(repo: Repository) -> None:
    repo.upsert_user(101, "bob", STATUS_PENDING)
    repo.set_vpn_client(101, "uuid-xyz", "vless://example")
    # upsert with new status — vpn fields must survive
    u = repo.upsert_user(101, "bob2", STATUS_ACTIVE)
    assert u.vpn_client_id == "uuid-xyz"
    assert u.access_url == "vless://example"
    assert u.username == "bob2"
    assert u.status == STATUS_ACTIVE


def test_upsert_updates_username_to_none_is_noop(repo: Repository) -> None:
    """username=None in upsert → COALESCE keeps existing value."""
    repo.upsert_user(102, "charlie", STATUS_PENDING)
    u = repo.upsert_user(102, None, STATUS_ACTIVE)
    assert u.username == "charlie"


def test_upsert_custom_traffic_limit(repo: Repository) -> None:
    u = repo.upsert_user(103, None, STATUS_ACTIVE, TIER_FREE, 20.0)
    assert u.traffic_limit_gb == 20.0


def test_get_user_missing_returns_none(repo: Repository) -> None:
    assert repo.get_user(9999) is None


# ─────────────────────── status ───────────────────────

def test_set_status(repo: Repository) -> None:
    repo.upsert_user(110, None, STATUS_PENDING)
    repo.set_status(110, STATUS_ACTIVE)
    assert repo.get_user(110).status == STATUS_ACTIVE


# ─────────────────────── vpn_client ───────────────────────

def test_set_vpn_client(repo: Repository) -> None:
    repo.upsert_user(120, None, STATUS_ACTIVE)
    repo.set_vpn_client(120, "abc-uuid", "vless://abc")
    u = repo.get_user(120)
    assert u.vpn_client_id == "abc-uuid"
    assert u.access_url == "vless://abc"


def test_clear_vpn_client(repo: Repository) -> None:
    repo.upsert_user(121, None, STATUS_ACTIVE)
    repo.set_vpn_client(121, "abc-uuid", "vless://abc")
    repo.set_vpn_client(121, None, None)
    u = repo.get_user(121)
    assert u.vpn_client_id is None
    assert u.access_url is None


# ─────────────────────── traffic ───────────────────────

def test_add_traffic_accumulates(repo: Repository) -> None:
    repo.upsert_user(130, None, STATUS_ACTIVE)
    repo.add_traffic(130, 1_000_000)
    repo.add_traffic(130, 500_000)
    assert repo.get_user(130).traffic_used_bytes == 1_500_000


def test_set_traffic_used(repo: Repository) -> None:
    repo.upsert_user(131, None, STATUS_ACTIVE)
    repo.add_traffic(131, 999)
    repo.set_traffic_used(131, 42)
    assert repo.get_user(131).traffic_used_bytes == 42


def test_reset_traffic_period(repo: Repository) -> None:
    repo.upsert_user(132, None, STATUS_ACTIVE)
    repo.add_traffic(132, 9_000_000)
    repo.set_low_traffic_notified(132, 1)
    repo.reset_traffic_period(132)
    u = repo.get_user(132)
    assert u.traffic_used_bytes == 0
    assert u.low_traffic_notified == 0


# ─────────────────────── tier / paid_summary ───────────────────────

def test_set_tier_paid(repo: Repository) -> None:
    repo.upsert_user(140, None, STATUS_ACTIVE)
    future_ts = int(time.time()) + 86400 * 30
    repo.set_tier(140, TIER_PAID, 20.0, future_ts)
    u = repo.get_user(140)
    assert u.tier == TIER_PAID
    assert u.traffic_limit_gb == 20.0
    assert u.paid_until == future_ts


def test_paid_summary_counts_active_paid(repo: Repository) -> None:
    now = int(time.time())
    future = now + 86400 * 10
    repo.upsert_user(141, None, STATUS_ACTIVE)
    repo.set_tier(141, TIER_PAID, 20.0, future)
    s = repo.paid_summary(as_of=now)
    assert s["active_paid"] == 1
    assert s["remaining_days"] >= 9  # at least 9 days


def test_paid_summary_excludes_expired(repo: Repository) -> None:
    now = int(time.time())
    past = now - 1
    repo.upsert_user(142, None, STATUS_ACTIVE)
    repo.set_tier(142, TIER_PAID, 20.0, past)
    s = repo.paid_summary(as_of=now)
    assert s["active_paid"] == 0


def test_list_paid_expired(repo: Repository) -> None:
    now = int(time.time())
    repo.upsert_user(143, None, STATUS_ACTIVE)
    repo.set_tier(143, TIER_PAID, 20.0, now - 1)
    expired = repo.list_paid_expired(as_of=now)
    assert any(u.telegram_id == 143 for u in expired)


# ─────────────────────── bans ───────────────────────

def test_is_banned_permanent(repo: Repository) -> None:
    repo.upsert_user(150, None, STATUS_ACTIVE)
    repo.add_ban(150, "spam", expires_at=None)
    assert repo.is_banned(150)


def test_is_banned_future_expires(repo: Repository) -> None:
    repo.upsert_user(151, None, STATUS_ACTIVE)
    future = int(time.time()) + 3600
    repo.add_ban(151, "rate-limit", expires_at=future)
    assert repo.is_banned(151)


def test_is_banned_expired_returns_false(repo: Repository) -> None:
    repo.upsert_user(152, None, STATUS_ACTIVE)
    past = int(time.time()) - 1
    repo.add_ban(152, "old", expires_at=past)
    assert not repo.is_banned(152)


def test_remove_ban(repo: Repository) -> None:
    repo.upsert_user(153, None, STATUS_ACTIVE)
    repo.add_ban(153, "test", None)
    repo.remove_ban(153)
    assert not repo.is_banned(153)


def test_add_ban_upserts_on_conflict(repo: Repository) -> None:
    """add_ban called twice → second call updates reason + expires."""
    repo.upsert_user(154, None, STATUS_ACTIVE)
    repo.add_ban(154, "first", expires_at=None)
    future = int(time.time()) + 60
    repo.add_ban(154, "updated", expires_at=future)
    ban = repo.get_ban(154)
    assert ban["reason"] == "updated"
    assert ban["expires_at"] == future


# ─────────────────────── list / count ───────────────────────

def test_list_users_by_status(repo: Repository) -> None:
    repo.upsert_user(160, None, STATUS_ACTIVE)
    repo.upsert_user(161, None, STATUS_PENDING)
    repo.upsert_user(162, None, STATUS_ACTIVE)
    active = repo.list_users_by_status(STATUS_ACTIVE)
    assert {u.telegram_id for u in active} == {160, 162}


def test_count_users(repo: Repository) -> None:
    for i in range(5):
        repo.upsert_user(170 + i, None, STATUS_ACTIVE)
    assert repo.count_users() == 5
    assert repo.count_users(STATUS_ACTIVE) == 5
    assert repo.count_users(STATUS_PENDING) == 0


def test_list_active_with_client(repo: Repository) -> None:
    repo.upsert_user(180, None, STATUS_ACTIVE)
    repo.set_vpn_client(180, "uuid-180", "vless://180")
    repo.upsert_user(181, None, STATUS_ACTIVE)  # no client
    with_client = repo.list_active_with_client()
    assert any(u.telegram_id == 180 for u in with_client)
    assert all(u.vpn_client_id is not None for u in with_client)


def test_list_all_users_pagination(repo: Repository) -> None:
    for i in range(10):
        repo.upsert_user(190 + i, None, STATUS_ACTIVE)
    page1 = repo.list_all_users(limit=4, offset=0)
    page2 = repo.list_all_users(limit=4, offset=4)
    assert len(page1) == 4
    assert len(page2) == 4
    ids1 = {u.telegram_id for u in page1}
    ids2 = {u.telegram_id for u in page2}
    assert ids1.isdisjoint(ids2)


# ─────────────────────── delete ───────────────────────

def test_delete_user_removes_dependents(repo: Repository) -> None:
    repo.upsert_user(200, None, STATUS_ACTIVE)
    repo.add_ban(200, "test", None)
    repo.log_connection(200, 1000)
    repo.create_payment(200, "stub", "inv-001", 2.99)
    existed = repo.delete_user(200)
    assert existed is True
    assert repo.get_user(200) is None
    assert repo.get_ban(200) is None


def test_delete_missing_user_returns_false(repo: Repository) -> None:
    assert repo.delete_user(9999) is False


# ─────────────────────── payments ───────────────────────

def test_create_payment_and_set_status(repo: Repository) -> None:
    repo.upsert_user(210, None, STATUS_ACTIVE)
    pid = repo.create_payment(210, "stub", "inv-abc", 2.99)
    assert pid > 0
    repo.set_payment_status("inv-abc", "confirmed", confirmed=True)
    pays = repo.list_payments(210)
    assert pays[0]["status"] == "confirmed"
    assert pays[0]["confirmed_at"] is not None


# ─────────────────────── audit ───────────────────────

def test_audit_written_and_readable(repo: Repository) -> None:
    repo.upsert_user(220, None, STATUS_ACTIVE)
    repo.audit("test_action", 220, {"key": "value"})
    rows = repo.recent_audit(5)
    assert rows[0]["action"] == "test_action"
    assert "key" in rows[0]["details"]


# ─────────────────────── stats_counts ───────────────────────

def test_stats_counts(repo: Repository) -> None:
    repo.upsert_user(230, None, STATUS_ACTIVE)
    repo.upsert_user(231, None, STATUS_PENDING)
    repo.add_traffic(230, 5_000_000)
    st = repo.stats_counts()
    assert st["by_status"][STATUS_ACTIVE] == 1
    assert st["by_status"][STATUS_PENDING] == 1
    assert st["total_traffic_bytes"] == 5_000_000
