"""Isolated, Sandbox-only PayPal REST spike. Never pass its credentials to an agent."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

BASE_URL = "https://api-m.sandbox.paypal.com"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,79}$")
MERCHANT_RE = re.compile(r"^[A-Z0-9]{8,32}$")


class SandboxError(Exception):
    """Safe error message with no PayPal response body or credentials."""


class UncertainOutcome(SandboxError):
    """A POST may have reached PayPal; reconcile before any further action."""


class Transport(Protocol):
    def request(self, method: str, path: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict]: ...


class UrlLibTransport:
    """Minimal stdlib HTTP transport; never exposes raw network exceptions."""

    def request(self, method: str, path: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Invalid API path")
        req = urllib.request.Request(BASE_URL + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                raw = response.read(1_000_001)
                status = response.status
        except urllib.error.HTTPError as exc:
            # Only the status is needed; error bodies may contain sensitive data.
            return exc.code, {}
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise UncertainOutcome("Network outcome uncertain; inspect PayPal order status before retrying") from None
        if len(raw) > 1_000_000:
            raise SandboxError("PayPal response exceeded size limit")
        try:
            parsed = json.loads(raw) if raw else {}
        except (UnicodeDecodeError, ValueError):
            raise SandboxError("PayPal returned invalid JSON") from None
        if not isinstance(parsed, dict):
            raise SandboxError("PayPal returned an invalid response shape")
        return status, parsed


def read_local_env(path: Path) -> dict[str, str]:
    """Read only simple KEY=VALUE lines; never print or evaluate their values."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class SandboxConfig:
    client_id: str
    client_secret: str
    merchant_id: str

    @classmethod
    def from_values(cls, values: dict[str, str]) -> "SandboxConfig":
        if values.get("PAYPAL_ENVIRONMENT") != "sandbox":
            raise SandboxError("PAYPAL_ENVIRONMENT must be sandbox")
        fields = ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_MERCHANT_ID")
        if any(not values.get(field) or values[field].lower().startswith("your_") for field in fields):
            raise SandboxError("Sandbox client ID, secret, and merchant ID are required in ignored .env")
        merchant_id = values["PAYPAL_MERCHANT_ID"]
        if not MERCHANT_RE.fullmatch(merchant_id):
            raise SandboxError("Invalid Sandbox merchant ID")
        return cls(values["PAYPAL_CLIENT_ID"], values["PAYPAL_CLIENT_SECRET"], merchant_id)


