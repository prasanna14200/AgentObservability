"""Day 6 gateway prototype: fake-adapter execution only, no live payments."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Lock
from typing import Literal, Protocol

from pydantic import field_validator

from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy, PolicyDecision
from agentsentinel.schema.payment import PaymentIntent, PaymentModel, _check_id

GatewayStatus = Literal["REJECTED", "REVIEW_REQUIRED", "IN_PROGRESS", "COMPLETED", "UNCERTAIN"]
GatewayReason = Literal[
    "POLICY_BLOCK", "POLICY_REVIEW", "POLICY_FAILURE", "EXECUTED_FAKE",
    "OPERATION_CONFLICT", "OPERATION_RESERVATION_FAILED", "OPERATION_IN_PROGRESS",
    "AUTHORIZATION_CONSUMPTION_FAILED", "EXECUTION_DISABLED", "PROVIDER_OUTCOME_UNCERTAIN",
    "OPERATION_FINALIZATION_FAILED",
]


class GatewayResult(PaymentModel):
    """Safe response. A missing decision means evaluation did not finish."""

    operation_id: str
    decision: PolicyDecision | None
    status: GatewayStatus
    reason_codes: tuple[GatewayReason, ...]

    @field_validator("operation_id")
    @classmethod
    def valid_operation_id(cls, value: str) -> str:
        return _check_id(value, "operation identifier")


class AdapterOutcome(PaymentModel):
    """Fake adapter output contains no raw provider response or payment instrument."""

    status: Literal["COMPLETED"]


class ProtectedAdapter(Protocol):
    def execute(self, intent: PaymentIntent) -> AdapterOutcome: ...


class TestOnlyAdapter(ABC):
    """Explicit Day 6 fake marker. Python inheritance is not a deployment boundary."""

    @abstractmethod
    def execute(self, intent: PaymentIntent) -> AdapterOutcome:
        ...


class AuthorizationConsumer(Protocol):
    def claim(self, context: AuthenticatedScope, intent: PaymentIntent, decision: PolicyDecision) -> bool:
        """Atomically recheck authoritative authorization and consume it once."""
        ...


@dataclass(frozen=True)
class Reservation:
    state: Literal["NEW", "RESERVED", "COMPLETED", "REJECTED", "REVIEW_REQUIRED", "UNCERTAIN", "CONFLICT"]
    result: GatewayResult | None = None


class OperationStore(Protocol):
    def reserve(self, operation_id: str, fingerprint: str) -> Reservation:
        """Atomically insert RESERVED if absent; otherwise return existing state/result."""
        ...

    def finish(self, operation_id: str, fingerprint: str, result: GatewayResult) -> None:
        """Atomically transition only a matching RESERVED operation to a terminal state."""
        ...


class InMemoryOperationStore:
    """Single-process test prototype; not durable, distributed, or crash-safe."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._records: dict[str, tuple[str, Reservation]] = {}

    def reserve(self, operation_id: str, fingerprint: str) -> Reservation:
        with self._lock:
            existing = self._records.get(operation_id)
            if existing is None:
                reservation = Reservation("NEW")
                self._records[operation_id] = (fingerprint, Reservation("RESERVED"))
                return reservation
            previous_fingerprint, reservation = existing
            return Reservation("CONFLICT") if previous_fingerprint != fingerprint else reservation

    def finish(self, operation_id: str, fingerprint: str, result: GatewayResult) -> None:
        with self._lock:
            existing = self._records.get(operation_id)
            if existing is None or existing[0] != fingerprint or existing[1].state != "RESERVED":
                raise RuntimeError("operation is not reserved")
            self._records[operation_id] = (fingerprint, Reservation(result.status, result))


