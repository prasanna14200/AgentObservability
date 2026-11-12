"""Fail-closed LIVE adapter placeholder; it never invokes a transport."""

from __future__ import annotations

from agentsentinel.payment_gateway import AdapterOutcome, LiveOnlyAdapter
from agentsentinel.paypal_live import LiveExecutionDisabled
from agentsentinel.schema.payment import PaymentIntent


class LivePaymentAdapter(LiveOnlyAdapter):
    """Constructible architecture marker whose payment execution always fails closed."""

    def execute(self, intent: PaymentIntent) -> AdapterOutcome:
        raise LiveExecutionDisabled("LIVE payment execution is disabled")
