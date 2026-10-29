from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from agentsentinel.approval_service import ApprovalStore
from agentsentinel.data.payment_synth_traces import generate_payment_traces
from agentsentinel.detectors.payment import (
    PaymentDetectorAdapter,
    PaymentDetection,
    PaymentDetectorAdvisoryProvider,
)
from agentsentinel.observability.collector import TraceCollector
from agentsentinel.observability.payment_audit import PaymentAuditChain
from agentsentinel.observability.payment_events import PaymentTelemetryPublisher, build_payment_event
from agentsentinel.payment_gateway import AdapterOutcome, PaymentGateway, TestOnlyAdapter
from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy
from agentsentinel.payment_sqlite import SQLitePaymentStore
from agentsentinel.schema.payment import Money, PaymentIntent
from agentsentinel.schema.payment_telemetry import PaymentTelemetryEvent

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
CONTEXT = AuthenticatedScope(principal_id="payment_user", scope_id="payment_scope")
MERCHANT = "MERCHANT001"


def make_intent(operation_id: str = "payment_op_001", amount: str = "4.00") -> PaymentIntent:
    return PaymentIntent(
        intent_id="payment_intent_001",
        operation_id=operation_id,
        trace_id="payment_trace_001",
        scope_id=CONTEXT.scope_id,
        money=Money(amount=amount, currency="USD"),
        merchant_id=MERCHANT,
        purpose="Offline telemetry test",
        action="create_order",
        requested_at=NOW,
    )


def make_event(event_id: str = "evt_audit_001", event_type: str = "PAYMENT_PROPOSED"):
    return build_payment_event(
        event_type=event_type,
        trace_id="payment_trace_001",
        operation_id="payment_op_001",
        payment_intent_id="payment_intent_001",
        action="create_order",
        actor_role="application",
        timestamp=NOW,
    ).model_copy(update={"event_id": event_id})


class CountingAdapter(TestOnlyAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, intent: PaymentIntent) -> AdapterOutcome:
        self.calls += 1
        return AdapterOutcome(status="COMPLETED")


class FailingCollector:
    def capture_payment_event(self, raw_event):
        raise OSError("storage details are deliberately not surfaced")


def test_gateway_and_approval_events_share_trace_and_span_correlation(tmp_path):
    collector = TraceCollector(tmp_path / "collected")
    publisher = PaymentTelemetryPublisher(collector)
    db = tmp_path / "shared.sqlite"
    approvals = ApprovalStore(db, clock=lambda: NOW, telemetry_publisher=publisher)
    intent = make_intent()
    request = approvals.create_approval(intent=intent, context=CONTEXT)
    approvals.approve_approval(request.approval_id, request_fingerprint=request.request_fingerprint)

    store = SQLitePaymentStore(db, trusted_issuer_id="trusted_approval_service", clock=lambda: NOW)
    adapter = CountingAdapter()
    gateway = PaymentGateway(
        PaymentPolicy(store, clock=lambda: NOW), store, store, adapter, telemetry_publisher=publisher
    )
    result = gateway.submit(intent, CONTEXT)

    assert result.status == "COMPLETED"
    assert adapter.calls == 1
    events = PaymentAuditChain(tmp_path / "collected" / "payment_telemetry.sqlite")
    with sqlite3.connect(events.path) as conn:
        payloads = [json.loads(row[0]) for row in conn.execute(
            "SELECT canonical_event FROM payment_events ORDER BY rowid"
        )]
    assert [item["event_type"] for item in payloads] == [
        "APPROVAL_REQUESTED",
        "APPROVAL_APPROVED",
        "PAYMENT_PROPOSED",
        "POLICY_EVALUATED",
        "PAYMENT_DISPATCHED",
        "PAYMENT_COMPLETED",
    ]
    assert {item["trace_id"] for item in payloads} == {intent.trace_id}
    assert {item["operation_id"] for item in payloads} == {intent.operation_id}
    assert payloads[0]["approval_id"] == request.approval_id
    by_type = {item["event_type"]: item for item in payloads}
    assert by_type["POLICY_EVALUATED"]["parent_span_id"] == by_type["PAYMENT_PROPOSED"]["span_id"]
    assert by_type["PAYMENT_DISPATCHED"]["parent_span_id"] == by_type["POLICY_EVALUATED"]["span_id"]
    assert by_type["PAYMENT_COMPLETED"]["parent_span_id"] == by_type["PAYMENT_DISPATCHED"]["span_id"]


