"""Reproducible, separate payment-domain fixtures for offline detector evaluation."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Literal

from agentsentinel.detectors.payment import PaymentTraceLabel
from agentsentinel.schema.payment_telemetry import PaymentTelemetryEvent

PAYMENT_ANOMALY_SCENARIOS = (
    "missing authorization",
    "merchant mismatch",
    "amount mismatch",
    "replayed authorization",
    "excessive tool calls",
    "retry storm",
    "budget exceeded",
    "approval expiry",
    "prompt-injection attempt",
    "kill-switch block",
    "uncertain provider outcome",
)

EVENTS_PER_SCENARIO = 10
DEFAULT_NORMAL_TRACES = 100
DEFAULT_SEED = 20261028
BASE_TIME = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def _event(
    *,
    trace_id: str,
    operation_id: str,
    index: int,
    event_type: str,
    when: datetime,
    duration_ms: float | None = None,
    decision: str | None = None,
    reason: str | None = None,
) -> PaymentTelemetryEvent:
    return PaymentTelemetryEvent(
        event_id=f"evt_{trace_id[-12:]}_{index:02d}",
        timestamp=when,
        trace_id=trace_id,
        span_id=f"span_{trace_id[-12:]}_{index:02d}",
        parent_span_id=f"span_{trace_id[-12:]}_{index - 1:02d}" if index else None,
        operation_id=operation_id,
        payment_intent_id=f"intent_{trace_id[-12:]}",
        action="create_order",
        event_type=event_type,
        gateway_decision=decision,
        reason_codes=(reason,) if reason else (),
        duration_ms=duration_ms,
        provider_outcome_category={
            "PAYMENT_COMPLETED": "created",
            "PAYMENT_UNCERTAIN": "uncertain",
            "PAYMENT_BLOCKED": "blocked",
        }.get(event_type, "not_started"),
        actor_role="application",
    )


def _build_trace(
    *,
    index: int,
    scenario: str,
    label: Literal["normal", "anomalous"],
    rng: random.Random,
) -> PaymentTraceLabel:
    trace_id = f"pay_trace_{index:04d}"
    operation_id = f"pay_op_{index:04d}"
    if label == "normal":
        plan = [
            ("PAYMENT_PROPOSED", None, None, rng.uniform(4.0, 12.0)),
            ("POLICY_EVALUATED", "ALLOW", "ELIGIBLE", rng.uniform(2.0, 8.0)),
            ("PAYMENT_DISPATCHED", "ALLOW", "ELIGIBLE", rng.uniform(4.0, 14.0)),
            ("PAYMENT_COMPLETED", "ALLOW", "ELIGIBLE", rng.uniform(3.0, 10.0)),
        ]
    elif scenario in {"excessive tool calls", "retry storm"}:
        dispatches = 4 if scenario == "excessive tool calls" else 6
        plan = [
            ("PAYMENT_PROPOSED", None, None, rng.uniform(4.0, 12.0)),
            ("POLICY_EVALUATED", "ALLOW", "ELIGIBLE", rng.uniform(2.0, 8.0)),
        ]
        plan.extend(
            ("PAYMENT_DISPATCHED", "ALLOW", "ELIGIBLE", rng.uniform(4.0, 14.0))
            for _ in range(dispatches)
        )
        plan.append(("PAYMENT_UNCERTAIN", "ALLOW", "PROVIDER_OUTCOME_UNCERTAIN", rng.uniform(5.0, 15.0)))
    elif scenario == "approval expiry":
        plan = [
            ("PAYMENT_PROPOSED", None, None, rng.uniform(4.0, 12.0)),
            ("POLICY_EVALUATED", "REVIEW", "REVIEW_AMOUNT_THRESHOLD", rng.uniform(2.0, 8.0)),
            ("APPROVAL_REQUESTED", None, "APPROVAL_CREATED", None),
            ("APPROVAL_EXPIRED", None, "APPROVAL_EXPIRED", None),
        ]
    elif scenario == "uncertain provider outcome":
        plan = [
            ("PAYMENT_PROPOSED", None, None, rng.uniform(4.0, 12.0)),
            ("POLICY_EVALUATED", "ALLOW", "ELIGIBLE", rng.uniform(2.0, 8.0)),
            ("PAYMENT_DISPATCHED", "ALLOW", "ELIGIBLE", rng.uniform(400.0, 900.0)),
            ("PAYMENT_UNCERTAIN", "ALLOW", "PROVIDER_OUTCOME_UNCERTAIN", rng.uniform(800.0, 1600.0)),
        ]
    else:
        reason = {
            "missing authorization": "AUTHORIZATION_MISSING",
            "merchant mismatch": "AUTHORIZATION_MERCHANT_MISMATCH",
            "amount mismatch": "AUTHORIZATION_AMOUNT_MISMATCH",
            "replayed authorization": "AUTHORIZATION_INACTIVE",
            "budget exceeded": "BUDGET_EXCEEDED",
            "prompt-injection attempt": "AUTHORIZATION_MERCHANT_MISMATCH",
            "kill-switch block": "KILL_SWITCH_ACTIVE",
        }[scenario]
        plan = [
            ("PAYMENT_PROPOSED", None, None, rng.uniform(4.0, 12.0)),
            ("POLICY_EVALUATED", "BLOCK", reason, rng.uniform(2.0, 8.0)),
            ("PAYMENT_BLOCKED", "BLOCK", reason, None),
        ]
    trace_time = BASE_TIME + timedelta(seconds=index * 10)
    events = tuple(
        _event(
            trace_id=trace_id,
            operation_id=operation_id,
            index=position,
            event_type=event_type,
            when=trace_time + timedelta(milliseconds=position * 20),
            duration_ms=duration,
            decision=decision,
            reason=reason,
        )
        for position, (event_type, decision, reason, duration) in enumerate(plan)
    )
    return PaymentTraceLabel(trace_id, label, scenario, events)


def generate_payment_traces(
    *,
    normal_count: int = DEFAULT_NORMAL_TRACES,
    per_scenario: int = EVENTS_PER_SCENARIO,
    seed: int = DEFAULT_SEED,
) -> list[PaymentTraceLabel]:
    if normal_count < 2 or per_scenario < 1:
        raise ValueError("synthetic trace counts must be positive and include at least two normal traces")
    rng = random.Random(seed)
    traces = [
        _build_trace(index=index, scenario="normal authorized payment", label="normal", rng=rng)
        for index in range(normal_count)
    ]
    index = normal_count
    for scenario in PAYMENT_ANOMALY_SCENARIOS:
        for _ in range(per_scenario):
            traces.append(_build_trace(index=index, scenario=scenario, label="anomalous", rng=rng))
            index += 1
    return traces
