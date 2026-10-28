"""Pre-execution gateway with fake tests and explicit manual Sandbox execution."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Lock
from typing import Literal, Protocol

from pydantic import field_validator

from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy, PolicyDecision, PolicyAudit, POLICY_VERSION
from agentsentinel.schema.payment import PaymentIntent, PaymentModel, _check_id

GatewayStatus = Literal["REJECTED", "REVIEW_REQUIRED", "IN_PROGRESS", "COMPLETED", "UNCERTAIN"]
GatewayReason = Literal[
    "POLICY_BLOCK", "POLICY_REVIEW", "POLICY_FAILURE", "EXECUTED_FAKE", "EXECUTED_SANDBOX",
    "OPERATION_CONFLICT", "OPERATION_RESERVATION_FAILED", "OPERATION_IN_PROGRESS",
    "AUTHORIZATION_CONSUMPTION_FAILED", "EXECUTION_DISABLED", "PROVIDER_OUTCOME_UNCERTAIN",
    "OPERATION_FINALIZATION_FAILED", "RECONCILIATION_REQUIRED", "VERIFIED_COMPLETED", "VERIFIED_NO_PAYMENT",
    "KILL_SWITCH_ACTIVE",
]


class GatewayResult(PaymentModel):
    """Safe response. A missing decision means evaluation did not finish."""

    operation_id: str
    decision: PolicyDecision | None
    status: GatewayStatus
    reason_codes: tuple[GatewayReason, ...]
    provider_order_id: str | None = None
    provider_capture_id: str | None = None

    @field_validator("operation_id")
    @classmethod
    def valid_operation_id(cls, value: str) -> str:
        return _check_id(value, "operation identifier")

    @field_validator("provider_order_id", "provider_capture_id")
    @classmethod
    def valid_provider_id(cls, value: str | None) -> str | None:
        return None if value is None else _check_id(value, "provider identifier")


class AdapterOutcome(PaymentModel):
    """Sanitized adapter output; never a raw provider response."""

    status: Literal["COMPLETED", "ORDER_CREATED", "CAPTURE_COMPLETED"]
    provider_order_id: str | None = None
    provider_capture_id: str | None = None

    @field_validator("provider_order_id", "provider_capture_id")
    @classmethod
    def valid_provider_id(cls, value: str | None) -> str | None:
        return None if value is None else _check_id(value, "provider identifier")


class ProtectedAdapter(Protocol):
    def execute(self, intent: PaymentIntent) -> AdapterOutcome: ...


class TestOnlyAdapter(ABC):
    """Explicit Day 6 fake marker. Python inheritance is not a deployment boundary."""

    @abstractmethod
    def execute(self, intent: PaymentIntent) -> AdapterOutcome:
        ...


class SandboxOnlyAdapter(ABC):
    """Explicit manual Sandbox adapter marker; never grants agent access to credentials."""

    @abstractmethod
    def execute(self, intent: PaymentIntent) -> AdapterOutcome:
        ...


class AuthorizationConsumer(Protocol):
    def claim(self, context: AuthenticatedScope, intent: PaymentIntent, decision: PolicyDecision) -> bool:
        """Atomically recheck authoritative authorization and consume it once."""
        ...


@dataclass(frozen=True)
class Reservation:
    state: Literal["NEW", "RESERVED", "CLAIMED", "COMPLETED", "REJECTED", "REVIEW_REQUIRED", "UNCERTAIN", "CONFLICT", "SAFETY_BLOCK"]
    result: GatewayResult | None = None
    guard_reason: Literal["OPERATION_LOOP_LIMIT", "TRACE_LOOP_LIMIT", "DAILY_BUDGET_LIMIT", "MERCHANT_BUDGET_LIMIT"] | None = None


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
        *, enable_sandbox_execution: bool = False,
        kill_switch: object | None = None,
    ) -> None:
        self._policy = policy
        self._operations = operations
        self._consumer = consumer
        self._adapter = adapter
        self._enable_sandbox_execution = enable_sandbox_execution
        self._kill_switch = kill_switch

    def submit(
        self, intent: PaymentIntent, context: AuthenticatedScope,
        *, advisory_signals: tuple[Literal["request_review"], ...] = (),
    ) -> GatewayResult:
        """Context must originate at an authenticated app boundary, never from the AI."""
        if not isinstance(intent, PaymentIntent) or not isinstance(context, AuthenticatedScope):
            raise TypeError("validated intent and trusted application context are required")
        # model_copy/model_construct can bypass Pydantic validators; revalidate untrusted input.
        intent = PaymentIntent.model_validate(intent.model_dump(mode="python"))
        context = AuthenticatedScope.model_validate(context.model_dump(mode="python"))
        fingerprint = _fingerprint(intent, context)
        try:
            # Durable stores can persist the bounded intent/context fields needed for recovery.
            reserve_intent = getattr(self._operations, "reserve_intent", None)
            reservation = (reserve_intent(intent, context, fingerprint) if reserve_intent is not None
                           else self._operations.reserve(intent.operation_id, fingerprint))
        except Exception:
            return self._result(intent, None, "REJECTED", "OPERATION_RESERVATION_FAILED")

        if reservation.state == "CONFLICT":
            return self._result(intent, None, "REJECTED", "OPERATION_CONFLICT")
        if reservation.state == "SAFETY_BLOCK":
            kind = "LOOP_LIMIT_EXCEEDED" if reservation.guard_reason in {"OPERATION_LOOP_LIMIT", "TRACE_LOOP_LIMIT"} else "BUDGET_EXCEEDED"
            if reservation.guard_reason is None:
                return self._result(intent, None, "REJECTED", "OPERATION_RESERVATION_FAILED")
            # A rejected pre-reservation attempt has no operation row to finalize.
            decision = PolicyDecision(
                decision_id="decision_" + hashlib.sha256(
                    f"{intent.operation_id}:{reservation.guard_reason}".encode("ascii")
                ).hexdigest()[:24], operation_id=intent.operation_id, outcome="BLOCK",
                reason_codes=(kind, reservation.guard_reason), policy_version=POLICY_VERSION,
                evaluated_at=datetime.now(timezone.utc),
                audit_metadata=PolicyAudit(authorization_lookup="skipped"),
            )
            return self._result(intent, decision, "REJECTED", "POLICY_BLOCK")
        if reservation.state == "RESERVED":
            return self._result(intent, None, "IN_PROGRESS", "OPERATION_IN_PROGRESS")
        if reservation.state == "CLAIMED":
            return self._result(intent, None, "UNCERTAIN", "RECONCILIATION_REQUIRED")
        if reservation.state != "NEW":
            if reservation.result is None:
                return self._result(intent, None, "UNCERTAIN", "OPERATION_FINALIZATION_FAILED")
            return reservation.result

        if self._kill_switch is not None:
            try:
                if bool(getattr(self._kill_switch, "is_kill_switch_active", lambda: False)()):
                    decision = PolicyDecision(
                        decision_id="decision_" + hashlib.sha256(
                            f"{intent.operation_id}:kill_switch_active".encode("ascii")
                        ).hexdigest()[:24],
                        operation_id=intent.operation_id,
                        outcome="BLOCK",
                        reason_codes=("KILL_SWITCH_ACTIVE",),
                        policy_version=POLICY_VERSION,
                        evaluated_at=datetime.now(timezone.utc),
                        audit_metadata=PolicyAudit(authorization_lookup="skipped"),
                    )
                    return self._finalize(intent, fingerprint, decision, "REJECTED", "KILL_SWITCH_ACTIVE")
            except Exception:
                decision = PolicyDecision(
                    decision_id="decision_" + hashlib.sha256(
                        f"{intent.operation_id}:kill_switch_read_error".encode("ascii")
                    ).hexdigest()[:24],
                    operation_id=intent.operation_id,
                    outcome="BLOCK",
                    reason_codes=("KILL_SWITCH_ACTIVE",),
                    policy_version=POLICY_VERSION,
                    evaluated_at=datetime.now(timezone.utc),
                    audit_metadata=PolicyAudit(authorization_lookup="skipped"),
                )
                return self._finalize(intent, fingerprint, decision, "REJECTED", "KILL_SWITCH_ACTIVE")

        try:
            decision = self._policy.evaluate(intent, context, advisory_signals=advisory_signals)
        except Exception:
            return self._finalize(intent, fingerprint, None, "REJECTED", "POLICY_FAILURE")
        if decision.outcome == "BLOCK":
            return self._finalize(intent, fingerprint, decision, "REJECTED", "POLICY_BLOCK")
        if decision.outcome == "REVIEW":
            return self._finalize(intent, fingerprint, decision, "REVIEW_REQUIRED", "POLICY_REVIEW")

        # Real Sandbox execution requires an explicit trusted application opt-in.
        sandbox_enabled = self._enable_sandbox_execution and isinstance(self._adapter, SandboxOnlyAdapter)
        if self._adapter is None or not (isinstance(self._adapter, TestOnlyAdapter) or sandbox_enabled):
            return self._finalize(intent, fingerprint, decision, "REJECTED", "EXECUTION_DISABLED")
        try:
            consumed = self._consumer.claim(context, intent, decision)
        except Exception:
            consumed = False
        if consumed is not True:
            return self._finalize(intent, fingerprint, decision, "REJECTED", "AUTHORIZATION_CONSUMPTION_FAILED")

        try:
            outcome = self._adapter.execute(intent)
            if not isinstance(outcome, AdapterOutcome):
                raise ValueError("fake adapter returned invalid outcome")
            if sandbox_enabled:
                expected = "ORDER_CREATED" if intent.action == "create_order" else "CAPTURE_COMPLETED"
                if (outcome.status != expected or outcome.provider_order_id is None
                        or (intent.action == "capture_order" and outcome.provider_capture_id is None)):
                    raise ValueError("Sandbox adapter returned an uncertain outcome")
            elif outcome.status != "COMPLETED":
                raise ValueError("fake adapter returned invalid outcome")
        except Exception:
            # Timeout may occur after a provider accepted a request. Never retry here.
            return self._finalize(intent, fingerprint, decision, "UNCERTAIN", "PROVIDER_OUTCOME_UNCERTAIN")
        reason = "EXECUTED_SANDBOX" if sandbox_enabled else "EXECUTED_FAKE"
        return self._finalize(intent, fingerprint, decision, "COMPLETED", reason, outcome=outcome)

    def _finalize(
        self, intent: PaymentIntent, fingerprint: str, decision: PolicyDecision | None,
        status: GatewayStatus, reason: GatewayReason, *, outcome: AdapterOutcome | None = None,
    ) -> GatewayResult:
        result = self._result(intent, decision, status, reason, outcome=outcome)
        try:
            self._operations.finish(intent.operation_id, fingerprint, result)
        except Exception:
            # A prior side effect may have happened. Keep the reservation non-retryable.
            return self._result(intent, decision, "UNCERTAIN", "OPERATION_FINALIZATION_FAILED")
        return result

    @staticmethod
    def _result(
        intent: PaymentIntent, decision: PolicyDecision | None, status: GatewayStatus, reason: GatewayReason,
        *, outcome: AdapterOutcome | None = None,
    ) -> GatewayResult:
        return GatewayResult(
            operation_id=intent.operation_id, decision=decision, status=status, reason_codes=(reason,),
            provider_order_id=outcome.provider_order_id if outcome else None,
            provider_capture_id=outcome.provider_capture_id if outcome else None,
        )


class AuthenticatedContextProvider(Protocol):
    """Trusted application boundary derives identity from its authenticated session."""

    def current_context(self) -> AuthenticatedScope: ...


class AdvisorySignalProvider(Protocol):
    """Internal-only source; its signals carry no authorization authority."""

    def signals_for(self, intent: PaymentIntent, context: AuthenticatedScope) -> tuple[Literal["request_review"], ...]: ...


class TrustedGatewayEntryPoint:
    """Narrow facade: AI-facing callers can supply only an intent, never scope."""

    def __init__(self, gateway: PaymentGateway, context_provider: AuthenticatedContextProvider,
                 advisory_provider: AdvisorySignalProvider | None = None) -> None:
        self._gateway = gateway
        self._context_provider = context_provider
        self._advisory_provider = advisory_provider

    def submit(self, intent: PaymentIntent) -> GatewayResult:
        # The agent-facing call has no signal argument. Only an injected internal
        # provider may request escalation; policy still applies hard blocks first.
        context = self._context_provider.current_context()
        signals = self._advisory_provider.signals_for(intent, context) if self._advisory_provider else ()
        return self._gateway.submit(intent, context, advisory_signals=signals)
