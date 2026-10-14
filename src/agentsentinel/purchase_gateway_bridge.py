"""Trusted application boundary from proposals to the existing payment gateway."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from agentsentinel.payment_gateway import TrustedGatewayEntryPoint
from agentsentinel.purchase_agent.contracts import Proposal, ToolResult
from agentsentinel.schema.payment import Money, PaymentIntent


def operation_id_for(run_id: str, action: str) -> str:
    """Trusted application correlation; never accepted from an agent proposal."""
    digest = hashlib.sha256(f"{run_id}:{action}".encode("utf-8")).hexdigest()[:24]
    return "agent_op_" + digest


class GatewayPurchaseClient:
    """Expose two proposal methods; omit authorization and provider output entirely."""

    def __init__(self, entrypoint: TrustedGatewayEntryPoint, *, scope_id: str,
                 run_id: str) -> None:
        self._entrypoint = entrypoint
        self._scope_id = scope_id
        self._run_id = run_id
        self._requested_at = datetime.now(timezone.utc)

    def _submit(self, proposal: Proposal, action: str) -> ToolResult:
        if proposal.action != action:
            raise ValueError("proposal action mismatch")
        operation_id = operation_id_for(self._run_id, action)
        trace_id = "agent_trace_" + hashlib.sha256(self._run_id.encode("utf-8")).hexdigest()[:24]
        intent = PaymentIntent(
            intent_id="intent_" + hashlib.sha256(operation_id.encode("ascii")).hexdigest()[:24],
            operation_id=operation_id, trace_id=trace_id, scope_id=self._scope_id,
            money=Money(amount=proposal.amount_usd, currency="USD"),
            merchant_id=proposal.merchant_id, purpose="Offline purchase agent proposal",
            action=action, target_order_id=proposal.target_order_id,
            requested_at=self._requested_at,
        )
        result = self._entrypoint.submit(intent)
        decision = result.decision.outcome if result.decision else "NO_DECISION"
        reason = (result.decision.reason_codes[0] if result.decision
                  else result.reason_codes[0])
        return ToolResult(decision=decision, status=result.status, reason_code=reason)

    def propose_create_order(self, proposal: Proposal) -> ToolResult:
        return self._submit(proposal, "create_order")

    def propose_capture_order(self, proposal: Proposal) -> ToolResult:
        return self._submit(proposal, "capture_order")
