#!/usr/bin/env python3
"""Одноразовая миграция legacy JSON → новая SQLite (ТЗ задача 2).

Переносится ТОЛЬКО факт существования пользователя / статус / тариф / баны.
Старые Outline-ключи (ss://) НЕ переносятся — движок другой, VPN-клиент
создаётся заново через vpn_engine при следующем /get после апрува.

Источники в DATA_DIR:
  - outline_keys.json  → формат {"users": {"<id>": {...}}}
      * owner_user_id == ADMIN (1393616622) → status active, tier free (владелец)
      * остальные                           → status under_approve (ждут апрува)
  - bans.json          → формат {"<id>": {"until": <ts|0>, "last":..., "viol":[...]}}
      * until == 0  → permanent ban (expires_at = NULL)
      * until > 0   → expires_at = until
      * забаненного создаём в users со статусом banned, если его нет

traffic_state.json и audit.log.jsonl НЕ мигрируются (ТЗ 2.3).

Идемпотентность (ТЗ 2.4): если в users уже есть строки — миграция пропускается
(exit 0). Так миграция отрабатывает один раз; повторные деплои её не трогают.

По умолчанию --dry-run (только показывает план). Реальная запись: --apply.

Запуск:
    python scripts/migrate_legacy_json.py --apply
    python scripts/migrate_legacy_json.py --data-dir ./DB_ARH --db /tmp/test.db --apply
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
    STATUS_UNDER_APPROVE,
    TIER_FREE,
)

ADMIN_ID = 1393616622
BAN_REASON = "migrated from legacy bans.json"


def _load_json(path: Path):
    if not path.exists():
        print(f"  — {path.name}: нет файла, пропуск")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  ! {path.name}: невалидный JSON ({e}) — пропуск")
        return None


def _unwrap_users(data):
    """Разворачивает обёртку {"users": {...}} legacy outline_keys.json.
    Если ключа "users" нет — возвращает data как есть."""
    if isinstance(data, dict) and set(data.keys()) == {"users"} and isinstance(
        data["users"], dict
    ):
        return data["users"]
    return data


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


def migrate(data_dir: Path, db_path: Path, apply: bool) -> int:
    repo = Repository(db_path)
    repo.init_schema()

    # --- Идемпотентность (ТЗ 2.4): непустая БД → не трогаем ---
    existing_count = repo.count_users()
    if existing_count > 0:
        print(f"DB already populated ({existing_count} users), skipping migration")
        repo.close()
        return 0

    # Итоговый статус каждого id считаем в памяти (bans перекрывают outline),
    # чтобы план dry-run и --apply давали одинаковые цифры независимо от записи.
    final_status: dict[int, str] = {}
    usernames: dict[int, str | None] = {}
    ban_rows: dict[int, int | None] = {}  # tid → expires_at (None=permanent)

    # 1) outline_keys.json → active (админ) / under_approve (остальные)
    print("outline_keys.json → active(admin) / under_approve:")
    keys_data = _unwrap_users(_load_json(data_dir / "outline_keys.json"))
    for tid, meta in _iter_ids_with_meta(keys_data):
        owner = meta.get("owner_user_id", tid)
        final_status[tid] = STATUS_ACTIVE if owner == ADMIN_ID else STATUS_UNDER_APPROVE
        usernames[tid] = meta.get("username")

    # 2) bans.json → banned (перекрывает статус из outline_keys)
    print("bans.json → bans:")
    bans_data = _load_json(data_dir / "bans.json")
    for tid, meta in _iter_ids_with_meta(bans_data):
        until = meta.get("until", 0) if isinstance(meta, dict) else 0
        try:
            until = int(until)
        except (TypeError, ValueError):
            until = 0
        ban_rows[tid] = until if until > 0 else None  # 0 → permanent (NULL)
        final_status[tid] = STATUS_BANNED
        usernames.setdefault(tid, None)

    # --- запись ---
    if apply:
        for tid, status in final_status.items():
            # access_url (ss:// Outline) НЕ переносим — другой движок
            repo.upsert_user(tid, usernames.get(tid), status, TIER_FREE)
            repo.audit("migrated_user", telegram_id=tid, details={"status": status})
        for tid, expires in ban_rows.items():
            repo.add_ban(tid, reason=BAN_REASON, expires_at=expires)
            repo.audit("migrated_ban", telegram_id=tid, details={"expires_at": expires})

    counts = {STATUS_ACTIVE: 0, STATUS_UNDER_APPROVE: 0, STATUS_BANNED: 0}
    for status in final_status.values():
        counts[status] = counts.get(status, 0) + 1
    perm_bans = sum(1 for e in ban_rows.values() if e is None)
    final_users = repo.count_users() if apply else len(final_status)
    repo.close()

    mode = "APPLIED" if apply else "DRY-RUN (ничего не записано, добавь --apply)"
    print("\n=== %s ===" % mode)
    print(f"  active:        {counts[STATUS_ACTIVE]}")
    print(f"  under_approve: {counts[STATUS_UNDER_APPROVE]}")
    print(f"  banned:        {counts[STATUS_BANNED]} (permanent: {perm_bans})")
    print(f"  users total:   {final_users}")
    return 0


def main() -> int:
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
    return migrate(data_dir, db_path, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
