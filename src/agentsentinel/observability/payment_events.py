"""Safe event construction and failure-visible publication for payment flows."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Protocol

from agentsentinel.observability.collector import PaymentTelemetryCapture
from agentsentinel.schema.payment_telemetry import PaymentEventType, PaymentTelemetryEvent

logger = logging.getLogger(__name__)


class PaymentEventCollector(Protocol):
    def capture_payment_event(
        self, raw_event: PaymentTelemetryEvent | dict[str, object]
    ) -> PaymentTelemetryCapture: ...


def _stable_id(prefix: str, material: str) -> str:
    return prefix + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def build_payment_event(
    *,
    event_type: PaymentEventType,
    trace_id: str | None = None,
    operation_id: str | None = None,
    payment_intent_id: str | None = None,
    approval_id: str | None = None,
    action: str | None = None,
    gateway_decision: str | None = None,
    reason_codes: tuple[str, ...] = (),
    duration_ms: float | None = None,
    provider_outcome_category: str | None = None,
    anomaly_score: float | None = None,
    detector_version: str | None = None,
    actor_role: str | None = None,
    timestamp: datetime | None = None,
) -> PaymentTelemetryEvent:
    occurred_at = timestamp or datetime.now(timezone.utc)
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("payment event timestamp must be timezone aware")
    occurred_at = occurred_at.astimezone(timezone.utc)
    correlation = trace_id or approval_id or "system"
    operation = operation_id or approval_id or "system"
    occurrence = occurred_at.isoformat() if operation_id is None and approval_id is None else ""
    identity = f"{correlation}|{operation}|{approval_id or ''}|{event_type}|{occurrence}"
    span_id = _stable_id("span_", identity)
    parent_event_type = {
        "POLICY_EVALUATED": "PAYMENT_PROPOSED",
        "PAYMENT_DISPATCHED": "POLICY_EVALUATED",
        "PAYMENT_COMPLETED": "PAYMENT_DISPATCHED",
        "PAYMENT_UNCERTAIN": "PAYMENT_DISPATCHED",
        "PAYMENT_BLOCKED": "POLICY_EVALUATED",
        "PAYMENT_REVIEW_REQUIRED": "POLICY_EVALUATED",
        "APPROVAL_REQUESTED": "POLICY_EVALUATED",
        "APPROVAL_APPROVED": "APPROVAL_REQUESTED",
        "APPROVAL_REJECTED": "APPROVAL_REQUESTED",
        "APPROVAL_EXPIRED": "APPROVAL_REQUESTED",
    }.get(event_type)
    parent_approval_id = None if event_type == "APPROVAL_REQUESTED" else approval_id
    parent_span_id = (
        _stable_id(
            "span_",
            f"{correlation}|{operation}|{parent_approval_id or ''}|{parent_event_type}|{occurrence}",
        )
        if parent_event_type
        else None
    )
    event_id = _stable_id("evt_", identity)
    return PaymentTelemetryEvent(
        event_id=event_id,
        timestamp=occurred_at,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        operation_id=operation_id,
        payment_intent_id=payment_intent_id,
        approval_id=approval_id,
        action=action,
        event_type=event_type,
        gateway_decision=gateway_decision,
        reason_codes=reason_codes,
        duration_ms=duration_ms,
        provider_outcome_category=provider_outcome_category,
        anomaly_score=anomaly_score,
        detector_version=detector_version,
        actor_role=actor_role,
    )


class PaymentTelemetryPublisher:
    """Best-effort observer whose storage failures never become safety evidence."""

    def __init__(self, collector: PaymentEventCollector) -> None:
        self.collector = collector
        self.failures: list[tuple[str, str]] = []

    def publish(self, event: PaymentTelemetryEvent) -> bool:
        try:
            result = self.collector.capture_payment_event(event)
            if result.status in {"accepted", "duplicate"}:
                return True
            reason_code = result.reason_code or "PAYMENT_TELEMETRY_REJECTED"
        except Exception:
            reason_code = "PAYMENT_TELEMETRY_COLLECTOR_FAILURE"
        self.failures.append((event.event_id, reason_code))
        logger.error(
            "payment telemetry publish failed event_id=%s reason_code=%s",
            event.event_id,
            reason_code,
        )
        return False
