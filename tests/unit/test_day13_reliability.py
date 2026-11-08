from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event

import pytest
from fastapi.testclient import TestClient

from agentsentinel.approval_service import ApprovalStore, create_app
from agentsentinel.observability.collector import TraceCollector
from agentsentinel.observability.payment_audit import PaymentAuditChain
from agentsentinel.observability.payment_events import (
    PaymentTelemetryPublisher,
    build_payment_event,
)
from agentsentinel.payment_gateway import AdapterOutcome, PaymentGateway, TestOnlyAdapter, _fingerprint
from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy
from agentsentinel.payment_sqlite import SQLitePaymentStore, SafetyLimits, StoreUnavailable
from agentsentinel.schema.payment import Money, PaymentIntent

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
CONTEXT = AuthenticatedScope(principal_id="day13_user", scope_id="day13_scope")
MERCHANT = "DAY13MERCHANT"


def make_intent(
    operation_id: str,
    *,
    action: str = "create_order",
    amount: str = "1.00",
    target_order_id: str | None = None,
) -> PaymentIntent:
    return PaymentIntent(
        intent_id=f"intent_{operation_id}",
        operation_id=operation_id,
        trace_id=f"trace_{operation_id}",
        scope_id=CONTEXT.scope_id,
        money=Money(amount=amount, currency="USD"),
        merchant_id=MERCHANT,
        purpose="Deterministic Day 13 test input",
        action=action,
        target_order_id=target_order_id,
        requested_at=NOW,
    )


class CountingAdapter(TestOnlyAdapter):
    def __init__(self, *, entered: Event | None = None, release: Event | None = None) -> None:
        self.calls = 0
        self.entered = entered
        self.release = release

    def execute(self, intent: PaymentIntent) -> AdapterOutcome:
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None and not self.release.wait(timeout=5):
            raise TimeoutError("test adapter release timed out")
        return AdapterOutcome(status="COMPLETED")


def approved_fixture(
    db,
    intent: PaymentIntent,
    *,
    clock=lambda: NOW,
    limits: SafetyLimits = SafetyLimits(),
):
    approvals = ApprovalStore(db, clock=clock)
    record = approvals.create_approval(intent=intent, context=CONTEXT)
    approvals.approve_approval(record.approval_id, request_fingerprint=record.request_fingerprint)
    payment_store = SQLitePaymentStore(
        db,
        trusted_issuer_id="trusted_approval_service",
        clock=clock,
        limits=limits,
    )
    return approvals, payment_store


def test_sqlite_reservation_failure_blocks_without_execution(tmp_path):
    intent = make_intent("operation_sqlite_failure")
    payment_store = SQLitePaymentStore(
        tmp_path / "payment.sqlite",
        trusted_issuer_id="trusted_approval_service",
        clock=lambda: NOW,
    )

    def unavailable(*args, **kwargs):
        raise OSError("private database path or SQL must not be logged")

    payment_store.reserve_intent = unavailable
    adapter = CountingAdapter()
    gateway = PaymentGateway(
        PaymentPolicy(payment_store, clock=lambda: NOW),
        payment_store,
        payment_store,
        adapter,
    )

    result = gateway.submit(intent, CONTEXT)

    assert result.status == "REJECTED"
    assert result.reason_codes == ("OPERATION_RESERVATION_FAILED",)
    assert result.decision is None
    assert adapter.calls == 0


def test_approval_service_crash_does_not_issue_authorization(tmp_path):
    secret = "day13-approver-test-secret-000000000"
    app = create_app(db_path=tmp_path / "approval.sqlite", approver_secret=secret)
    store = app.state.approval_store
    intent = make_intent("operation_approval_crash")
    record = store.create_approval(intent=intent, context=CONTEXT)
    store.approve_approval = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("sensitive approval backend detail")
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        f"/approvals/{record.approval_id}/approve",
        headers={
            "Authorization": f"Bearer {secret}",
            "X-CSRF-Token": "csrf",
            "Cookie": "csrf_token=csrf",
        },
        json={"request_fingerprint": record.request_fingerprint},
    )

    assert response.status_code == 500
    assert "sensitive approval backend detail" not in response.text
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM authorizations WHERE operation_id=?",
            (intent.operation_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?",
            (record.approval_id,),
        ).fetchone()[0] == "PENDING"


