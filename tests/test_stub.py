"""Tests for src/payments/stub.py."""

import pytest
from src.payments.stub import StubProvider, UNAVAILABLE_MESSAGE
from src.payments.provider import INVOICE_UNAVAILABLE


def test_stub_create_invoice_status_unavailable() -> None:
    p = StubProvider()
    result = p.create_invoice(telegram_id=12345, amount_usd=2.99)
    assert result.status == INVOICE_UNAVAILABLE
    assert result.pay_url is None
    assert result.amount_usd == 2.99


def test_stub_create_invoice_has_invoice_id() -> None:
    p = StubProvider()
    r1 = p.create_invoice(1, 2.99)
    r2 = p.create_invoice(2, 2.99)
    assert r1.invoice_id.startswith("stub-")
    assert r1.invoice_id != r2.invoice_id  # each call generates a unique id


def test_stub_create_invoice_message_non_empty() -> None:
    p = StubProvider()
    result = p.create_invoice(12345, 2.99)
    assert result.message == UNAVAILABLE_MESSAGE
    assert len(result.message) > 0


def test_stub_verify_webhook_raises() -> None:
    p = StubProvider()
    with pytest.raises(NotImplementedError):
        p.verify_webhook(request=None)


def test_stub_name() -> None:
    assert StubProvider.name == "stub"
