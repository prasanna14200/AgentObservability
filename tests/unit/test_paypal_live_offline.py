from __future__ import annotations

import base64
import hmac
import json
from urllib.parse import urlparse

import pytest

from agentsentinel.paypal_live import (
    LIVE_BASE_URL,
    LiveConfig,
    LiveExecutionDisabled,
    LiveProviderError,
    PayPalLiveClient,
)

CLIENT_ID = "TEST_ONLY_LIVE_CLIENT_ID"
CLIENT_SECRET = "TEST_ONLY_LIVE_CLIENT_SECRET_001"
TOKEN = "offline-live-token-never-log"
ORDER_ID = "LIVEORDER123"


def _client(*responses):
    client = PayPalLiveClient(LiveConfig(CLIENT_ID, CLIENT_SECRET), tuple(responses))
    return client, client._offline_transport


def test_client_rejects_caller_supplied_transport():
    class NetworkCapableTransport:
        def request(self, method, url, headers, body):
            raise AssertionError("caller-supplied transport must never be invoked")

    with pytest.raises(TypeError, match="offline responses must be a tuple"):
        PayPalLiveClient(
            LiveConfig(CLIENT_ID, CLIENT_SECRET),
            NetworkCapableTransport(),
        )


def _response(value: object) -> tuple[int, bytes]:
    return 200, json.dumps(value).encode("utf-8")


def _sensitive_values_absent(message: str) -> bool:
    return all(
        value not in message
        for value in (CLIENT_ID, CLIENT_SECRET, TOKEN, "sensitive@example.invalid", "4111111111111111")
    )


def test_live_client_has_fixed_host_and_constructs_oauth_request():
    client, transport = _client(_response({"access_token": TOKEN}))

    client.authenticate()

    assert client.api_host == LIVE_BASE_URL == "https://api-m.paypal.com"
    assert len(transport.calls) == 1
    method, url, headers, body = transport.calls[0]
    assert (method, url, body) == (
        "POST",
        "https://api-m.paypal.com/v1/oauth2/token",
        b"grant_type=client_credentials",
    )
    expected_basic = "Basic " + base64.b64encode(
        f"{CLIENT_ID}:{CLIENT_SECRET}".encode("utf-8")
    ).decode("ascii")
    assert hmac.compare_digest(headers["Authorization"], expected_basic)
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert _sensitive_values_absent(repr(client))


def test_bearer_token_is_used_on_fixed_live_host_read_only_request(caplog):
    client, transport = _client(
        _response({"access_token": TOKEN}),
        (503, b'{"error":"private provider data","access_token":"offline-live-token-never-log"}'),
    )

    with pytest.raises(LiveProviderError) as exc_info:
        client.get_order(ORDER_ID)

    message = str(exc_info.value)
    assert _sensitive_values_absent(message)
    assert len(transport.calls) == 2
    api_method, api_url, api_headers, api_body = transport.calls[1]
    assert (api_method, api_url, api_body) == (
        "GET",
        f"https://api-m.paypal.com/v2/checkout/orders/{ORDER_ID}",
        None,
    )
    assert hmac.compare_digest(api_headers["Authorization"], f"Bearer {TOKEN}")
    assert all(urlparse(call[1]).netloc == "api-m.paypal.com" for call in transport.calls)
    assert all("sandbox" not in call[1] for call in transport.calls)
    assert all(TOKEN not in record.getMessage() for record in caplog.records)


def test_offline_response_tape_exhaustion_fails_without_network_access():
    client, transport = _client()

    with pytest.raises(LiveProviderError, match="offline response unavailable"):
        client.authenticate()

    assert len(transport.calls) == 1


def test_read_only_response_is_allowlisted_and_sensitive_fields_are_omitted():
    client, _ = _client(
        _response({"access_token": TOKEN}),
        _response(
            {
                "id": ORDER_ID,
                "status": "APPROVED",
                "payer": {"email_address": "sensitive@example.invalid"},
                "payment_source": {"card": {"number": "4111111111111111"}},
                "client_secret": CLIENT_SECRET,
            }
        ),
    )

    result = client.get_order(ORDER_ID)

    assert result.order_id == ORDER_ID
    assert result.status == "APPROVED"
    assert _sensitive_values_absent(repr(result))


def test_oauth_failure_prevents_any_api_request_and_sanitizes_body():
    client, transport = _client(
        (
            401,
            json.dumps(
                {
                    "access_token": TOKEN,
                    "client_secret": CLIENT_SECRET,
                    "detail": "sensitive@example.invalid",
                }
            ).encode("utf-8"),
        )
    )

    with pytest.raises(LiveProviderError) as exc_info:
        client.get_order(ORDER_ID)

    assert len(transport.calls) == 1
    assert _sensitive_values_absent(str(exc_info.value))


@pytest.mark.parametrize("failure", [TimeoutError("timeout " + TOKEN), ConnectionError(CLIENT_SECRET)])
def test_transport_failures_are_sanitized(failure):
    client, transport = _client(_response({"access_token": TOKEN}), failure)

    with pytest.raises(LiveProviderError) as exc_info:
        client.get_order(ORDER_ID)

    assert len(transport.calls) == 2
    assert _sensitive_values_absent(str(exc_info.value))


@pytest.mark.parametrize(
    "response_kind",
    ["malformed-json", "non-object-json", "oversized-json"],
)
def test_malformed_or_oversized_api_responses_fail_safely(response_kind: str):
    raw = {
        "malformed-json": b"{ malformed",
        "non-object-json": b"[]",
        "oversized-json": b"x" * 1_000_001,
    }[response_kind]
    client, transport = _client(_response({"access_token": TOKEN}), (200, raw))

    with pytest.raises(LiveProviderError) as exc_info:
        client.get_order(ORDER_ID)

    assert len(transport.calls) == 2
    assert _sensitive_values_absent(str(exc_info.value))


@pytest.mark.parametrize("status", [301, 401, 429, 500])
def test_unexpected_api_status_fails_without_exposing_provider_body(status):
    client, transport = _client(
        _response({"access_token": TOKEN}),
        (
            status,
            json.dumps(
                {
                    "access_token": TOKEN,
                    "client_secret": CLIENT_SECRET,
                    "detail": "sensitive@example.invalid",
                }
            ).encode("utf-8"),
        ),
    )

    with pytest.raises(LiveProviderError) as exc_info:
        client.get_order(ORDER_ID)

    assert len(transport.calls) == 2
    assert f"HTTP {status}" in str(exc_info.value)
    assert _sensitive_values_absent(str(exc_info.value))


def test_invalid_credentials_fail_without_values_in_errors_or_repr():
    with pytest.raises(ValueError) as exc_info:
        LiveConfig(CLIENT_ID, "")
    assert _sensitive_values_absent(str(exc_info.value))
    assert _sensitive_values_absent(repr(LiveConfig(CLIENT_ID, CLIENT_SECRET)))


def test_invalid_order_identifier_never_reaches_transport():
    client, transport = _client()

    with pytest.raises(ValueError):
        client.get_order("../arbitrary-host")

    assert transport.calls == []


def test_client_has_no_payment_write_methods():
    client, transport = _client()

    assert not hasattr(client, "create_order")
    assert not hasattr(client, "capture_order")
    assert not hasattr(client, "refund")
    assert transport.calls == []
