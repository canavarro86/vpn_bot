"""Заглушка платёжного провайдера (ТЗ раздел 5).

Реального шлюза пока нет. `create_invoice` не создаёт счёт — возвращает
InvoiceResult со status=unavailable и текстом для пользователя. `verify_webhook`
не реализован (реального вебхука нет) и бросает NotImplementedError —
никакой код не должен на него рассчитывать, пока PAYMENT_PROVIDER=stub.

Замена на реальный шлюз: создать src/payments/cryptocloud.py с тем же
интерфейсом, выставить PAYMENT_PROVIDER=cryptocloud — фабрика в config.py
подхватит, остальной код не меняется.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Any

from .provider import INVOICE_UNAVAILABLE, InvoiceResult, WebhookEvent

UNAVAILABLE_MESSAGE = (
    "Оплата криптовалютой временно недоступна. "
    "Обратитесь к администратору для продления тарифа."
)


class StubProvider:
    name = "stub"

    def create_invoice(self, telegram_id: int, amount_usd: float) -> InvoiceResult:
        # Фиктивный invoice_id — для записи в payments (audit/трассировка),
        # без реальной ссылки на оплату.
        return InvoiceResult(
            invoice_id=f"stub-{uuid_lib.uuid4().hex[:12]}",
            amount_usd=amount_usd,
            status=INVOICE_UNAVAILABLE,
            pay_url=None,
            message=UNAVAILABLE_MESSAGE,
        )

    def verify_webhook(self, request: Any) -> WebhookEvent:
        raise NotImplementedError(
            "stub-провайдер не обрабатывает вебхуки (PAYMENT_PROVIDER=stub)"
        )