def test_payment_event_schema_and_collector_reject_sensitive_fields(tmp_path):
    collector = TraceCollector(tmp_path / "collected")
    raw = make_event().model_dump(mode="json")
    raw["approver_secret"] = "RAW_APPROVER_SECRET"
    result = collector.capture_payment_event(raw)
    assert result.status == "rejected"
    assert collector.payment_diagnostics[-1].reason_code.startswith("INVALID_PAYMENT_EVENT")
    assert "RAW_APPROVER_SECRET" not in repr(collector.payment_diagnostics)
    rejection = (tmp_path / "collected" / "rejected" / "payment_telemetry.jsonl").read_text()
    assert "RAW_APPROVER_SECRET" not in rejection
    assert "INVALID_PAYMENT_EVENT" in rejection

    invalid = make_event().model_dump(mode="json")
    invalid["reason_codes"] = ["merchant name includes private data"]
    with pytest.raises(ValueError):
        PaymentTelemetryEvent.model_validate(invalid)


def test_duplicate_telemetry_event_is_idempotent(tmp_path):
    chain = PaymentAuditChain(tmp_path / "telemetry.sqlite")
    first = make_event()
    later = first.model_copy(update={"timestamp": datetime(2026, 10, 28, 12, 1, tzinfo=timezone.utc)})
    assert chain.append(first) == "appended"
    assert chain.append(later) == "duplicate"
    assert chain.record_count() == 1
    assert chain.verify().valid


def test_collector_failure_does_not_authorize_or_execute(tmp_path):
    publisher = PaymentTelemetryPublisher(FailingCollector())
    store = SQLitePaymentStore(tmp_path / "payment.sqlite", trusted_issuer_id="test_issuer")
    adapter = CountingAdapter()
    gateway = PaymentGateway(PaymentPolicy(store), store, store, adapter, telemetry_publisher=publisher)

    result = gateway.submit(make_intent(), CONTEXT)

    assert result.status == "REJECTED"
    assert result.decision is not None and result.decision.outcome == "BLOCK"
    assert adapter.calls == 0
    assert len(publisher.failures) >= 3
    assert all(reason == "PAYMENT_TELEMETRY_COLLECTOR_FAILURE" for _, reason in publisher.failures)


def test_payment_synthetic_generation_is_deterministic_and_separate():
    first = generate_payment_traces(normal_count=3, per_scenario=1)
    second = generate_payment_traces(normal_count=3, per_scenario=1)
    assert first == second
    assert first[0].scenario == "normal authorized payment"
    assert first[0].label == "normal"
    assert len([trace for trace in first if trace.label == "anomalous"]) == 11


def test_payment_rules_and_isolation_forest_emit_versioned_advisory_results():
    traces = generate_payment_traces(normal_count=70, per_scenario=2)
    adapter = PaymentDetectorAdapter()
    retry_trace = next(trace for trace in traces if trace.scenario == "retry storm")
    rules = adapter.rules(retry_trace.events)
    model = adapter.fit_isolation_forest([trace for trace in traces[:70]])
    forest = adapter.isolation_forest(retry_trace.events, model)

    assert rules.detector_version == "payment-rules-v1"
    assert rules.anomaly is True
    assert "PAYMENT_RETRY_STORM" in rules.reason_codes
    assert forest.detector_version == "payment-iforest-synthetic-v1"
    assert isinstance(forest.anomaly_score, float)
    assert PaymentDetectorAdapter.advisory_signals((rules, forest), "ALLOW") == ("request_review",)