def test_corrupt_audit_store_is_reported_without_payload_disclosure(tmp_path):
    sink = tmp_path / "telemetry"
    sink.mkdir()
    (sink / "payment_telemetry.sqlite").write_bytes(b"not a sqlite database")
    collector = TraceCollector(sink)
    event = build_payment_event(
        event_type="PAYMENT_PROPOSED",
        trace_id="trace_audit_failure",
        operation_id="operation_audit_failure",
        payment_intent_id="intent_audit_failure",
        action="create_order",
        timestamp=NOW,
    )

    capture = collector.capture_payment_event(event)

    assert capture.status == "unavailable"
    assert capture.reason_code == "PAYMENT_TELEMETRY_STORE_UNAVAILABLE"
    diagnostics = (sink / "rejected" / "payment_telemetry.jsonl").read_text()
    assert "not a sqlite database" not in diagnostics
    assert "PAYMENT_TELEMETRY_STORE_UNAVAILABLE" in diagnostics


def test_uncertain_provider_result_is_not_retried_on_duplicate_submit(tmp_path):
    intent = make_intent("operation_uncertain_no_retry")
    approvals, payment_store = approved_fixture(tmp_path / "payment.sqlite", intent)

    class TimeoutAdapter(CountingAdapter):
        def execute(self, payment_intent: PaymentIntent) -> AdapterOutcome:
            self.calls += 1
            raise TimeoutError("provider outcome may be unknown")

    adapter = TimeoutAdapter()
    gateway = PaymentGateway(
        PaymentPolicy(payment_store, clock=lambda: NOW),
        payment_store,
        payment_store,
        adapter,
    )

    first = gateway.submit(intent, CONTEXT)
    duplicate = gateway.submit(intent, CONTEXT)

    assert first.status == "UNCERTAIN"
    assert duplicate == first
    assert adapter.calls == 1
    assert approvals.get_approval(
        ApprovalStore._approval_id_for(intent.operation_id, intent.trace_id, intent.intent_id)
    ).status == "APPROVED"


def test_operation_finalization_failure_stays_non_retryable(tmp_path):
    intent = make_intent("operation_finalize_failure")
    _, payment_store = approved_fixture(tmp_path / "finalize_failure.sqlite", intent)
    adapter = CountingAdapter()

    class FinishFailure:
        def reserve_intent(self, request, context, fingerprint):
            return payment_store.reserve_intent(request, context, fingerprint)

        def finish(self, *args, **kwargs):
            raise OSError("database write failed after fake execution")

    gateway = PaymentGateway(
        PaymentPolicy(payment_store, clock=lambda: NOW),
        FinishFailure(),
        payment_store,
        adapter,
    )

    result = gateway.submit(intent, CONTEXT)
    duplicate = gateway.submit(intent, CONTEXT)

    assert result.status == "UNCERTAIN"
    assert result.reason_codes == ("OPERATION_FINALIZATION_FAILED",)
    assert duplicate.status == "UNCERTAIN"
    assert duplicate.reason_codes == ("RECONCILIATION_REQUIRED",)
    assert adapter.calls == 1
    assert payment_store.audit_operation(intent.operation_id).state == "CLAIMED"


def test_telemetry_rejection_does_not_relax_block_or_execute(tmp_path):
    class RejectingCollector:
        def capture_payment_event(self, raw_event):
            from agentsentinel.observability.collector import PaymentTelemetryCapture

            return PaymentTelemetryCapture("rejected", "INVALID_PAYMENT_EVENT:test")

    payment_store = SQLitePaymentStore(
        tmp_path / "payment.sqlite",
        trusted_issuer_id="trusted_approval_service",
        clock=lambda: NOW,
    )
    publisher = PaymentTelemetryPublisher(RejectingCollector())
    adapter = CountingAdapter()
    gateway = PaymentGateway(
        PaymentPolicy(payment_store, clock=lambda: NOW),
        payment_store,
        payment_store,
        adapter,
        telemetry_publisher=publisher,
    )

    result = gateway.submit(make_intent("operation_telemetry_rejected"), CONTEXT)

    assert result.status == "REJECTED"
    assert result.decision is not None and result.decision.outcome == "BLOCK"
    assert result.decision.reason_codes == ("AUTHORIZATION_MISSING",)
    assert adapter.calls == 0
    assert publisher.failures


