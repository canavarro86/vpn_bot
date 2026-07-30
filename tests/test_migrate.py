"""Tests for scripts/migrate_legacy_json.py.

Tests use in-process calls to migrate() directly (not subprocess).
Covers: dry-run, --apply, idempotency, partial data (missing files).
"""

import json
from pathlib import Path

import pytest

from scripts.migrate_legacy_json import migrate, ADMIN_ID
from src.db.repository import (
    Repository,
    STATUS_ACTIVE,
    STATUS_BANNED,
    STATUS_UNDER_APPROVE,
    TIER_FREE,
)

SAMPLE_KEYS = {
    "users": {
        str(ADMIN_ID): {"key_id": "1", "access_url": "ss://example"},
        "111111111": {"key_id": "2", "access_url": "ss://example2"},
        "222222222": {"key_id": "3", "access_url": "ss://example3"},
    }
}

SAMPLE_BANS = {
    "333333333": {"until": 0, "last": 9999, "viol": []},         # permanent ban
    "444444444": {"until": 9_999_999_999, "last": 0, "viol": []}, # future expiry
    "111111111": {"until": 0},                                    # overlaps with keys
}


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    (tmp_path / "outline_keys.json").write_text(json.dumps(SAMPLE_KEYS))
    (tmp_path / "bans.json").write_text(json.dumps(SAMPLE_BANS))
    return tmp_path


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "migrate_test.db"


# ─────────────────────── dry-run ───────────────────────

def test_dry_run_does_not_write_users(data_dir: Path, db: Path) -> None:
    ret = migrate(data_dir, db, apply=False)
    assert ret == 0
    repo = Repository(db)
    repo.init_schema()
    assert repo.count_users() == 0
    repo.close()


def test_dry_run_returns_zero(data_dir: Path, db: Path) -> None:
    assert migrate(data_dir, db, apply=False) == 0


# ─────────────────────── apply ───────────────────────

def test_apply_creates_correct_statuses(data_dir: Path, db: Path) -> None:
    migrate(data_dir, db, apply=True)
    repo = Repository(db)
    repo.init_schema()

    # Admin → active
    admin = repo.get_user(ADMIN_ID)
    assert admin is not None
    assert admin.status == STATUS_ACTIVE

    # Regular outline user → under_approve
    u = repo.get_user(111111111)
    assert u is not None
    # 111111111 is in bans too → banned overrides under_approve
    assert u.status == STATUS_BANNED

    u2 = repo.get_user(222222222)
    assert u2 is not None
    assert u2.status == STATUS_UNDER_APPROVE

    # Ban-only user (not in outline_keys)
    u3 = repo.get_user(333333333)
    assert u3 is not None
    assert u3.status == STATUS_BANNED

    repo.close()


def test_apply_writes_ban_records(data_dir: Path, db: Path) -> None:
    migrate(data_dir, db, apply=True)
    repo = Repository(db)
    repo.init_schema()

    # Permanent ban: until=0 → expires_at=NULL
    ban333 = repo.get_ban(333333333)
    assert ban333 is not None
    assert ban333["expires_at"] is None

    # Timed ban: until > 0
    ban444 = repo.get_ban(444444444)
    assert ban444 is not None
    assert ban444["expires_at"] == 9_999_999_999

    repo.close()


def test_apply_all_users_tier_free(data_dir: Path, db: Path) -> None:
    migrate(data_dir, db, apply=True)
    repo = Repository(db)
    repo.init_schema()
    for u in repo.list_all_users():
        assert u.tier == TIER_FREE
        assert u.vpn_client_id is None  # ss:// keys NOT migrated
    repo.close()


def test_apply_writes_audit_entries(data_dir: Path, db: Path) -> None:
    migrate(data_dir, db, apply=True)
    repo = Repository(db)
    repo.init_schema()
    rows = repo.recent_audit(100)
    actions = {r["action"] for r in rows}
    assert "migrated_user" in actions
    assert "migrated_ban" in actions
    repo.close()


# ─────────────────────── idempotency ───────────────────────

def test_apply_is_idempotent(data_dir: Path, db: Path) -> None:
    """Second --apply call is a no-op (DB already populated)."""
    migrate(data_dir, db, apply=True)
    repo1 = Repository(db)
    repo1.init_schema()
    count_after_first = repo1.count_users()
    repo1.close()

    migrate(data_dir, db, apply=True)
    repo2 = Repository(db)
    repo2.init_schema()
    count_after_second = repo2.count_users()
    repo2.close()

    assert count_after_first == count_after_second


# ─────────────────────── missing files ───────────────────────

def test_migrate_without_keys_file(tmp_path: Path) -> None:
    (tmp_path / "bans.json").write_text(json.dumps({"555": {"until": 0}}))
    db = tmp_path / "no_keys.db"
    ret = migrate(tmp_path, db, apply=True)
    assert ret == 0
    repo = Repository(db)
    repo.init_schema()
    u = repo.get_user(555)
    assert u is not None
    assert u.status == STATUS_BANNED
    repo.close()


def test_migrate_without_bans_file(tmp_path: Path) -> None:
    (tmp_path / "outline_keys.json").write_text(json.dumps(SAMPLE_KEYS))
    db = tmp_path / "no_bans.db"
    ret = migrate(tmp_path, db, apply=True)
    assert ret == 0
    repo = Repository(db)
    repo.init_schema()
    assert repo.count_users() == 3
    repo.close()


def test_migrate_empty_data_dir(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    ret = migrate(tmp_path, db, apply=True)
    assert ret == 0
    repo = Repository(db)
    repo.init_schema()
    assert repo.count_users() == 0
    repo.close()
