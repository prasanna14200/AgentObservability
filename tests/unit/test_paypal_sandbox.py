"""No network calls, browser approvals, or real captures in this suite."""

from __future__ import annotations

import json
import hmac
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
CAPTURE_EVIDENCE = {
    "id": "CAPTURE123",
    "status": "COMPLETED",
    "amount": {"currency_code": "USD", "value": "1.00"},
    "supplementary_data": {"related_ids": {"order_id": "ORDER123"}},
}
CAPTURED = {"id": "ORDER123", "status": "COMPLETED", "purchase_units": [{
    "payments": {"captures": [CAPTURE_EVIDENCE]}
}]}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.last_bearer_authorization = None

    def request(self, method, path, headers, body):
        captured_headers = headers.copy()
        if path != "/v1/oauth2/token":
            self.last_bearer_authorization = headers.get("Authorization")
            captured_headers["Authorization"] = "******"
        self.calls.append((method, path, captured_headers, body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(*responses):
    fake = FakeTransport((200, {"access_token": "sandbox-test-token-never-log"}), *responses)
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
    authorization_matches = hmac.compare_digest(
        fake.last_bearer_authorization or "",
        "Bearer sandbox-test-token-never-log",
    )
    assert authorization_matches is True
    assert headers["PayPal-Request-Id"] == request_id(CONFIG.merchant_id, "create", "purchase-001")
    assert json.loads(body) == {"intent": "CAPTURE", "purchase_units": [{
        "reference_id": "purchase-001", "amount": {"currency_code": "USD", "value": "1.00"},
        "payee": {"merchant_id": CONFIG.merchant_id},
    }]}
    token_exposed = "sandbox-test-token-never-log" in repr(result)
    assert token_exposed is False


def test_authenticated_api_call_passes_bearer_token_to_fake_transport_without_logging(caplog):
    test_token = "unit-test-oauth-token-never-log"
    transport = FakeTransport(
        (200, {"access_token": test_token}),
        (200, ORDER),
    )
    api = PayPalSandboxClient(CONFIG, transport)

    result = api.get_order("ORDER123")

    captured_authorization = transport.last_bearer_authorization
    expected_authorization = f"Bearer {test_token}"
    authorization_matches = hmac.compare_digest(
        captured_authorization or "",
        expected_authorization,
    )
    assert authorization_matches is True
    token_in_result = test_token in repr(result)
    token_in_logs = any(
        test_token in record.getMessage()
        for record in caplog.records
    )
    assert token_in_result is False
    assert token_in_logs is False


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


@pytest.mark.parametrize(
    "missing_field",
    ["PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_MERCHANT_ID"],
)
def test_missing_each_sandbox_credential_fails_without_exposing_values(missing_field):
    values = {
        "PAYPAL_ENVIRONMENT": "sandbox",
        "PAYPAL_CLIENT_ID": "test-client-id-not-secret",
        "PAYPAL_CLIENT_SECRET": "test-client-secret-not-real",
        "PAYPAL_MERCHANT_ID": CONFIG.merchant_id,
    }
    values.pop(missing_field)

    with pytest.raises(SandboxError) as exc_info:
        SandboxConfig.from_values(values)

    message = str(exc_info.value)
    value_exposed = any(
        value in message
        for value in (
            "test-client-id-not-secret",
            "test-client-secret-not-real",
            CONFIG.merchant_id,
        )
    )
    assert value_exposed is False


def test_oauth_failure_does_not_expose_response():
    test_token = "oauth-failure-token-never-log"
    api, fake = client()
    api.transport.responses[0] = (
        401,
        {
            "access_token": test_token,
            "client_secret": "test-client-secret-not-real",
            "message": "private provider detail",
        },
    )
    with pytest.raises(SandboxError, match="OAuth failed") as exc:
        api.create_order("purchase-001", "1.00")
    message = str(exc.value)
    sensitive_value_exposed = any(
        value in message
        for value in (
            test_token,
            "test-client-secret-not-real",
            "private provider detail",
        )
    )
    assert sensitive_value_exposed is False
    api_request_sent = any(
        path != "/v1/oauth2/token"
        for _, path, _, _ in fake.calls
    )
    assert api_request_sent is False


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
    api, fake = client((200, APPROVED), (201, CAPTURED), (200, CAPTURE_EVIDENCE))
    result = api.capture_order("ORDER123", "capture-001", "1.00")
    assert result["capture_status"] == "COMPLETED"
    assert fake.calls[2][2]["PayPal-Request-Id"] == request_id(CONFIG.merchant_id, "capture", "capture-001")
    assert fake.calls[2][3] == b"{}"
    assert api.get_capture("CAPTURE123") == {
        "capture_id": "CAPTURE123",
        "status": "COMPLETED",
        "amount": "1.00",
        "currency": "USD",
        "related_order_id": "ORDER123",
    }
    assert fake.calls[-1][1] == "/v2/payments/captures/CAPTURE123"


def test_get_capture_missing_nested_evidence_is_returned_as_missing():
    api, _ = client((200, {"id": "CAPTURE123", "status": "COMPLETED"}))

    assert api.get_capture("CAPTURE123") == {
        "capture_id": "CAPTURE123",
        "status": "COMPLETED",
        "amount": None,
        "currency": None,
        "related_order_id": None,
    }


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
