"""Tests for src/payments/cryptocloud.py (HTTP mocked with respx)."""

import json

import httpx
import pytest
import respx

from src.payments.cryptocloud import (
    CREATE_URL,
    ERROR_MESSAGE,
    INFO_URL,
    CryptoCloudProvider,
)
from src.payments.provider import INVOICE_CREATED, INVOICE_UNAVAILABLE

UUID = "INV-ABC12345"
PAY_URL = "https://pay.cryptocloud.plus/invoice/INV-ABC12345"


@pytest.fixture
def provider(minimal_settings):
    from dataclasses import replace

    return CryptoCloudProvider(
        replace(
            minimal_settings,
            payment_provider="cryptocloud",
            cryptocloud_api_key="test-key",
            cryptocloud_shop_id="shop-1",
            cryptocloud_secret="sec",
        )
    )


# ---------- create_invoice ----------


@respx.mock
def test_create_invoice_success(provider) -> None:
    route = respx.post(CREATE_URL).mock(
        return_value=httpx.Response(
            200,
            json={"status": "success", "result": {"uuid": UUID, "link": PAY_URL}},
        )
    )
    result = provider.create_invoice(telegram_id=777, amount_usd=2.99)

    assert result.status == INVOICE_CREATED
    assert result.invoice_id == UUID
    assert result.pay_url == PAY_URL
    assert result.amount_usd == 2.99

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Token test-key"
    body = json.loads(request.content)
    assert body["shop_id"] == "shop-1"
    assert body["amount"] == 2.99
    assert body["currency"] == "USD"
    assert body["order_id"].startswith("777_")  # <telegram_id>_<ts>


@respx.mock
def test_create_invoice_http_error_is_unavailable(provider) -> None:
    respx.post(CREATE_URL).mock(return_value=httpx.Response(401, json={"detail": "bad token"}))
    result = provider.create_invoice(777, 2.99)

    assert result.status == INVOICE_UNAVAILABLE
    assert result.pay_url is None
    assert result.message == ERROR_MESSAGE


@respx.mock
def test_create_invoice_network_error_is_unavailable(provider) -> None:
    respx.post(CREATE_URL).mock(side_effect=httpx.ConnectError("no route"))
    result = provider.create_invoice(777, 2.99)

    assert result.status == INVOICE_UNAVAILABLE
    assert result.pay_url is None


@respx.mock
def test_create_invoice_malformed_response_is_unavailable(provider) -> None:
    # 200, но без uuid/link — шлюз ответил не тем, что ждём
    respx.post(CREATE_URL).mock(
        return_value=httpx.Response(200, json={"status": "error", "result": {}})
    )
    result = provider.create_invoice(777, 2.99)
    assert result.status == INVOICE_UNAVAILABLE


def test_create_invoice_without_credentials_is_unavailable(minimal_settings) -> None:
    # пустые ключи → в сеть не ходим вообще (respx не активен: любой запрос упал бы)
    p = CryptoCloudProvider(minimal_settings)
    result = p.create_invoice(777, 2.99)
    assert result.status == INVOICE_UNAVAILABLE


# ---------- get_invoice_status ----------


@respx.mock
def test_get_invoice_status_paid(provider) -> None:
    respx.post(INFO_URL).mock(
        return_value=httpx.Response(
            200,
            json={"status": "success", "result": [{"uuid": UUID, "status": "paid"}]},
        )
    )
    assert provider.get_invoice_status(UUID) == "paid"


@respx.mock
def test_get_invoice_status_picks_matching_uuid(provider) -> None:
    respx.post(INFO_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "result": [
                    {"uuid": "INV-OTHER", "status": "canceled"},
                    {"uuid": UUID, "status": "created"},
                ],
            },
        )
    )
    assert provider.get_invoice_status(UUID) == "created"


@respx.mock
def test_get_invoice_status_uppercase_normalized(provider) -> None:
    respx.post(INFO_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "success", "result": {"uuid": UUID, "status": "PAID"}}
        )
    )
    assert provider.get_invoice_status(UUID) == "paid"


@respx.mock
def test_get_invoice_status_falls_back_to_get_on_405(provider) -> None:
    respx.post(INFO_URL).mock(return_value=httpx.Response(405))
    get_route = respx.get(INFO_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "success", "result": [{"uuid": UUID, "status": "expired"}]}
        )
    )
    assert provider.get_invoice_status(UUID) == "expired"
    assert get_route.calls.last.request.url.params["uuids[]"] == UUID


@respx.mock
def test_get_invoice_status_http_error_returns_none(provider) -> None:
    respx.post(INFO_URL).mock(return_value=httpx.Response(500, text="boom"))
    assert provider.get_invoice_status(UUID) is None


@respx.mock
def test_get_invoice_status_network_error_returns_none(provider) -> None:
    respx.post(INFO_URL).mock(side_effect=httpx.TimeoutException("timeout"))
    assert provider.get_invoice_status(UUID) is None


@respx.mock
def test_get_invoice_status_malformed_returns_none(provider) -> None:
    respx.post(INFO_URL).mock(
        return_value=httpx.Response(200, json={"status": "error", "result": []})
    )
    assert provider.get_invoice_status(UUID) is None


@respx.mock
def test_get_invoice_status_non_json_returns_none(provider) -> None:
    respx.post(INFO_URL).mock(return_value=httpx.Response(200, text="<html>502</html>"))
    assert provider.get_invoice_status(UUID) is None


# ---------- прочее ----------


def test_verify_webhook_not_implemented(provider) -> None:
    with pytest.raises(NotImplementedError):
        provider.verify_webhook(request=None)


def test_name() -> None:
    assert CryptoCloudProvider.name == "cryptocloud"


def test_factory_returns_cryptocloud(minimal_settings) -> None:
    from dataclasses import replace

    from src.config import get_payment_provider

    p = get_payment_provider(replace(minimal_settings, payment_provider="cryptocloud"))
    assert isinstance(p, CryptoCloudProvider)
