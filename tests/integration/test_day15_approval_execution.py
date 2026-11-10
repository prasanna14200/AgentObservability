"""Offline end-to-end approval and payment execution tests."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentsentinel.approval_service import ApprovalStore, create_app
from agentsentinel.observability.collector import TraceCollector
from agentsentinel.observability.payment_audit import PaymentAuditChain
from agentsentinel.observability.payment_events import PaymentTelemetryPublisher
from agentsentinel.payment_gateway import (
    AdapterOutcome,
    ApprovalRequired,
    PaymentGateway,
    TestOnlyAdapter,
    TrustedGatewayEntryPoint,
)
from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy
from agentsentinel.payment_sqlite import SQLitePaymentStore
from agentsentinel.purchase_gateway_bridge import ApprovedPaymentExecutor
from agentsentinel.schema.payment import Money, PaymentIntent

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
CONTEXT = AuthenticatedScope(principal_id="day15_principal", scope_id="day15_scope")
APPROVER_SECRET = "day15-approver-test-secret-0000000001"


def make_intent(operation_id: str = "day15_operation", **changes: object) -> PaymentIntent:
    fields: dict[str, object] = {
        "intent_id": f"intent_{operation_id}",
        "operation_id": operation_id,
        "trace_id": f"trace_{operation_id}",
        "scope_id": CONTEXT.scope_id,
        "money": Money(amount="6.00", currency="USD"),
        "merchant_id": "DAY15MERCHANT",
        "purpose": "Offline Day 15 approval test",
        "action": "create_order",
        "target_order_id": None,
        "requested_at": NOW,
    }
    fields.update(changes)
    return PaymentIntent.model_validate(fields)


class FixedContext:
    def current_context(self) -> AuthenticatedScope:
        return CONTEXT


class FakeAdapter(TestOnlyAdapter):
    __test__ = False

    def __init__(
        self,
        *,
        error: Exception | None = None,
        outcome: AdapterOutcome | None = None,
    ) -> None:
        self.calls = 0
        self.error = error
        self.outcome = outcome

    def execute(self, intent: PaymentIntent) -> AdapterOutcome:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.outcome or AdapterOutcome(status="COMPLETED")


class Day15System:
    def __init__(
        self,
        root: Path,
        *,
        adapter_error: Exception | None = None,
        adapter_outcome: AdapterOutcome | None = None,
    ) -> None:
        self.database = root / "payments.sqlite"
        self.telemetry_root = root / "collected"
        self.collector = TraceCollector(self.telemetry_root)
        self.publisher = PaymentTelemetryPublisher(self.collector)
        self.approvals = ApprovalStore(
            self.database,
            clock=lambda: NOW,
            trusted_issuer_id="day15_issuer",
            telemetry_publisher=self.publisher,
        )
        self.payments = SQLitePaymentStore(
            self.database,
            trusted_issuer_id="day15_issuer",
            clock=lambda: NOW,
        )
        self.adapter = FakeAdapter(error=adapter_error, outcome=adapter_outcome)
        self.gateway = PaymentGateway(
            PaymentPolicy(self.payments, clock=lambda: NOW),
            self.payments,
            self.payments,
            self.adapter,
            kill_switch=self.approvals,
            telemetry_publisher=self.publisher,
        )
        self.entrypoint = TrustedGatewayEntryPoint(self.gateway, FixedContext())
        self.executor = ApprovedPaymentExecutor(self.entrypoint, self.approvals)

    def request_review(self, intent: PaymentIntent):
        result = self.entrypoint.submit_or_request_approval(intent, self.approvals)
        assert isinstance(result, ApprovalRequired)
        return result

    def approve(self, approval_id: str):
        record = self.approvals.get_approval(approval_id)
        assert record is not None
        return self.approvals.approve_approval(
            approval_id,
            request_fingerprint=record.request_fingerprint,
        )


def _telemetry_events(root: Path) -> list[dict[str, object]]:
    chain = PaymentAuditChain(root / "collected" / "payment_telemetry.sqlite")
    assert chain.verify().valid
    with sqlite3.connect(chain.path) as connection:
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT canonical_event FROM payment_audit_chain ORDER BY sequence_number"
            )
        ]


def _sign_in(client: TestClient) -> None:
    response = client.get("/dashboard/login")
    assert response.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert token is not None
    login = client.post(
        "/dashboard/login",
        data={"csrf_token": token.group(1), "credential": APPROVER_SECRET},
        follow_redirects=False,
    )
    assert login.status_code == 303


def test_review_approval_dispatch_and_dashboard_show_persisted_lifecycle(tmp_path):
    system = Day15System(tmp_path)
    intent = make_intent()
    request = system.request_review(intent)
    assert system.adapter.calls == 0

    pending = system.approvals.get_approval(request.approval_id)
    assert pending is not None and pending.status == "PENDING"
    approved = system.approve(request.approval_id)
    assert approved.status == "APPROVED"
    exact = system.approvals.approved_payment(request.approval_id)
    assert exact == (intent, CONTEXT)

    result = system.executor.execute(request.approval_id)
    assert result.status == "COMPLETED"
    assert result.decision is not None
    assert result.decision.outcome == "ALLOW"
    assert result.decision.reason_codes == ("HUMAN_APPROVAL_ACCEPTED",)
    assert result.decision.audit_metadata.approval_id == request.approval_id
    assert system.adapter.calls == 1
    authorization = system.payments.find(CONTEXT, intent.intent_id, intent.operation_id)
    assert authorization is not None and authorization.status == "consumed"

    events = _telemetry_events(tmp_path)
    event_types = [str(event["event_type"]) for event in events]
    for event_type in (
        "PAYMENT_PROPOSED",
        "POLICY_EVALUATED",
        "PAYMENT_REVIEW_REQUIRED",
        "APPROVAL_REQUESTED",
        "APPROVAL_APPROVED",
        "APPROVAL_GRANTED",
        "PAYMENT_DISPATCHED",
        "PAYMENT_COMPLETED",
    ):
        assert event_type in event_types
    assert event_types.count("PAYMENT_DISPATCHED") == 1
    by_type = {
        event_type: next(event for event in events if event["event_type"] == event_type)
        for event_type in (
            "PAYMENT_PROPOSED",
            "POLICY_EVALUATED",
            "APPROVAL_REQUESTED",
            "APPROVAL_APPROVED",
            "APPROVAL_GRANTED",
            "PAYMENT_DISPATCHED",
            "PAYMENT_COMPLETED",
        )
    }
    assert by_type["POLICY_EVALUATED"]["parent_span_id"] == by_type["PAYMENT_PROPOSED"]["span_id"]
    assert by_type["APPROVAL_REQUESTED"]["parent_span_id"] == by_type["POLICY_EVALUATED"]["span_id"]
    assert by_type["APPROVAL_APPROVED"]["parent_span_id"] == by_type["APPROVAL_REQUESTED"]["span_id"]
    assert by_type["APPROVAL_GRANTED"]["parent_span_id"] == by_type["APPROVAL_REQUESTED"]["span_id"]
    assert by_type["PAYMENT_DISPATCHED"]["parent_span_id"] == by_type["POLICY_EVALUATED"]["span_id"]
    assert by_type["PAYMENT_COMPLETED"]["parent_span_id"] == by_type["PAYMENT_DISPATCHED"]["span_id"]

    app = create_app(
        db_path=system.database,
        telemetry_db_path=system.telemetry_root / "payment_telemetry.sqlite",
        approver_secret=APPROVER_SECRET,
    )
    client = TestClient(app)
    _sign_in(client)
    approval_page = client.get("/dashboard/approvals")
    trace_page = client.get(f"/dashboard/traces/{intent.trace_id}")
    assert approval_page.status_code == trace_page.status_code == 200
    assert request.approval_id in approval_page.text
    for visible in (
        "PAYMENT_PROPOSED",
        "POLICY_EVALUATED",
        "APPROVAL_REQUESTED",
        "APPROVAL_APPROVED",
        "APPROVAL_GRANTED",
        "PAYMENT_DISPATCHED",
        "PAYMENT_COMPLETED",
        request.approval_id,
        intent.intent_id,
        "6.00 USD",
        "payment-policy-v1",
        "COMPLETED",
        "MAY_HAVE_DISPATCHED",
    ):
        assert visible in trace_page.text


@pytest.mark.parametrize(
    "changes",
    [
        {"money": Money(amount="7.00", currency="USD")},
        {"merchant_id": "OTHERDAY15MERCHANT"},
        {"intent_id": "different_day15_intent"},
        {"action": "capture_order", "target_order_id": "target_order_001"},
        {"money": Money(amount="6.00", currency="EUR")},
    ],
)
def test_approval_cannot_authorize_changed_intent_fields(tmp_path, changes):
    system = Day15System(tmp_path)
    original = make_intent()
    request = system.request_review(original)
    system.approve(request.approval_id)
    changed = original.model_copy(update=changes)

    result = system.entrypoint.submit_approved(changed, CONTEXT, request.approval_id)

    assert result.status == "REJECTED"
    assert result.decision is not None and result.decision.outcome == "BLOCK"
    assert system.adapter.calls == 0


def test_hard_block_remains_absolute_after_human_approval(tmp_path):
    system = Day15System(tmp_path)
    original = make_intent()
    request = system.request_review(original)
    system.approve(request.approval_id)
    over_limit = original.model_copy(update={"money": Money(amount="11.00", currency="USD")})

    result = system.entrypoint.submit_approved(over_limit, CONTEXT, request.approval_id)

    assert result.status == "REJECTED"
    assert result.decision is not None and result.decision.outcome == "BLOCK"
    assert "AMOUNT_OUT_OF_RANGE" in result.decision.reason_codes
    assert system.adapter.calls == 0
    event_types = [event["event_type"] for event in _telemetry_events(tmp_path)]
    assert "PAYMENT_BLOCKED" in event_types
    assert "PAYMENT_REJECTED" in event_types


def test_duplicate_approval_and_execution_are_idempotent(tmp_path):
    system = Day15System(tmp_path)
    request = system.request_review(make_intent())
    approved_once = system.approve(request.approval_id)
    approved_twice = system.approve(request.approval_id)
    assert approved_once == approved_twice
    with sqlite3.connect(system.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM authorizations WHERE approval_id=?",
            (request.approval_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_events WHERE approval_id=? AND action='approval_approved'",
            (request.approval_id,),
        ).fetchone()[0] == 1

    first = system.executor.execute(request.approval_id)
    second = system.executor.execute(request.approval_id)
    assert first == second and first.status == "COMPLETED"
    assert system.adapter.calls == 1


def test_expired_review_cannot_issue_authorization(tmp_path):
    system = Day15System(tmp_path)
    intent = make_intent()
    record = system.approvals.create_approval(
        intent=intent,
        context=CONTEXT,
        reason_codes=("REVIEW_AMOUNT_THRESHOLD",),
        expires_at=NOW - timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="approval expired"):
        system.approve(record.approval_id)

    assert system.approvals.get_approval(record.approval_id).status == "EXPIRED"
    with sqlite3.connect(system.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM authorizations WHERE operation_id=?",
            (intent.operation_id,),
        ).fetchone()[0] == 0


def test_kill_switch_enabled_after_approval_blocks_execution(tmp_path):
    system = Day15System(tmp_path)
    request = system.request_review(make_intent())
    system.approve(request.approval_id)
    system.approvals.set_kill_switch(enabled=True)

    result = system.executor.execute(request.approval_id)

    assert result.status == "REJECTED"
    assert result.reason_codes == ("KILL_SWITCH_ACTIVE",)
    assert system.adapter.calls == 0
    authorization = system.payments.find(CONTEXT, "intent_day15_operation", "day15_operation")
    assert authorization is not None and authorization.status == "issued"


def test_kill_switch_serializes_with_dispatch_marker(tmp_path):
    database = tmp_path / "payments.sqlite"
    collector = TraceCollector(tmp_path / "collected")
    publisher = PaymentTelemetryPublisher(collector)
    approvals = ApprovalStore(
        database, clock=lambda: NOW, trusted_issuer_id="day15_issuer", telemetry_publisher=publisher
    )

    class SwitchAfterClaimStore(SQLitePaymentStore):
        def claim(self, context, intent, decision, *, require_kill_switch=False):
            claimed = super().claim(
                context, intent, decision, require_kill_switch=require_kill_switch
            )
            if claimed:
                approvals.set_kill_switch(enabled=True)
            return claimed

    payments = SwitchAfterClaimStore(
        database, trusted_issuer_id="day15_issuer", clock=lambda: NOW
    )
    intent = make_intent()
    request = approvals.create_approval(
        intent=intent,
        context=CONTEXT,
        reason_codes=("REVIEW_AMOUNT_THRESHOLD",),
        defer_telemetry=True,
    )
    approvals.approve_approval(request.approval_id, request_fingerprint=request.request_fingerprint)
    adapter = FakeAdapter()
    gateway = PaymentGateway(
        PaymentPolicy(payments, clock=lambda: NOW),
        payments,
        payments,
        adapter,
        kill_switch=approvals,
    )

    result = gateway.submit(intent, CONTEXT, approval_id=request.approval_id)

    assert result.status == "REJECTED"
    assert result.reason_codes == ("KILL_SWITCH_ACTIVE",)
    assert adapter.calls == 0
    audit = payments.audit_operation(intent.operation_id)
    assert audit is not None and audit.dispatch_state == "NOT_DISPATCHED"


def test_adapter_timeout_is_uncertain_nonretryable_even_after_restart(tmp_path):
    system = Day15System(tmp_path, adapter_error=TimeoutError("sanitized provider timeout"))
    request = system.request_review(make_intent())
    system.approve(request.approval_id)

    first = system.executor.execute(request.approval_id)
    second = system.executor.execute(request.approval_id)
    assert first.status == second.status == "UNCERTAIN"
    assert first.reason_codes == ("PROVIDER_OUTCOME_UNCERTAIN",)
    assert system.adapter.calls == 1
    operation = system.payments.audit_operation(first.operation_id)
    assert operation is not None and operation.dispatch_state == "MAY_HAVE_DISPATCHED"
    app = create_app(
        db_path=system.database,
        telemetry_db_path=system.telemetry_root / "payment_telemetry.sqlite",
        approver_secret=APPROVER_SECRET,
    )
    client = TestClient(app)
    _sign_in(client)
    trace_page = client.get("/dashboard/traces/trace_day15_operation")
    assert trace_page.status_code == 200
    assert "PAYMENT_UNCERTAIN" in trace_page.text
    assert "RECONCILIATION_REQUIRED" in trace_page.text

    restarted = Day15System(tmp_path, adapter_error=TimeoutError("must not be retried"))
    after_restart = restarted.executor.execute(request.approval_id)
    assert after_restart == first
    assert restarted.adapter.calls == 0


def test_confirmed_adapter_failure_is_persisted_without_retry(tmp_path):
    system = Day15System(
        tmp_path,
        adapter_outcome=AdapterOutcome(status="FAILED"),
    )
    request = system.request_review(make_intent())
    system.approve(request.approval_id)

    failed = system.executor.execute(request.approval_id)
    replay = system.executor.execute(request.approval_id)
    events = _telemetry_events(tmp_path)

    assert failed == replay
    assert failed.status == "REJECTED"
    assert failed.reason_codes == ("PROVIDER_CONFIRMED_FAILURE",)
    assert system.adapter.calls == 1
    failed_event = next(event for event in events if event["event_type"] == "PAYMENT_FAILED")
    assert "PROVIDER_CONFIRMED_FAILURE" in failed_event["reason_codes"]
    assert "PAYMENT_UNCERTAIN" not in [event["event_type"] for event in events]
    operation = system.payments.audit_operation(failed.operation_id)
    assert operation is not None
    assert operation.state == "REJECTED"
    assert operation.dispatch_state == "MAY_HAVE_DISPATCHED"

    app = create_app(
        db_path=system.database,
        telemetry_db_path=system.telemetry_root / "payment_telemetry.sqlite",
        approver_secret=APPROVER_SECRET,
    )
    client = TestClient(app)
    _sign_in(client)
    trace_page = client.get("/dashboard/traces/trace_day15_operation")
    assert trace_page.status_code == 200
    assert "PAYMENT_FAILED" in trace_page.text
    assert "FAILED" in trace_page.text
