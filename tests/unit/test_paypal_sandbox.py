"""No network calls, browser approvals, or real captures in this suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentsentinel.paypal_sandbox import (
    PayPalSandboxClient, SandboxConfig, SandboxError, UncertainOutcome,
    amount_usd, request_id,
)

CONFIG = SandboxConfig("fake-id", "fake-secret", "MERCHANT12345")
ORDER = {"id": "ORDER123", "status": "CREATED"}
APPROVED = {"id": "ORDER123", "status": "APPROVED", "purchase_units": [{
    "amount": {"currency_code": "USD", "value": "1.00"},
    "payee": {"merchant_id": CONFIG.merchant_id},
}]}
CAPTURED = {"id": "ORDER123", "status": "COMPLETED", "purchase_units": [{
    "payments": {"captures": [{"id": "CAPTURE123", "status": "COMPLETED"}]}
}]}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, headers, body):
        self.calls.append((method, path, headers, body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(*responses):
    fake = FakeTransport((200, {"access_token": "private-token"}), *responses)
    return PayPalSandboxClient(CONFIG, fake), fake


def test_mock_oauth_and_create_request():
    api, fake = client((201, ORDER))
    result = api.create_order("purchase-001", "1.00")
    assert result["order_id"] == "ORDER123"
    assert fake.calls[0][1] == "/v1/oauth2/token"
    assert fake.calls[0][3] == b"grant_type=client_credentials"
    assert fake.calls[0][2]["Authorization"].startswith("Basic ")
    method, path, headers, body = fake.calls[1]
    assert (method, path) == ("POST", "/v2/checkout/orders")
    assert headers["Authorization"] == "Bearer private-token"
    assert headers["PayPal-Request-Id"] == request_id(CONFIG.merchant_id, "create", "purchase-001")
    assert json.loads(body) == {"intent": "CAPTURE", "purchase_units": [{
        "reference_id": "purchase-001", "amount": {"currency_code": "USD", "value": "1.00"},
        "payee": {"merchant_id": CONFIG.merchant_id},
    }]}
    assert "private-token" not in repr(result)


@pytest.mark.parametrize("amount", ["0.00", "10.01", "1", "1.001", "nan", "-1.00", "1e3"])
def test_invalid_amounts(amount):
    with pytest.raises(ValueError):
        amount_usd(amount)


def test_invalid_currency_and_operation_id():
    api, fake = client()
    with pytest.raises(ValueError):
        api.create_order("purchase-001", "1.00", "EUR")
    with pytest.raises(ValueError):
        request_id(CONFIG.merchant_id, "capture", "../../bad")
    assert not fake.calls


def test_missing_credentials_and_merchant_validation():
    with pytest.raises(SandboxError):
        SandboxConfig.from_values({"PAYPAL_ENVIRONMENT": "sandbox"})
    with pytest.raises(SandboxError):
        SandboxConfig.from_values({"PAYPAL_ENVIRONMENT": "live", "PAYPAL_CLIENT_ID": "a",
                                   "PAYPAL_CLIENT_SECRET": "b", "PAYPAL_MERCHANT_ID": "MERCHANT12345"})
    with pytest.raises(SandboxError):
        SandboxConfig.from_values({"PAYPAL_ENVIRONMENT": "sandbox", "PAYPAL_CLIENT_ID": "a",
                                   "PAYPAL_CLIENT_SECRET": "b", "PAYPAL_MERCHANT_ID": "bad"})


def test_oauth_failure_does_not_expose_response():
    api, _ = client()
    api.transport.responses[0] = (401, {"access_token": "secret-value", "message": "private"})
    with pytest.raises(SandboxError, match="OAuth failed") as exc:
        api.create_order("purchase-001", "1.00")
    assert "secret-value" not in str(exc.value)


def test_create_failure_sanitized():
    api, _ = client((422, {"message": "private buyer data"}))
    with pytest.raises(SandboxError, match="HTTP 422") as exc:
        api.create_order("purchase-001", "1.00")
    assert "buyer" not in str(exc.value)


def test_buyer_approval_pending_blocks_capture():
    pending = dict(APPROVED, status="CREATED")
    api, fake = client((200, pending))
    with pytest.raises(SandboxError, match="approval pending"):
        api.capture_order("ORDER123", "capture-001", "1.00")
    assert not any(path.endswith("/capture") for _, path, _, _ in fake.calls)


def test_merchant_mismatch_blocks_capture():
    wrong = dict(APPROVED, purchase_units=[{**APPROVED["purchase_units"][0],
                                           "payee": {"merchant_id": "OTHERMERCHANT"}}])
    api, fake = client((200, wrong))
    with pytest.raises(SandboxError, match="mismatch"):
        api.capture_order("ORDER123", "capture-001", "1.00")
    assert not any(path.endswith("/capture") for _, path, _, _ in fake.calls)


def test_capture_success_and_status_lookup():
    api, fake = client((200, APPROVED), (201, CAPTURED), (200, {"id": "CAPTURE123", "status": "COMPLETED"}))
    result = api.capture_order("ORDER123", "capture-001", "1.00")
    assert result["capture_status"] == "COMPLETED"
    assert fake.calls[2][2]["PayPal-Request-Id"] == request_id(CONFIG.merchant_id, "capture", "capture-001")
    assert fake.calls[2][3] == b"{}"
    assert api.get_capture("CAPTURE123") == {"capture_id": "CAPTURE123", "status": "COMPLETED"}
    assert fake.calls[-1][1] == "/v2/payments/captures/CAPTURE123"


def test_capture_failure_and_timeout():
    api, _ = client((200, APPROVED), (422, {"message": "secret"}))
    with pytest.raises(SandboxError, match="HTTP 422"):
        api.capture_order("ORDER123", "capture-001", "1.00")
    api, fake = client((200, APPROVED), UncertainOutcome("uncertain"))
    with pytest.raises(UncertainOutcome):
        api.capture_order("ORDER123", "capture-001", "1.00")
    assert len(fake.calls) == 3


def test_request_ids_stable_and_distinct():
    first = request_id(CONFIG.merchant_id, "create", "purchase-001")
    assert first == request_id(CONFIG.merchant_id, "create", "purchase-001")
    assert first != request_id(CONFIG.merchant_id, "capture", "purchase-001")
    assert first != request_id(CONFIG.merchant_id, "create", "purchase-002")


def test_approval_link_requires_sandbox_host():
    good = dict(ORDER, links=[{"rel": "approve", "href":
                              "https://www.sandbox.paypal.com/checkoutnow?token=private"}])
    api, _ = client((200, good))
    assert api.approval_link("ORDER123").startswith("https://www.sandbox.paypal.com/")
    bad = dict(ORDER, links=[{"rel": "approve", "href":
                             "https://www.paypal.com/checkoutnow?token=private"}])
    api, _ = client((200, bad))
    with pytest.raises(SandboxError, match="No validated"):
        api.approval_link("ORDER123")


def test_approval_link_accepts_documented_payer_action_required_state():
    pending = dict(ORDER, status="PAYER_ACTION_REQUIRED", links=[
        {"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=private"}
    ])
    api, fake = client((200, pending))
    assert api.approval_link("ORDER123").startswith("https://www.sandbox.paypal.com/")
    assert [call[0] for call in fake.calls] == ["POST", "GET"]
