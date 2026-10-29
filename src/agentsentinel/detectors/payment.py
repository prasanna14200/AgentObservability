"""Narrow, value-free adapter from payment events to existing detector engines."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Literal, Sequence

import pandas as pd
from sklearn.ensemble import IsolationForest

from agentsentinel.data.split import PRUNED_FEATURES
from agentsentinel.detectors.isoforest import predict_isolation_forest, train_isolation_forest
from agentsentinel.detectors.rules import apply_rules
from agentsentinel.payment_policy import AuthenticatedScope, Outcome
from agentsentinel.schema.payment import PaymentIntent
from agentsentinel.schema.payment_telemetry import PaymentTelemetryEvent

logger = logging.getLogger(__name__)
PAYMENT_RULES_VERSION = "payment-rules-v1"
PAYMENT_ISOFOREST_VERSION = "payment-iforest-synthetic-v1"


@dataclass(frozen=True)
class PaymentDetection:
    detector_version: str
    anomaly: bool
    anomaly_score: float
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class PaymentTraceLabel:
    trace_id: str
    label: Literal["normal", "anomalous"]
    scenario: str
    events: tuple[PaymentTelemetryEvent, ...]


class PaymentFeatureAdapter:
    """Derive only operational counts/timing features; payment values are excluded."""

    def trace_features(self, events: Sequence[PaymentTelemetryEvent]) -> dict[str, float]:
        if not events:
            raise ValueError("payment trace must contain at least one event")
        trace_ids = {event.trace_id for event in events}
        if len(trace_ids) != 1 or None in trace_ids:
            raise ValueError("payment events must share one non-null trace_id")
        ordered = sorted(events, key=lambda event: (event.timestamp, event.event_id))
        dispatches = sum(event.event_type == "PAYMENT_DISPATCHED" for event in ordered)
        durations = [event.duration_ms for event in ordered if event.duration_ms is not None]
        gaps = [
            max(0.0, (current.timestamp - previous.timestamp).total_seconds() * 1000)
            for previous, current in zip(ordered, ordered[1:])
        ]
        event_count = len(ordered)
        repeated_dispatches = max(0, dispatches - 1)
        mean_duration = sum(durations) / max(1, len(durations))
        latency_signal = (mean_duration - 20.0) / 10.0
        retry_signal = float(repeated_dispatches)
        values = {
            "task_latency_zscore": latency_signal,
            "task_retry_zscore": retry_signal,
            "task_token_zscore": 0.0,
            "task_cost_zscore": 0.0,
            "agent_task_latency_zscore": latency_signal,
            "agent_task_retry_zscore": retry_signal,
            "agent_task_token_zscore": 0.0,
            "agent_task_cost_zscore": 0.0,
            "call_position": float(event_count),
            "time_since_last_call_ms": sum(gaps) / max(1, len(gaps)),
            "tool_repeat_in_session": float(repeated_dispatches),
            "permission_match": 1.0,
            "trace_event_count": float(event_count),
        }
        return {name: values[name] for name in PRUNED_FEATURES}

    def to_frame(self, traces: Sequence[PaymentTraceLabel]) -> pd.DataFrame:
        rows = []
        for trace in traces:
            row = self.trace_features(trace.events)
            row.update({"trace_id": trace.trace_id, "label": trace.label, "scenario": trace.scenario})
            rows.append(row)
        return pd.DataFrame(rows, columns=[*PRUNED_FEATURES, "trace_id", "label", "scenario"])


class PaymentDetectorAdapter:
    """Runs the repository's rules and Isolation Forest on safe payment features."""

    def __init__(self, feature_adapter: PaymentFeatureAdapter | None = None) -> None:
        self.features = feature_adapter or PaymentFeatureAdapter()

    def rules(self, events: Sequence[PaymentTelemetryEvent]) -> PaymentDetection:
        row = self.features.trace_features(events)
        frame = apply_rules(pd.DataFrame([row]))
        reasons: list[str] = []
        if bool(frame.iloc[0]["rules_loop"]):
            reasons.append("REPEATED_PAYMENT_DISPATCH")
        if bool(frame.iloc[0]["rules_retry_storm"]):
            reasons.append("PAYMENT_RETRY_STORM")
        if bool(frame.iloc[0]["rules_resource_spike"]):
            reasons.append("PAYMENT_RESOURCE_SPIKE")
        anomalous = bool(frame.iloc[0]["rules_anomalous"])
        return PaymentDetection(PAYMENT_RULES_VERSION, anomalous, float(anomalous), tuple(reasons))

    def fit_isolation_forest(self, normal_traces: Sequence[PaymentTraceLabel]) -> IsolationForest:
        if not normal_traces or any(trace.label != "normal" for trace in normal_traces):
            raise ValueError("payment Isolation Forest training accepts only normal synthetic traces")
        frame = self.features.to_frame(normal_traces)
        return train_isolation_forest(frame)

    def isolation_forest(
        self, events: Sequence[PaymentTelemetryEvent], model: IsolationForest
    ) -> PaymentDetection:
        row = self.features.trace_features(events)
        frame = predict_isolation_forest(model, pd.DataFrame([row]))
        is_anomaly = bool(frame.iloc[0]["iso_anomaly"])
        score = float(frame.iloc[0]["iso_anomaly_score"])
        reasons = ("ISOLATION_FOREST_THRESHOLD",) if is_anomaly else ()
        return PaymentDetection(PAYMENT_ISOFOREST_VERSION, is_anomaly, score, reasons)

    @staticmethod
    def advisory_signals(
        detections: Sequence[PaymentDetection], deterministic_outcome: Outcome
    ) -> tuple[Literal["request_review"], ...]:
        if deterministic_outcome == "ALLOW" and any(item.anomaly for item in detections):
            return ("request_review",)
        return ()


class PaymentDetectorAdvisoryProvider:
    """Trusted gateway adapter; detector outages yield no signal, never authorization."""

    def __init__(
        self,
        adapter: PaymentDetectorAdapter,
        event_provider: Callable[[PaymentIntent, AuthenticatedScope], Sequence[PaymentTelemetryEvent]],
        model: IsolationForest | None = None,
    ) -> None:
        self.adapter = adapter
        self.event_provider = event_provider
        self.model = model
        self.diagnostics: list[str] = []

    def signals_for(
        self, intent: PaymentIntent, context: AuthenticatedScope
    ) -> tuple[Literal["request_review"], ...]:
        try:
            events = self.event_provider(intent, context)
            if not events:
                return ()
            detections = [self.adapter.rules(events)]
            if self.model is not None:
                detections.append(self.adapter.isolation_forest(events, self.model))
            return ("request_review",) if any(item.anomaly for item in detections) else ()
        except Exception:
            self.diagnostics.append("PAYMENT_DETECTOR_UNAVAILABLE")
            logger.warning("payment detector unavailable; deterministic policy remains authoritative")
            return ()