def test_concurrent_same_operation_executes_fake_once_and_caches_result(tmp_path):
    intent = make_intent("operation_concurrent_idempotent")
    _, payment_store = approved_fixture(tmp_path / "payment.sqlite", intent)
    entered, release = Event(), Event()
    adapter = CountingAdapter(entered=entered, release=release)
    gateway = PaymentGateway(
        PaymentPolicy(payment_store, clock=lambda: NOW),
        payment_store,
        payment_store,
        adapter,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(gateway.submit, intent, CONTEXT)
        assert entered.wait(timeout=5)
        concurrent = gateway.submit(intent, CONTEXT)
        release.set()
        first = first_future.result(timeout=5)

    replay = gateway.submit(intent, CONTEXT)

    assert first.status == "COMPLETED"
    assert concurrent.status == "UNCERTAIN"
    assert concurrent.reason_codes == ("RECONCILIATION_REQUIRED",)
    assert replay == first
    assert adapter.calls == 1


def test_concurrent_distinct_captures_cannot_exceed_shared_budget(tmp_path):
    db = tmp_path / "capture_budget.sqlite"
    limits = SafetyLimits(
        daily_spend_usd=Decimal("1.00"),
        merchant_daily_spend_usd=Decimal("1.00"),
    )
    intents = [
        make_intent(
            f"operation_capture_budget_{index}",
            action="capture_order",
            target_order_id=f"order_{index}",
        )
        for index in range(2)
    ]
    approvals = ApprovalStore(db, clock=lambda: NOW)
    for intent in intents:
        record = approvals.create_approval(intent=intent, context=CONTEXT)
        approvals.approve_approval(record.approval_id, request_fingerprint=record.request_fingerprint)
    payment_store = SQLitePaymentStore(
        db,
        trusted_issuer_id="trusted_approval_service",
        clock=lambda: NOW,
        limits=limits,
    )
    adapter = CountingAdapter()
    gateway = PaymentGateway(
        PaymentPolicy(payment_store, clock=lambda: NOW),
        payment_store,
        payment_store,
        adapter,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: gateway.submit(item, CONTEXT), intents))

    assert sorted(result.status for result in results) == ["COMPLETED", "REJECTED"]
    blocked = next(result for result in results if result.status == "REJECTED")
    assert blocked.decision is not None and blocked.decision.outcome == "BLOCK"
    assert "BUDGET_EXCEEDED" in blocked.decision.reason_codes
    assert adapter.calls == 1


@pytest.mark.parametrize("terminal_state", ["REJECTED", "EXPIRED"])
def test_rejected_or_expired_approval_never_reaches_gateway(tmp_path, terminal_state):
    now = [NOW]
    db = tmp_path / f"approval_{terminal_state.lower()}.sqlite"
    approvals = ApprovalStore(db, clock=lambda: now[0], ttl_seconds=1)
    intent = make_intent(f"operation_{terminal_state.lower()}")
    record = approvals.create_approval(intent=intent, context=CONTEXT)
    if terminal_state == "REJECTED":
        approvals.reject_approval(record.approval_id)
    else:
        now[0] += timedelta(seconds=2)
        approvals.expire_pending_approvals(now=now[0])
    payment_store = SQLitePaymentStore(
        db,
        trusted_issuer_id="trusted_approval_service",
        clock=lambda: now[0],
    )
    adapter = CountingAdapter()
    gateway = PaymentGateway(
        PaymentPolicy(payment_store, clock=lambda: now[0]),
        payment_store,
        payment_store,
        adapter,
    )

    result = gateway.submit(intent, CONTEXT)

    assert result.status == "REJECTED"
    assert result.decision is not None and result.decision.outcome == "BLOCK"
    assert result.decision.reason_codes == ("AUTHORIZATION_MISSING",)
    assert adapter.calls == 0
    assert approvals.get_approval(record.approval_id).status == terminal_state