def amount_usd(value: str) -> str:
    """Small USD-only spike; rejects floats, extra precision, and large charges."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]{2}", value):
        raise ValueError("Amount must be a USD string with exactly two decimal places")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid amount") from None
    if not Decimal("0.01") <= amount <= Decimal("10.00"):
        raise ValueError("Sandbox spike amount must be between USD 0.01 and 10.00")
    return f"{amount:.2f}"


def request_id(merchant_id: str, kind: str, operation_id: str) -> str:
    if kind not in {"create", "capture"} or not ID_RE.fullmatch(operation_id):
        raise ValueError("Invalid operation identifier")
    digest = hashlib.sha256(f"{merchant_id}:{kind}:{operation_id}".encode()).hexdigest()
    return f"as-{kind}-{digest[:32]}"


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError(f"Invalid {name}")
    return value


def _capture_from_order(data: dict) -> tuple[str | None, str | None]:
    try:
        capture = data["purchase_units"][0]["payments"]["captures"][0]
        return capture.get("id"), capture.get("status")
    except (KeyError, IndexError, TypeError):
        return None, None


class PayPalSandboxClient:
    def __init__(self, config: SandboxConfig, transport: Transport | None = None):
        self.config = config
        self.transport = transport or UrlLibTransport()
        self._token: str | None = None

    def authenticate(self) -> None:
        credentials = base64.b64encode(f"{self.config.client_id}:{self.config.client_secret}".encode()).decode()
        status, data = self.transport.request("POST", "/v1/oauth2/token", {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }, b"grant_type=client_credentials")
        if status != 200 or not isinstance(data.get("access_token"), str) or not data["access_token"]:
            raise SandboxError(f"Sandbox OAuth failed (HTTP {status})")
        self._token = data["access_token"]

    def _call(self, method: str, path: str, body: dict | None = None, key: str | None = None) -> dict:
        if self._token is None:
            self.authenticate()
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if key:
            headers["PayPal-Request-Id"] = key
        payload = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        status, data = self.transport.request(method, path, headers, payload)
        if status < 200 or status >= 300:
            raise SandboxError(f"Sandbox {method} request failed (HTTP {status})")
        return data

    def create_order(self, operation_id: str, amount: str, currency: str = "USD") -> dict:
        if currency != "USD":
            raise ValueError("This spike supports USD only")
        value = amount_usd(amount)
        key = request_id(self.config.merchant_id, "create", operation_id)
        data = self._call("POST", "/v2/checkout/orders", {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": operation_id,
                "amount": {"currency_code": currency, "value": value},
                "payee": {"merchant_id": self.config.merchant_id},
            }],
        }, key)
        order_id = _identifier(data.get("id"), "PayPal order ID")
        return {"order_id": order_id, "status": str(data.get("status", "UNKNOWN")), "request_id": key}

    def get_order(self, order_id: str) -> dict:
        data = self._call("GET", f"/v2/checkout/orders/{_identifier(order_id, 'order ID')}")
        units = data.get("purchase_units") or []
        unit = units[0] if len(units) == 1 and isinstance(units[0], dict) else {}
        amount = unit.get("amount") or {}
        payee = unit.get("payee") or {}
        capture_id, capture_status = _capture_from_order(data)
        return {
            "order_id": _identifier(data.get("id"), "PayPal order ID"),
            "status": str(data.get("status", "UNKNOWN")),
            "amount": amount.get("value"),
            "currency": amount.get("currency_code"),
            "merchant_id": payee.get("merchant_id"),
            "capture_id": capture_id,
            "capture_status": capture_status,
        }

    def approval_link(self, order_id: str) -> str:
        data = self._call("GET", f"/v2/checkout/orders/{_identifier(order_id, 'order ID')}")
        if data.get("status") != "CREATED":
            raise SandboxError("Order is not awaiting buyer approval")
        for link in data.get("links", []):
            if link.get("rel") not in {"approve", "payer-action"}:
                continue
            href = link.get("href", "")
            parsed = urlparse(href)
            if parsed.scheme == "https" and parsed.hostname in {"www.sandbox.paypal.com", "sandbox.paypal.com"}:
                return href
        raise SandboxError("No validated Sandbox buyer approval link found")

    def capture_order(self, order_id: str, operation_id: str, amount: str) -> dict:
        value = amount_usd(amount)
        key = request_id(self.config.merchant_id, "capture", operation_id)
        order = self.get_order(order_id)
        if order["status"] != "APPROVED":
            raise SandboxError("Buyer approval pending or order not capturable")
        if (order["amount"], order["currency"], order["merchant_id"]) != (value, "USD", self.config.merchant_id):
            raise SandboxError("Order amount, currency, or merchant mismatch")
        data = self._call("POST", f"/v2/checkout/orders/{order_id}/capture", {}, key)
        capture_id, capture_status = _capture_from_order(data)
        if not capture_id:
            raise UncertainOutcome("Capture response lacked an ID; reconcile order before further action")
        return {"order_id": order_id, "status": str(data.get("status", "UNKNOWN")),
                "capture_id": _identifier(capture_id, "capture ID"), "capture_status": capture_status,
                "request_id": key}

    def get_capture(self, capture_id: str) -> dict:
        data = self._call("GET", f"/v2/payments/captures/{_identifier(capture_id, 'capture ID')}")
        return {"capture_id": _identifier(data.get("id"), "PayPal capture ID"),
                "status": str(data.get("status", "UNKNOWN"))}