def _fingerprint(intent: PaymentIntent, context: AuthenticatedScope) -> str:
    # Digest comparison prevents returning another request's existing result.
    material = intent.model_dump_json() + "|" + context.model_dump_json()
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class PaymentGateway:
    """Trusted application component. Never expose this object or dependencies to an agent."""

    def __init__(
        self,
        policy: PaymentPolicy,
        operations: OperationStore,
        consumer: AuthorizationConsumer,
        adapter: ProtectedAdapter | None = None,
    ) -> None:
        self._policy = policy
        self._operations = operations
        self._consumer = consumer
        self._adapter = adapter

    def submit(self, intent: PaymentIntent, context: AuthenticatedScope) -> GatewayResult:
        """Context must originate at an authenticated app boundary, never from the AI."""
        if not isinstance(intent, PaymentIntent) or not isinstance(context, AuthenticatedScope):
            raise TypeError("validated intent and trusted application context are required")
        # model_copy/model_construct can bypass Pydantic validators; revalidate untrusted input.
        intent = PaymentIntent.model_validate(intent.model_dump(mode="python"))
        context = AuthenticatedScope.model_validate(context.model_dump(mode="python"))
        fingerprint = _fingerprint(intent, context)
        try:
            reservation = self._operations.reserve(intent.operation_id, fingerprint)
        except Exception:
            return self._result(intent, None, "REJECTED", "OPERATION_RESERVATION_FAILED")

        if reservation.state == "CONFLICT":
            return self._result(intent, None, "REJECTED", "OPERATION_CONFLICT")
        if reservation.state == "RESERVED":
            return self._result(intent, None, "IN_PROGRESS", "OPERATION_IN_PROGRESS")
        if reservation.state != "NEW":
            if reservation.result is None:
                return self._result(intent, None, "UNCERTAIN", "OPERATION_FINALIZATION_FAILED")
            return reservation.result

        try:
            decision = self._policy.evaluate(intent, context)
        except Exception:
            return self._finalize(intent, fingerprint, None, "REJECTED", "POLICY_FAILURE")
        if decision.outcome == "BLOCK":
            return self._finalize(intent, fingerprint, decision, "REJECTED", "POLICY_BLOCK")
        if decision.outcome == "REVIEW":
            return self._finalize(intent, fingerprint, decision, "REVIEW_REQUIRED", "POLICY_REVIEW")

        # Day 6 cannot connect a real adapter: only an explicitly marked fake may execute.
        if self._adapter is None or not isinstance(self._adapter, TestOnlyAdapter):
            return self._finalize(intent, fingerprint, decision, "REJECTED", "EXECUTION_DISABLED")
        try:
            consumed = self._consumer.claim(context, intent, decision)
        except Exception:
            consumed = False
        if consumed is not True:
            return self._finalize(intent, fingerprint, decision, "REJECTED", "AUTHORIZATION_CONSUMPTION_FAILED")

        try:
            outcome = self._adapter.execute(intent)
            if not isinstance(outcome, AdapterOutcome) or outcome.status != "COMPLETED":
                raise ValueError("fake adapter returned invalid outcome")
        except Exception:
            # Timeout may occur after a provider accepted a request. Never retry here.
            return self._finalize(intent, fingerprint, decision, "UNCERTAIN", "PROVIDER_OUTCOME_UNCERTAIN")
        return self._finalize(intent, fingerprint, decision, "COMPLETED", "EXECUTED_FAKE")

    def _finalize(
        self, intent: PaymentIntent, fingerprint: str, decision: PolicyDecision | None,
        status: GatewayStatus, reason: GatewayReason,
    ) -> GatewayResult:
        result = self._result(intent, decision, status, reason)
        try:
            self._operations.finish(intent.operation_id, fingerprint, result)
        except Exception:
            # A prior side effect may have happened. Keep the reservation non-retryable.
            return self._result(intent, decision, "UNCERTAIN", "OPERATION_FINALIZATION_FAILED")
        return result

    @staticmethod
    def _result(
        intent: PaymentIntent, decision: PolicyDecision | None, status: GatewayStatus, reason: GatewayReason
    ) -> GatewayResult:
        return GatewayResult(operation_id=intent.operation_id, decision=decision, status=status, reason_codes=(reason,))
