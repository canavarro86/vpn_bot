"""Платёжный провайдер CryptoCloud (cryptocloud.plus), режим БЕЗ вебхука.

У сервера нет домена/HTTPS, поэтому подтверждение оплаты идёт **поллингом**:
фоновая задача `jobs.check_pending_payments` раз в 30 секунд опрашивает статусы
незакрытых счетов через `get_invoice_status`. `verify_webhook` намеренно не
реализован.

Методы синхронные (httpx.Client) — вызывающий код уже оборачивает провайдера в
`asyncio.to_thread(...)`, как и Repository (см. handlers._handle_upgrade).

Сетевые ошибки наружу не поднимаются:
  - create_invoice → InvoiceResult(status=INVOICE_UNAVAILABLE, message=...)
  - get_invoice_status → None
Это требование интерфейса (provider.py) и защита фоновой задачи от падения.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from ..config import Settings
from .provider import INVOICE_CREATED, INVOICE_UNAVAILABLE, InvoiceResult, WebhookEvent

log = logging.getLogger(__name__)

API_BASE = "https://api.cryptocloud.plus/v2"
CREATE_URL = f"{API_BASE}/invoice/create"
INFO_URL = f"{API_BASE}/invoice/merchant/info"
TIMEOUT_SECONDS = 10.0

# Статусы счёта на стороне CryptoCloud (нормализованные к нижнему регистру).
CC_CREATED = "created"
CC_PAID = "paid"
CC_OVERPAID = "overpaid"
CC_PARTIAL = "partial"
CC_CANCELED = "canceled"
CC_EXPIRED = "expired"

# Какие статусы CryptoCloud считаем успешной оплатой (см. jobs.check_pending_payments).
CC_SUCCESS = frozenset({CC_PAID, CC_OVERPAID})
# Терминальные неуспешные — счёт закрыт, поллинг прекращаем.
CC_DEAD = frozenset({CC_CANCELED, CC_EXPIRED})

ERROR_MESSAGE = (
    "Не удалось создать счёт на оплату — платёжный шлюз недоступен. "
    "Попробуйте позже или обратитесь к администратору."
)


class CryptoCloudProvider:
    name = "cryptocloud"

    def __init__(self, settings: Settings):
        self.api_key = settings.cryptocloud_api_key
        self.shop_id = settings.cryptocloud_shop_id
        # secret нужен только для проверки подписи вебхука; в polling-режиме
        # не используется, храним для будущего включения вебхука.
        self.secret = settings.cryptocloud_secret

    # ---------- инфраструктура ----------

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self.api_key}"}

    def _configured(self) -> bool:
        return bool(self.api_key and self.shop_id)

    # ---------- API ----------

    def create_invoice(self, telegram_id: int, amount_usd: float) -> InvoiceResult:
        """POST /v2/invoice/create → InvoiceResult с uuid счёта и ссылкой на оплату.

        order_id = "<telegram_id>_<unix_ts>" — по нему платёж сопоставим с юзером
        даже вне нашей БД (в личном кабинете CryptoCloud).
        Запись в таблицу payments делает вызывающий код (handlers._handle_upgrade),
        чтобы весь SQL оставался в Repository.
        """
        order_id = f"{telegram_id}_{int(time.time())}"
        if not self._configured():
            log.error("CryptoCloud не настроен: пустой CRYPTOCLOUD_API_KEY/SHOP_ID")
            return self._unavailable(order_id, amount_usd)

        payload = {
            "shop_id": self.shop_id,
            "amount": amount_usd,
            "currency": "USD",
            "order_id": order_id,
        }
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                resp = client.post(CREATE_URL, json=payload, headers=self._headers)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            log.error("CryptoCloud create_invoice (%s) не удался: %s", order_id, e)
            return self._unavailable(order_id, amount_usd)

        result = data.get("result") or {}
        invoice_id = result.get("uuid")
        pay_url = result.get("link")
        if data.get("status") != "success" or not invoice_id or not pay_url:
            log.error("CryptoCloud create_invoice (%s): неожиданный ответ %s", order_id, data)
            return self._unavailable(order_id, amount_usd)

        log.info("CryptoCloud счёт %s создан для %s ($%.2f)", invoice_id, telegram_id, amount_usd)
        return InvoiceResult(
            invoice_id=str(invoice_id),
            amount_usd=amount_usd,
            status=INVOICE_CREATED,
            pay_url=str(pay_url),
        )

    def get_invoice_status(self, invoice_id: str) -> Optional[str]:
        """Статус счёта в терминах CryptoCloud (created/paid/overpaid/canceled/…),
        в нижнем регистре. `None` — статус выяснить не удалось (сеть/невалидный
        ответ); вызывающий код должен просто повторить попытку позже.

        Документированный v2-эндпоинт — POST /v2/invoice/merchant/info с телом
        {"uuids": [...]}. Если шлюз ответит 404/405 (старый GET-контракт с
        `uuids[]=` в query), пробуем GET.
        """
        if not self._configured():
            return None
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                resp = client.post(
                    INFO_URL, json={"uuids": [invoice_id]}, headers=self._headers
                )
                if resp.status_code in (404, 405):
                    resp = client.get(
                        INFO_URL, params={"uuids[]": invoice_id}, headers=self._headers
                    )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            log.error("CryptoCloud get_invoice_status (%s) не удался: %s", invoice_id, e)
            return None

        status = self._extract_status(data, invoice_id)
        if status is None:
            log.error(
                "CryptoCloud get_invoice_status (%s): неожиданный ответ %s", invoice_id, data
            )
        return status

    # ---------- вебхук ----------

    def verify_webhook(self, request: Any) -> WebhookEvent:
        raise NotImplementedError(
            "webhook отключён, используется polling — см. jobs.check_pending_payments"
        )

    # ---------- helpers ----------

    def _unavailable(self, invoice_id: str, amount_usd: float) -> InvoiceResult:
        return InvoiceResult(
            invoice_id=invoice_id,
            amount_usd=amount_usd,
            status=INVOICE_UNAVAILABLE,
            pay_url=None,
            message=ERROR_MESSAGE,
        )

    @staticmethod
    def _extract_status(data: dict, invoice_id: str) -> Optional[str]:
        """Достаёт статус нужного счёта из ответа merchant/info.

        `result` — список счетов (запрашиваем всегда один). Если в списке
        несколько, берём запись с совпадающим uuid.
        """
        if data.get("status") != "success":
            return None
        result = data.get("result")
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list) or not result:
            return None
        item = next(
            (i for i in result if isinstance(i, dict) and i.get("uuid") == invoice_id),
            result[0],
        )
        if not isinstance(item, dict):
            return None
        status = item.get("status")
        return str(status).lower() if status else None
