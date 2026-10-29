"""Versioned, value-minimized telemetry contracts for payment activity."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field, field_validator

from agentsentinel.payment_policy import Outcome
from agentsentinel.schema.payment import PaymentAction, PaymentModel, _check_id

PaymentEventType = Literal[
    "PAYMENT_PROPOSED",
    "POLICY_EVALUATED",
    "APPROVAL_REQUESTED",
    "APPROVAL_APPROVED",
    "APPROVAL_REJECTED",
    "APPROVAL_EXPIRED",
    "PAYMENT_BLOCKED",
    "PAYMENT_REVIEW_REQUIRED",
    "PAYMENT_DISPATCHED",
    "PAYMENT_COMPLETED",
    "PAYMENT_UNCERTAIN",
    "KILL_SWITCH_ENABLED",
    "KILL_SWITCH_DISABLED",
]


class PaymentTelemetryEvent(PaymentModel):
    """Only bounded identifiers, decisions, and operational measurements are retained."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    schema_version: Literal["payment-event-v1"] = "payment-event-v1"
    timestamp: AwareDatetime
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    operation_id: str | None = None
    payment_intent_id: str | None = None
    approval_id: str | None = None
    action: PaymentAction | None = None
    event_type: PaymentEventType
    gateway_decision: Outcome | None = None
    reason_codes: tuple[str, ...] = ()
    duration_ms: float | None = Field(default=None, ge=0)
    provider_outcome_category: Literal[
        "not_started", "created", "captured", "completed", "uncertain", "blocked", "unknown"
    ] | None = None
    anomaly_score: float | None = None
    detector_version: str | None = None
    actor_role: Literal["application", "system", "approver"] | None = None

    @field_validator("timestamp")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @field_validator(
        "event_id", "trace_id", "span_id", "parent_span_id", "operation_id",
        "payment_intent_id", "approval_id",
    )
    @classmethod
    def valid_identifier(cls, value: str | None) -> str | None:
        return None if value is None else _check_id(value, "payment telemetry identifier")

    @field_validator("reason_codes")
    @classmethod
    def safe_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 20 or len(set(value)) != len(value):
            raise ValueError("reason codes must be unique and bounded")
        if any(not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", reason) for reason in value):
            raise ValueError("reason codes must use the safe uppercase vocabulary")
        return value

    @field_validator("duration_ms", "anomaly_score")
    @classmethod
    def finite_measurements(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("telemetry measurements must be finite")
        return value

    @field_validator("detector_version")
    @classmethod
    def bounded_detector_version(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            raise ValueError("invalid detector version")
        return value