def test_kill_switch_activation_during_policy_blocks_before_claim(tmp_path):
    intent = make_intent("operation_switch_before_claim")
    approvals, payment_store = approved_fixture(tmp_path / "switch_before_claim.sqlite", intent)
    evaluated, resume = Event(), Event()
    base_policy = PaymentPolicy(payment_store, clock=lambda: NOW)

    class PausingPolicy:
        def evaluate(self, request, context, *, advisory_signals=()):
            decision = base_policy.evaluate(request, context, advisory_signals=advisory_signals)
            evaluated.set()
            if not resume.wait(timeout=5):
                raise TimeoutError("test policy release timed out")
            return decision

    adapter = CountingAdapter()
    gateway = PaymentGateway(
        PausingPolicy(),
        payment_store,
        payment_store,
        adapter,
        kill_switch=approvals,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(gateway.submit, intent, CONTEXT)
        assert evaluated.wait(timeout=5)
        approvals.set_kill_switch(enabled=True, actor_role="approver")
        resume.set()
        result = future.result(timeout=5)

    assert result.status == "REJECTED"
    assert result.decision is not None and result.decision.outcome == "BLOCK"
    assert result.reason_codes == ("KILL_SWITCH_ACTIVE",)
    assert adapter.calls == 0
    with sqlite3.connect(payment_store.path) as conn:
        assert conn.execute(
            "SELECT status FROM authorizations WHERE operation_id=?",
            (intent.operation_id,),
        ).fetchone()[0] == "issued"


def test_execution_admission_wins_before_switch_and_is_not_cancelled(tmp_path):
    intent = make_intent("operation_admission_before_switch")
    approvals, payment_store = approved_fixture(tmp_path / "admission_before_switch.sqlite", intent)
    entered, release = Event(), Event()
    adapter = CountingAdapter(entered=entered, release=release)
    gateway = PaymentGateway(
        PaymentPolicy(payment_store, clock=lambda: NOW),
        payment_store,
        payment_store,
        adapter,
        kill_switch=approvals,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(gateway.submit, intent, CONTEXT)
        assert entered.wait(timeout=5)
        assert payment_store.audit_operation(intent.operation_id).state == "CLAIMED"
        with sqlite3.connect(payment_store.path) as conn:
            assert conn.execute(
                "SELECT status FROM authorizations WHERE operation_id=?",
                (intent.operation_id,),
            ).fetchone()[0] == "consumed"
        state = approvals.set_kill_switch(enabled=True, actor_role="approver")
        assert state.enabled is True
        release.set()
        result = future.result(timeout=5)

    assert result.status == "COMPLETED"
    assert adapter.calls == 1
    assert payment_store.audit_operation(intent.operation_id).state == "COMPLETED"


def test_kill_switch_claim_write_failure_does_not_consume_or_execute(tmp_path):
    intent = make_intent("operation_switch_claim_write_failure")
    approvals, payment_store = approved_fixture(tmp_path / "switch_claim_write_failure.sqlite", intent)
    with sqlite3.connect(payment_store.path) as conn:
        conn.execute("""
            CREATE TRIGGER reject_authorization_claim
            BEFORE UPDATE OF status ON authorizations
            BEGIN SELECT RAISE(ABORT, 'claim unavailable'); END
        """)
    adapter = CountingAdapter()
    gateway = PaymentGateway(
        PaymentPolicy(payment_store, clock=lambda: NOW),
        payment_store,
        payment_store,
        adapter,
        kill_switch=approvals,
    )

    result = gateway.submit(intent, CONTEXT)

    assert result.status == "REJECTED"
    assert adapter.calls == 0
    with sqlite3.connect(payment_store.path) as conn:
        assert conn.execute(
            "SELECT status FROM authorizations WHERE operation_id=?",
            (intent.operation_id,),
        ).fetchone()[0] == "issued"
        assert conn.execute(
            "SELECT state FROM operations WHERE operation_id=?",
            (intent.operation_id,),
        ).fetchone()[0] == "REJECTED"


@pytest.mark.parametrize("missing", ["table", "row"])
def test_missing_kill_switch_state_fails_closed(tmp_path, missing):
    intent = make_intent(f"operation_missing_switch_{missing}")
    approvals, payment_store = approved_fixture(tmp_path / f"missing_switch_{missing}.sqlite", intent)
    with sqlite3.connect(payment_store.path) as conn:
        if missing == "table":
            conn.execute("DROP TABLE kill_switch")
        else:
            conn.execute("DELETE FROM kill_switch WHERE key='payment_kill_switch'")
    adapter = CountingAdapter()
    gateway = PaymentGateway(
        PaymentPolicy(payment_store, clock=lambda: NOW),
        payment_store,
        payment_store,
        adapter,
        kill_switch=approvals,
    )

    result = gateway.submit(intent, CONTEXT)

    assert result.status == "REJECTED"
    assert result.decision is not None and result.decision.outcome == "BLOCK"
    assert result.reason_codes == ("KILL_SWITCH_ACTIVE",)
    assert adapter.calls == 0
    with sqlite3.connect(payment_store.path) as conn:
        assert conn.execute(
            "SELECT status FROM authorizations WHERE operation_id=?",
            (intent.operation_id,),
        ).fetchone()[0] == "issued"


@pytest.mark.parametrize("missing", ["table", "row"])
def test_atomic_claim_rejects_missing_kill_switch_state(tmp_path, missing):
    intent = make_intent(f"operation_atomic_missing_switch_{missing}")
    approvals, payment_store = approved_fixture(
        tmp_path / f"atomic_missing_switch_{missing}.sqlite", intent
    )
    fingerprint = _fingerprint(intent, CONTEXT)
    assert payment_store.reserve_intent(intent, CONTEXT, fingerprint).state == "NEW"
    decision = PaymentPolicy(payment_store, clock=lambda: NOW).evaluate(intent, CONTEXT)
    assert decision.outcome == "ALLOW"
    with sqlite3.connect(payment_store.path) as conn:
        if missing == "table":
            conn.execute("DROP TABLE kill_switch")
        else:
            conn.execute("DELETE FROM kill_switch WHERE key='payment_kill_switch'")

    with pytest.raises(StoreUnavailable, match="authorization claim unavailable"):
        payment_store.claim(CONTEXT, intent, decision, require_kill_switch=True)

    with sqlite3.connect(payment_store.path) as conn:
        assert conn.execute(
            "SELECT status FROM authorizations WHERE operation_id=?",
            (intent.operation_id,),
        ).fetchone()[0] == "issued"
        assert conn.execute(
            "SELECT state FROM operations WHERE operation_id=?",
            (intent.operation_id,),
        ).fetchone()[0] == "RESERVED"


def test_kill_switch_store_path_mismatch_fails_gateway_configuration(tmp_path):
    intent = make_intent("operation_switch_path_mismatch")
    approvals, payment_store = approved_fixture(tmp_path / "payment.sqlite", intent)
    other_switch = ApprovalStore(tmp_path / "other.sqlite", clock=lambda: NOW)

    with pytest.raises(ValueError, match="same SQLite database"):
        PaymentGateway(
            PaymentPolicy(payment_store, clock=lambda: NOW),
            payment_store,
            payment_store,
            CountingAdapter(),
            kill_switch=other_switch,
        )


def test_malformed_kill_switch_dependency_fails_closed(tmp_path):
    intent = make_intent("operation_bad_switch")
    _, payment_store = approved_fixture(tmp_path / "bad_switch.sqlite", intent)
    adapter = CountingAdapter()
    with pytest.raises(ValueError, match="shared SQLite path"):
        PaymentGateway(
            PaymentPolicy(payment_store, clock=lambda: NOW),
            payment_store,
            payment_store,
            adapter,
            kill_switch=object(),
        )
    assert adapter.calls == 0


def test_audit_corruption_is_detected_before_reporting_success(tmp_path):
    chain = PaymentAuditChain(tmp_path / "audit_corrupt.sqlite")
    chain.append(
        build_payment_event(
            event_type="PAYMENT_PROPOSED",
            trace_id="trace_corrupt",
            operation_id="operation_corrupt",
            payment_intent_id="intent_corrupt",
            action="create_order",
            timestamp=NOW,
        )
    )
    with sqlite3.connect(chain.path) as conn:
        conn.execute(
            "UPDATE payment_audit_chain SET canonical_event=? WHERE sequence_number=1",
            ("{malformed and deliberately non-sensitive}",),
        )

    verification = chain.verify()

    assert not verification.valid
    assert verification.records_checked == 0
    assert verification.first_broken_sequence == 1
    assert verification.reason_code == "INVALID_EVENT"
