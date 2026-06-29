#!/usr/bin/env python3
"""Одноразовая миграция legacy JSON → новая SQLite (ТЗ раздел 6).

Переносится ТОЛЬКО факт существования пользователя / статус / тариф / баны.
Старые Outline-ключи НЕ переносятся — движок другой, VPN-клиент создаётся
заново через vpn_engine при следующем /get.

Источники в DATA_DIR:
  - outline_keys.json  → пользователи со статусом active, tier free
  - pending_ids.json   → 140 id со статусом pending_subscription (БЕЗ сообщений)
  - bans.json          → таблица bans + статус banned

Идемпотентно: повторный запуск не плодит дубли и не понижает active→pending.
По умолчанию --dry-run (только показывает план). Реальная запись: --apply.

Запуск:
    python -m scripts.migrate_legacy_json --apply
    python -m scripts.migrate_legacy_json --data-dir ./data --db ./data/hideway.db --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Позволяет запуск как `python scripts/migrate_legacy_json.py` и как модуль.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import repository as repo_mod  # noqa: E402
from src.db.repository import (  # noqa: E402
    Repository,
    STATUS_ACTIVE,
    STATUS_BANNED,
    STATUS_PENDING,
    TIER_FREE,
)


def _load_json(path: Path):
    if not path.exists():
        print(f"  — {path.name}: нет файла, пропуск")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  ! {path.name}: невалидный JSON ({e}) — пропуск")
        return None


def _iter_ids_with_meta(data):
    """Нормализует разные формы legacy JSON в (telegram_id, meta_dict).

    Поддержка:
      - {"123": {...}, ...}          (dict id→meta)
      - [123, 456, ...]              (список id)
      - [{"telegram_id": 123, ...}]  (список объектов)
    """
    if data is None:
        return
    if isinstance(data, dict):
        for k, v in data.items():
            try:
                tid = int(k)
            except (TypeError, ValueError):
                # ключ не id — возможно meta содержит id
                if isinstance(v, dict) and "telegram_id" in v:
                    tid = int(v["telegram_id"])
                else:
                    continue
            yield tid, (v if isinstance(v, dict) else {})
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                tid = item.get("telegram_id") or item.get("id") or item.get("user_id")
                if tid is None:
                    continue
                yield int(tid), item
            else:
                try:
                    yield int(item), {}
                except (TypeError, ValueError):
                    continue


def migrate(data_dir: Path, db_path: Path, apply: bool) -> None:
    repo = Repository(db_path)
    repo.init_schema()

    plan = {"active": 0, "pending": 0, "banned": 0, "skipped_existing": 0}

    # 1) outline_keys.json → active/free
    print("outline_keys.json → active users:")
    keys_data = _load_json(data_dir / "outline_keys.json")
    for tid, meta in _iter_ids_with_meta(keys_data):
        username = meta.get("username")
        existing = repo.get_user(tid)
        if existing and existing.status == STATUS_ACTIVE:
            plan["skipped_existing"] += 1
            continue
        plan["active"] += 1
        if apply:
            repo.upsert_user(tid, username, STATUS_ACTIVE, TIER_FREE)
            repo.audit("migrated_active", telegram_id=tid, details={"source": "outline_keys.json"})

    # 2) pending_ids.json → pending_subscription (не понижаем уже active)
    print("pending_ids.json → pending_subscription:")
    pending_data = _load_json(data_dir / "pending_ids.json")
    for tid, meta in _iter_ids_with_meta(pending_data):
        existing = repo.get_user(tid)
        if existing is not None:
            plan["skipped_existing"] += 1
            continue
        plan["pending"] += 1
        if apply:
            repo.upsert_user(tid, meta.get("username"), STATUS_PENDING, TIER_FREE)
            repo.audit("migrated_pending", telegram_id=tid, details={"source": "pending_ids.json"})

    # 3) bans.json → bans + статус banned
    print("bans.json → bans:")
    bans_data = _load_json(data_dir / "bans.json")
    for tid, meta in _iter_ids_with_meta(bans_data):
        reason = meta.get("reason") if isinstance(meta, dict) else None
        expires = meta.get("expires_at") if isinstance(meta, dict) else None
        plan["banned"] += 1
        if apply:
            # пользователь обязан существовать для FK-чистоты статуса
            if repo.get_user(tid) is None:
                repo.upsert_user(tid, None, STATUS_BANNED, TIER_FREE)
            else:
                repo.set_status(tid, STATUS_BANNED)
            repo.add_ban(tid, reason=reason, expires_at=expires)
            repo.audit("migrated_ban", telegram_id=tid, details={"source": "bans.json"})

    repo.close()

    mode = "APPLIED" if apply else "DRY-RUN (ничего не записано, добавь --apply)"
    print("\n=== %s ===" % mode)
    print(f"  active(new/updated): {plan['active']}")
    print(f"  pending(new):        {plan['pending']}")
    print(f"  banned:              {plan['banned']}")
    print(f"  skipped(existing):   {plan['skipped_existing']}")


def main() -> None:
    p = argparse.ArgumentParser(description="Миграция legacy JSON → SQLite")
    p.add_argument("--data-dir", default=None, help="каталог с legacy JSON (по умолч. из .env DATA_DIR)")
    p.add_argument("--db", default=None, help="путь к hideway.db (по умолч. DATA_DIR/hideway.db)")
    p.add_argument("--apply", action="store_true", help="реально записать (иначе dry-run)")
    args = p.parse_args()

    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        from src.config import load_settings
        data_dir = load_settings().data_dir

    db_path = Path(args.db) if args.db else data_dir / "hideway.db"
    print(f"data_dir = {data_dir}")
    print(f"db       = {db_path}\n")
    migrate(data_dir, db_path, apply=args.apply)


if __name__ == "__main__":
    main()