def test_trusted_detector_provider_uses_only_events_and_fails_open_to_policy():
    traces = generate_payment_traces(normal_count=70, per_scenario=1)
    retry_events = next(trace for trace in traces if trace.scenario == "retry storm").events
    adapter = PaymentDetectorAdapter()
    provider = PaymentDetectorAdvisoryProvider(adapter, lambda intent, context: retry_events)
    assert provider.signals_for(make_intent(), CONTEXT) == ("request_review",)

    unavailable = PaymentDetectorAdvisoryProvider(
        adapter, lambda intent, context: (_ for _ in ()).throw(RuntimeError("unavailable"))
    )
    assert unavailable.signals_for(make_intent(), CONTEXT) == ()
    assert unavailable.diagnostics == ["PAYMENT_DETECTOR_UNAVAILABLE"]


def test_detector_advisory_never_overrides_block_and_escalates_allowed_review(tmp_path):
    traces = generate_payment_traces(normal_count=70, per_scenario=1)
    adapter = PaymentDetectorAdapter()
    anomaly = adapter.rules(next(trace for trace in traces if trace.scenario == "retry storm").events)
    assert PaymentDetectorAdapter.advisory_signals((anomaly,), "BLOCK") == ()

    db = tmp_path / "approval.sqlite"
    approvals = ApprovalStore(db, clock=lambda: NOW)
    intent = make_intent()
    approval = approvals.create_approval(intent=intent, context=CONTEXT)
    approvals.approve_approval(approval.approval_id, request_fingerprint=approval.request_fingerprint)
    store = SQLitePaymentStore(db, trusted_issuer_id="trusted_approval_service", clock=lambda: NOW)
    policy = PaymentPolicy(store, clock=lambda: NOW)

    assert policy.evaluate(intent, CONTEXT).outcome == "ALLOW"
    advisory = PaymentDetectorAdapter.advisory_signals((anomaly,), "ALLOW")
    escalated = policy.evaluate(intent, CONTEXT, advisory_signals=advisory)
    assert escalated.outcome == "REVIEW"
    assert escalated.reason_codes == ("REVIEW_ADVISORY_SIGNAL",)
    blocked = make_intent(operation_id="payment_op_blocked", amount="11.00")
    assert policy.evaluate(blocked, CONTEXT, advisory_signals=("request_review",)).outcome == "BLOCK"


def test_audit_chain_success_edit_and_reordering_detection(tmp_path):
    db = tmp_path / "audit.sqlite"
    chain = PaymentAuditChain(db)
    assert chain.append(make_event("evt_audit_001")) == "appended"
    assert chain.append(make_event("evt_audit_002", "POLICY_EVALUATED")) == "appended"
    assert chain.verify().valid

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE payment_audit_chain SET event_type='PAYMENT_BLOCKED' WHERE sequence_number=1"
        )
    edited = chain.verify()
    assert not edited.valid and edited.first_broken_sequence == 1

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE payment_audit_chain SET event_type='PAYMENT_PROPOSED' WHERE sequence_number=1"
        )
        conn.execute("UPDATE payment_audit_chain SET sequence_number=10 WHERE sequence_number=1")
        conn.execute("UPDATE payment_audit_chain SET sequence_number=1 WHERE sequence_number=2")
        conn.execute("UPDATE payment_audit_chain SET sequence_number=2 WHERE sequence_number=10")
    reordered = chain.verify()
    assert not reordered.valid
    assert reordered.first_broken_sequence == 1


def test_concurrent_audit_append_has_unique_ordered_chain(tmp_path):
    chain = PaymentAuditChain(tmp_path / "concurrent.sqlite")
    events = [make_event(f"evt_concurrent_{index:03d}") for index in range(20)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(chain.append, events))
    assert results == ["appended"] * 20
    assert chain.record_count() == 20
    assert chain.verify().valid


def test_legacy_audit_records_are_preserved_without_backfill(tmp_path):
    db = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE operation_events (event_id INTEGER, event_type TEXT)")
        conn.execute("CREATE TABLE approval_events (event_id INTEGER, action TEXT)")
        conn.execute("INSERT INTO operation_events VALUES (7, 'finish')")
        conn.execute("INSERT INTO approval_events VALUES (8, 'approval_approved')")

    chain = PaymentAuditChain(db)
    assert chain.verify().valid
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT event_id,event_type FROM operation_events").fetchone() == (7, "finish")
        assert conn.execute("SELECT event_id,action FROM approval_events").fetchone() == (8, "approval_approved")
    assert chain.record_count() == 0
