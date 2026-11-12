"""Offline-only PayPal LIVE client boundary with read-only requests."""

from __future__ import annotations

import base64
import json
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

LIVE_BASE_URL = "https://api-m.paypal.com"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,79}$")
_MAX_RESPONSE_BYTES = 1_000_000
_ORDER_STATUSES = frozenset(
    {"CREATED", "SAVED", "APPROVED", "VOIDED", "COMPLETED", "PAYER_ACTION_REQUIRED"}
)


class LiveProviderError(RuntimeError):
    """Sanitized failure; never includes credentials or provider response data."""


class LiveExecutionDisabled(LiveProviderError):
    """Payment execution is deliberately unavailable in this phase."""


@dataclass(frozen=True)
class LiveConfig:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.client_id, str)
            or not self.client_id.strip()
            or not isinstance(self.client_secret, str)
            or not self.client_secret.strip()
        ):
            raise ValueError("PayPal LIVE client credentials are required for offline client tests")


@dataclass(frozen=True)
class LiveOrderStatus:
    order_id: str
    status: str


OfflineResponse = tuple[int, bytes] | TimeoutError | ConnectionError | OSError


class _OfflineTransport:
    """Replays preloaded responses and cannot perform network requests."""

    def __init__(self, responses: tuple[OfflineResponse, ...]) -> None:
        if type(responses) is not tuple:
            raise TypeError("offline responses must be a tuple")
        for response in responses:
            if type(response) is tuple:
                if (
                    len(response) != 2
                    or type(response[0]) is not int
                    or type(response[1]) is not bytes
                ):
                    raise TypeError("offline response must contain an integer status and bytes")
            elif type(response) not in {TimeoutError, ConnectionError, OSError}:
                raise TypeError("unsupported offline response fixture")
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        self.calls.append((method, url, dict(headers), body))
        if not self._responses:
            raise LiveProviderError("PayPal LIVE offline response unavailable")
        response = self._responses.pop(0)
        if isinstance(response, (TimeoutError, ConnectionError, OSError)):
            raise response
        return response


class PayPalLiveClient:
    """Read-only LIVE client whose response tape cannot perform network requests."""

    def __init__(
        self, config: LiveConfig, responses: tuple[OfflineResponse, ...]
    ) -> None:
        if not isinstance(config, LiveConfig):
            raise TypeError("explicit PayPal LIVE configuration required")
        self._config = config
        self._offline_transport = _OfflineTransport(responses)
        self._token: str | None = None

    @property
    def api_host(self) -> str:
        return LIVE_BASE_URL

    def authenticate(self) -> None:
        self._token = None
        credentials = base64.b64encode(
            f"{self._config.client_id}:{self._config.client_secret}".encode("utf-8")
        ).decode("ascii")
        body = b"grant_type=client_credentials"
        data = self._request(
            "POST",
            "/v1/oauth2/token",
            {
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            body,
        )
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise LiveProviderError("PayPal LIVE OAuth response was invalid")
        self._token = token

    def get_order(self, order_id: str) -> LiveOrderStatus:
        if not isinstance(order_id, str) or not _IDENTIFIER.fullmatch(order_id):
            raise ValueError("Invalid PayPal order identifier")
        if self._token is None:
            self.authenticate()
        if self._token is None:
            raise LiveProviderError("PayPal LIVE authentication unavailable")
        data = self._request(
            "GET",
            f"/v2/checkout/orders/{order_id}",
            {
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            None,
        )
        status = data.get("status")
        if (
            data.get("id") != order_id
            or not isinstance(status, str)
            or status not in _ORDER_STATUSES
        ):
            raise LiveProviderError("PayPal LIVE order response was invalid")
        return LiveOrderStatus(order_id=order_id, status=status)

    def _request(
        self, method: str, path: str, headers: dict[str, str], body: bytes | None
    ) -> dict[str, object]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Invalid PayPal API path")
        url = LIVE_BASE_URL + path
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api-m.paypal.com":
            raise LiveProviderError("PayPal LIVE host validation failed")
        try:
            status, raw = self._offline_transport.request(method, url, headers, body)
        except (TimeoutError, ConnectionError, OSError, socket.timeout):
            raise LiveProviderError("PayPal LIVE request outcome is unavailable") from None
        if type(status) is not int:
            raise LiveProviderError("PayPal LIVE response status was invalid")
        if status < 200 or status >= 300:
            raise LiveProviderError(f"PayPal LIVE {method} request failed (HTTP {status})")
        if not isinstance(raw, bytes) or len(raw) > _MAX_RESPONSE_BYTES:
            raise LiveProviderError("PayPal LIVE response was invalid")
        try:
            data = json.loads(raw) if raw else {}
        except (UnicodeDecodeError, ValueError):
            raise LiveProviderError("PayPal LIVE response was invalid JSON") from None
        if not isinstance(data, dict):
            raise LiveProviderError("PayPal LIVE response was invalid")
        return data
