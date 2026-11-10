"""Trusted application boundary from proposals to the existing payment gateway."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from agentsentinel.approval_service import ApprovalStore
from agentsentinel.payment_gateway import ApprovalRequired, GatewayResult, TrustedGatewayEntryPoint
from agentsentinel.purchase_agent.contracts import Proposal, ToolResult
from agentsentinel.schema.payment import Money, PaymentIntent


def operation_id_for(run_id: str, action: str) -> str:
    """Trusted application correlation; never accepted from an agent proposal."""
    digest = hashlib.sha256(f"{run_id}:{action}".encode("utf-8")).hexdigest()[:24]
    return "agent_op_" + digest


class GatewayPurchaseClient:
    """Expose two proposal methods; omit authorization and provider output entirely."""

    def __init__(self, entrypoint: TrustedGatewayEntryPoint, *, scope_id: str,
                 run_id: str, approval_store: ApprovalStore | None = None) -> None:
        self._entrypoint = entrypoint
        self._scope_id = scope_id
        self._run_id = run_id
        self._requested_at = datetime.now(timezone.utc)
        self._approval_store = approval_store

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
        if self._approval_store is None:
            result = self._entrypoint.submit(intent)
        else:
            submitted = self._entrypoint.submit_or_request_approval(intent, self._approval_store)
            if isinstance(submitted, ApprovalRequired):
                return ToolResult(
                    decision="REVIEW",
                    status="REVIEW_REQUIRED",
                    reason_code=submitted.decision.reason_codes[0],
                    approval_id=submitted.approval_id,
                )
            result = submitted
        decision = result.decision.outcome if result.decision else "NO_DECISION"
        reason = (result.decision.reason_codes[0] if result.decision
                  else result.reason_codes[0])
        return ToolResult(decision=decision, status=result.status, reason_code=reason)

    def propose_create_order(self, proposal: Proposal) -> ToolResult:
        return self._submit(proposal, "create_order")

    def propose_capture_order(self, proposal: Proposal) -> ToolResult:
        return self._submit(proposal, "capture_order")


class ApprovedPaymentExecutor:
    """Trusted continuation for an exact, durably approved request; never agent-facing."""

    def __init__(self, entrypoint: TrustedGatewayEntryPoint, approval_store: ApprovalStore) -> None:
        self._entrypoint = entrypoint
        self._approval_store = approval_store

    def execute(self, approval_id: str) -> GatewayResult:
        approved = self._approval_store.approved_payment(approval_id)
        if approved is None:
            raise ValueError("approved payment request is unavailable")
        intent, context = approved
        return self._entrypoint.submit_approved(intent, context, approval_id)
