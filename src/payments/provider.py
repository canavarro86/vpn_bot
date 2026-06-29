"""Абстрактный интерфейс платёжного провайдера (ТЗ раздел 5).

Вся бизнес-логика (handlers, jobs) обращается ТОЛЬКО к `PaymentProvider`,
никогда напрямую к конкретной реализации (stub/cryptocloud). Подключение
реального шлюза = новый файл + правка фабрики в config.py, остальной код не
меняется.

Тип запроса вебхука намеренно `Any` — чтобы интерфейс не тянул зависимость от
конкретного веб-фреймворка (FastAPI/aiohttp). Реальный провайдер сам решает,
что ему нужно из запроса (тело + заголовки подписи).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

# Статусы счёта — единые для всех провайдеров (мэппинг провайдер→эти значения
# делается внутри конкретной реализации).
INVOICE_CREATED = "created"
INVOICE_PENDING = "pending"
INVOICE_CONFIRMED = "confirmed"
INVOICE_FAILED = "failed"
INVOICE_EXPIRED = "expired"
INVOICE_UNAVAILABLE = "unavailable"  # шлюз не подключён (stub)


@dataclass
class InvoiceResult:
    """Результат создания счёта. `pay_url` = None, если оплата недоступна
    (stub) — тогда `message` содержит текст для показа пользователю."""

    invoice_id: str
    amount_usd: float
    status: str
    pay_url: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WebhookEvent:
    """Нормализованное событие из вебхука провайдера, после проверки подписи."""

    invoice_id: str
    status: str
    telegram_id: Optional[int] = None
    amount_usd: Optional[float] = None
    raw: Optional[dict] = None


@runtime_checkable
class PaymentProvider(Protocol):
    name: str

    def create_invoice(self, telegram_id: int, amount_usd: float) -> InvoiceResult:
        """Создаёт счёт на оплату. Не бросает на штатных путях — недоступность
        выражается через status=INVOICE_UNAVAILABLE + message."""
        ...

    def verify_webhook(self, request: Any) -> WebhookEvent:
        """Проверяет подпись вебхука и возвращает нормализованное событие.
        ОБЯЗАН верифицировать подпись до возврата любого статуса оплаты."""
        ...
