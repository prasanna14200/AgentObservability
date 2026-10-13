"""Protected, manual-only Sandbox REST adapter and read-only status verifier."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from agentsentinel.payment_gateway import AdapterOutcome, SandboxOnlyAdapter
from agentsentinel.payment_sqlite import ReconciliationSnapshot, SandboxReadOnlyVerifier, VerifiedOutcome
from agentsentinel.paypal_sandbox import PayPalSandboxClient, SandboxError
from agentsentinel.schema.payment import PaymentIntent


class SandboxPaymentAdapter(SandboxOnlyAdapter):
    """Gateway-owned adapter. Never pass the client or its credentials to an agent."""

    def __init__(self, client: PayPalSandboxClient) -> None:
        self._client = client

    def execute(self, intent: PaymentIntent) -> AdapterOutcome:
        if intent.money.currency != "USD" or intent.merchant_id != self._client.config.merchant_id:
            raise SandboxError("Sandbox currency or merchant mismatch")
        amount = f"{intent.money.amount:.2f}"
        if intent.action == "create_order":
            result = self._client.create_order(intent.operation_id, amount, "USD")
            if result["status"] != "CREATED":
                raise SandboxError("Sandbox create outcome uncertain; reconcile before further action")
            return AdapterOutcome(status="ORDER_CREATED", provider_order_id=result["order_id"])
        if intent.action == "capture_order" and intent.target_order_id is not None:
            result = self._client.capture_order(intent.target_order_id, intent.operation_id, amount)
            if result["status"] != "COMPLETED" or result["capture_status"] != "COMPLETED":
                raise SandboxError("Sandbox capture outcome uncertain; reconcile before further action")
            return AdapterOutcome(status="CAPTURE_COMPLETED", provider_order_id=result["order_id"],
                                  provider_capture_id=result["capture_id"])
        raise SandboxError("Unsupported Sandbox payment action")


class SandboxStatusVerifier(SandboxReadOnlyVerifier):
    """Read-only GET verifier for operations with a known order ID."""

    def __init__(self, client: PayPalSandboxClient) -> None:
        self._client = client

    def verify(self, snapshot: ReconciliationSnapshot) -> VerifiedOutcome:
        order_id = snapshot.provider_order_id or snapshot.target_order_id
        common = dict(operation_id=snapshot.operation_id, attempt_id=snapshot.attempt_id,
                      idempotency_key=snapshot.idempotency_key, verified_at=datetime.now(timezone.utc))
        if order_id is None:
            # PayPal Orders GET cannot locate an unknown create-order ID from our local key.
            return VerifiedOutcome(status="STILL_UNCERTAIN", **common)
        order = self._client.get_order(order_id)
        if (order["order_id"] != order_id or order["merchant_id"] != snapshot.merchant_id
                or order["currency"] != snapshot.currency
                or order["amount"] is None or Decimal(order["amount"]) != Decimal(snapshot.amount_text)):
            raise SandboxError("Sandbox order binding mismatch during reconciliation")
        if snapshot.action == "create_order":
            status = "COMPLETED" if order["status"] in {"CREATED", "APPROVED", "COMPLETED"} else "STILL_UNCERTAIN"
            return VerifiedOutcome(status=status, provider_order_id=order_id, **common)
        capture_id = order["capture_id"] or snapshot.provider_capture_id
        if capture_id is None:
            return VerifiedOutcome(status="STILL_UNCERTAIN", provider_order_id=order_id, **common)
        capture = self._client.get_capture(capture_id)
        status = ("COMPLETED" if order["status"] == "COMPLETED" and capture["status"] == "COMPLETED"
                  and capture["capture_id"] == capture_id else "STILL_UNCERTAIN")
        return VerifiedOutcome(status=status, provider_order_id=order_id,
                               provider_capture_id=capture_id, **common)
