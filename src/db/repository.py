"""Единственное место SQL в проекте. handlers/jobs не пишут raw SQL.

Синхронный sqlite3 (stdlib). Бот на aiogram async — вызывать методы через
`asyncio.to_thread(repo.method, ...)` из хендлеров; объём нагрузки низкий,
отдельный async-драйвер избыточен.

Все мутации идут внутри `with self._tx() as cur:` — коммит при успехе,
rollback при исключении.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# --- Статусы / тарифы (единые константы, чтобы не плодить строковые литералы) ---
STATUS_PENDING = "pending_subscription"
STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
STATUS_BANNED = "banned"
# under_approve — мигрированный из legacy юзер, ждёт ручного апрува админом
# (/admin_approve → pending_subscription). VPN не выдаётся до апрува.
STATUS_UNDER_APPROVE = "under_approve"

TIER_FREE = "free"
TIER_PAID = "paid"


def now_ts() -> int:
    return int(time.time())


@dataclass
class User:
    telegram_id: int
    username: Optional[str]
    status: str
    tier: str
    vpn_client_id: Optional[str]
    access_url: Optional[str]
    traffic_limit_gb: float
    traffic_used_bytes: int
    paid_until: Optional[int]
    low_traffic_notified: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "User":
        return cls(**{k: row[k] for k in row.keys()})


class Repository:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")

    # ---------- инфраструктура ----------

    def init_schema(self) -> None:
        with self._tx() as cur:
            cur.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    # ---------- users ----------

    def get_user(self, telegram_id: int) -> Optional[User]:
        row = self._conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return User.from_row(row) if row else None

    def upsert_user(
        self,
        telegram_id: int,
        username: Optional[str],
        status: str,
        tier: str = TIER_FREE,
        traffic_limit_gb: Optional[float] = None,
    ) -> User:
        """Создаёт пользователя или обновляет username/status, не затирая
        существующие vpn/traffic/paid поля. Возвращает актуальную запись."""
        ts = now_ts()
        existing = self.get_user(telegram_id)
        with self._tx() as cur:
            if existing is None:
                limit = traffic_limit_gb if traffic_limit_gb is not None else 5.0
                cur.execute(
                    """INSERT INTO users
                       (telegram_id, username, status, tier, traffic_limit_gb,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (telegram_id, username, status, tier, limit, ts, ts),
                )
            else:
                cur.execute(
                    """UPDATE users
                       SET username = COALESCE(?, username),
                           status = ?, tier = ?,
                           traffic_limit_gb = COALESCE(?, traffic_limit_gb),
                           updated_at = ?
                       WHERE telegram_id = ?""",
                    (username, status, tier, traffic_limit_gb, ts, telegram_id),
                )
        user = self.get_user(telegram_id)
        assert user is not None
        return user

    def set_status(self, telegram_id: int, status: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE users SET status = ?, updated_at = ? WHERE telegram_id = ?",
                (status, now_ts(), telegram_id),
            )

    def set_vpn_client(
        self, telegram_id: int, vpn_client_id: Optional[str], access_url: Optional[str]
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                """UPDATE users SET vpn_client_id = ?, access_url = ?, updated_at = ?
                   WHERE telegram_id = ?""",
                (vpn_client_id, access_url, now_ts(), telegram_id),
            )

    def set_tier(
        self,
        telegram_id: int,
        tier: str,
        traffic_limit_gb: float,
        paid_until: Optional[int],
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                """UPDATE users
                   SET tier = ?, traffic_limit_gb = ?, paid_until = ?, updated_at = ?
                   WHERE telegram_id = ?""",
                (tier, traffic_limit_gb, paid_until, now_ts(), telegram_id),
            )

    def add_traffic(self, telegram_id: int, delta_bytes: int) -> None:
        with self._tx() as cur:
            cur.execute(
                """UPDATE users
                   SET traffic_used_bytes = traffic_used_bytes + ?, updated_at = ?
                   WHERE telegram_id = ?""",
                (delta_bytes, now_ts(), telegram_id),
            )

    def set_traffic_used(self, telegram_id: int, total_bytes: int) -> None:
        with self._tx() as cur:
            cur.execute(
                """UPDATE users SET traffic_used_bytes = ?, updated_at = ?
                   WHERE telegram_id = ?""",
                (total_bytes, now_ts(), telegram_id),
            )

    def reset_traffic_period(self, telegram_id: int) -> None:
        """Сброс счётчика трафика и флага уведомления — начало нового периода."""
        with self._tx() as cur:
            cur.execute(
                """UPDATE users
                   SET traffic_used_bytes = 0, low_traffic_notified = 0, updated_at = ?
                   WHERE telegram_id = ?""",
                (now_ts(), telegram_id),
            )

    def set_low_traffic_notified(self, telegram_id: int, value: int = 1) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE users SET low_traffic_notified = ?, updated_at = ? WHERE telegram_id = ?",
                (value, now_ts(), telegram_id),
            )

    def list_users_by_status(self, status: str) -> list[User]:
        rows = self._conn.execute(
            "SELECT * FROM users WHERE status = ? ORDER BY created_at", (status,)
        ).fetchall()
        return [User.from_row(r) for r in rows]

    def count_users(self, status: Optional[str] = None) -> int:
        if status is None:
            row = self._conn.execute("SELECT COUNT(*) c FROM users").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) c FROM users WHERE status = ?", (status,)
            ).fetchone()
        return int(row["c"])

    def list_all_users(
        self, status: Optional[str] = None, limit: int = 30, offset: int = 0
    ) -> list[User]:
        """Постранично все пользователи (для /admin_list). Сорт по created_at."""
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM users ORDER BY created_at LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE status = ? ORDER BY created_at LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        return [User.from_row(r) for r in rows]

    def delete_user(self, telegram_id: int) -> bool:
        """Полное удаление юзера + зависимых записей (payments, connection_log,
        bans). audit_log сохраняется (telegram_id остаётся как историческая
        ссылка). Возвращает True, если юзер существовал. Транзакция: дочерние
        строки удаляются перед родителем (FK ON)."""
        existing = self.get_user(telegram_id)
        with self._tx() as cur:
            cur.execute("DELETE FROM payments WHERE telegram_id = ?", (telegram_id,))
            cur.execute("DELETE FROM connection_log WHERE telegram_id = ?", (telegram_id,))
            cur.execute("DELETE FROM bans WHERE telegram_id = ?", (telegram_id,))
            cur.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        return existing is not None

    def traffic_top(self, limit: int = 10) -> list[User]:
        rows = self._conn.execute(
            "SELECT * FROM users ORDER BY traffic_used_bytes DESC LIMIT ?", (limit,)
        ).fetchall()
        return [User.from_row(r) for r in rows]

    def paid_summary(self, as_of: Optional[int] = None) -> dict[str, Any]:
        """Сводка по активным paid-подпискам: число активных и сумма
        оставшихся оплаченных дней."""
        cutoff = as_of if as_of is not None else now_ts()
        row = self._conn.execute(
            """SELECT COUNT(*) c,
                      COALESCE(SUM(paid_until - ?), 0) remaining_seconds
               FROM users
               WHERE tier = ? AND paid_until IS NOT NULL AND paid_until > ?""",
            (cutoff, TIER_PAID, cutoff),
        ).fetchone()
        return {
            "active_paid": int(row["c"]),
            "remaining_days": int(row["remaining_seconds"]) // 86400,
        }

    def list_active_with_client(self) -> list[User]:
        rows = self._conn.execute(
            "SELECT * FROM users WHERE status = ? AND vpn_client_id IS NOT NULL",
            (STATUS_ACTIVE,),
        ).fetchall()
        return [User.from_row(r) for r in rows]

    def list_paid_expired(self, as_of: Optional[int] = None) -> list[User]:
        cutoff = as_of if as_of is not None else now_ts()
        rows = self._conn.execute(
            """SELECT * FROM users
               WHERE tier = ? AND paid_until IS NOT NULL AND paid_until <= ?""",
            (TIER_PAID, cutoff),
        ).fetchall()
        return [User.from_row(r) for r in rows]

    def stats_counts(self) -> dict[str, Any]:
        """Сводка для /admin_stats: разбивка по статусам и тарифам + суммарный трафик."""
        out: dict[str, Any] = {"by_status": {}, "by_tier": {}}
        for row in self._conn.execute(
            "SELECT status, COUNT(*) c FROM users GROUP BY status"
        ):
            out["by_status"][row["status"]] = row["c"]
        for row in self._conn.execute(
            "SELECT tier, COUNT(*) c FROM users GROUP BY tier"
        ):
            out["by_tier"][row["tier"]] = row["c"]
        total = self._conn.execute(
            "SELECT COALESCE(SUM(traffic_used_bytes),0) t FROM users"
        ).fetchone()["t"]
        out["total_traffic_bytes"] = total
        return out

    # ---------- payments ----------

    def create_payment(
        self,
        telegram_id: int,
        provider: str,
        provider_invoice_id: str,
        amount_usd: float,
        status: str = "created",
    ) -> int:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO payments
                   (telegram_id, provider, provider_invoice_id, amount_usd, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (telegram_id, provider, provider_invoice_id, amount_usd, status, now_ts()),
            )
            return int(cur.lastrowid)

    def set_payment_status(
        self, provider_invoice_id: str, status: str, confirmed: bool = False
    ) -> None:
        confirmed_at = now_ts() if confirmed else None
        with self._tx() as cur:
            cur.execute(
                """UPDATE payments
                   SET status = ?, confirmed_at = COALESCE(?, confirmed_at)
                   WHERE provider_invoice_id = ?""",
                (status, confirmed_at, provider_invoice_id),
            )

    def list_payments(self, telegram_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM payments WHERE telegram_id = ? ORDER BY created_at DESC",
            (telegram_id,),
        ).fetchall()

    # ---------- connection_log (privacy-first, ТЗ раздел 4) ----------

    def log_connection(
        self,
        telegram_id: int,
        bytes_used: int,
        ip_address: Optional[str] = None,
        sampled_at: Optional[int] = None,
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO connection_log (telegram_id, ip_address, bytes_used, sampled_at)
                   VALUES (?, ?, ?, ?)""",
                (telegram_id, ip_address, bytes_used, sampled_at or now_ts()),
            )

    def last_ip(self, telegram_id: int) -> Optional[str]:
        row = self._conn.execute(
            """SELECT ip_address FROM connection_log
               WHERE telegram_id = ? AND ip_address IS NOT NULL
               ORDER BY sampled_at DESC LIMIT 1""",
            (telegram_id,),
        ).fetchone()
        return row["ip_address"] if row else None

    # ---------- bans ----------

    def add_ban(
        self,
        telegram_id: int,
        reason: Optional[str] = None,
        expires_at: Optional[int] = None,
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO bans (telegram_id, reason, banned_at, expires_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                       reason = excluded.reason,
                       banned_at = excluded.banned_at,
                       expires_at = excluded.expires_at""",
                (telegram_id, reason, now_ts(), expires_at),
            )

    def remove_ban(self, telegram_id: int) -> None:
        with self._tx() as cur:
            cur.execute("DELETE FROM bans WHERE telegram_id = ?", (telegram_id,))

    def get_ban(self, telegram_id: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM bans WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()

    def is_banned(self, telegram_id: int, as_of: Optional[int] = None) -> bool:
        row = self.get_ban(telegram_id)
        if row is None:
            return False
        exp = row["expires_at"]
        if exp is None:
            return True
        return (as_of or now_ts()) < exp

    # ---------- audit_log ----------

    def audit(
        self,
        action: str,
        telegram_id: Optional[int] = None,
        details: Optional[dict] = None,
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO audit_log (telegram_id, action, details, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    telegram_id,
                    action,
                    json.dumps(details, ensure_ascii=False) if details else None,
                    now_ts(),
                ),
            )

    def recent_audit(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
