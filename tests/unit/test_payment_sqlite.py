"""SQLite durability and failure-window tests. All authorization insertion is test-only."""

import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

import pytest

from agentsentinel.payment_gateway import (
    AdapterOutcome, PaymentGateway, TestOnlyAdapter, TrustedGatewayEntryPoint, _fingerprint,
)
from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy
from agentsentinel.payment_sqlite import (
    SQLitePaymentStore, SQLiteReconciler, StoreUnavailable, TestOnlyOutcomeVerifier, VerifiedOutcome,
)
from agentsentinel.schema.payment import Money, PaymentAuthorization, PaymentIntent

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
CONTEXT = AuthenticatedScope(principal_id="principal_001", scope_id="scope_001")


def intent(*, amount="3.00", currency="USD", action="create_order", target=None, **changes):
    fields = dict(intent_id="intent_001", operation_id="operation_001", trace_id="trace_001",
                  scope_id="scope_001", money=Money(amount=amount, currency=currency),
                  merchant_id="merchant_001", purpose="Notebook", action=action,
                  target_order_id=target, requested_at=NOW)
    fields.update(changes)
    return PaymentIntent(**fields)


def authorization(request=None, **changes):
    request = request or intent()
    fields = dict(authorization_id="authorization_001", issuer_id="issuer_001",
                  scope_id=request.scope_id, intent_id=request.intent_id,
                  operation_id=request.operation_id, merchant_id=request.merchant_id,
                  money=request.money, action=request.action, target_order_id=request.target_order_id,
                  issued_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=5),
                  status="issued", record_origin="application_issued")
    fields.update(changes)
    return PaymentAuthorization(**fields)


def seed_test_authorization(db: Path, record: PaymentAuthorization, principal="principal_001"):
    """Direct SQL fixture only; intentionally absent from the production store API."""
    with sqlite3.connect(db) as conn:
        conn.execute("""INSERT INTO authorizations
            (authorization_id,issuer_id,principal_id,scope_id,intent_id,operation_id,merchant_id,
             amount_text,currency,action,target_order_id,issued_at,expires_at,status,closed_at,record_origin)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record.authorization_id, record.issuer_id, principal, record.scope_id,
             record.intent_id, record.operation_id, record.merchant_id, str(record.money.amount),
             record.money.currency, record.action, record.target_order_id,
             record.issued_at.isoformat(), record.expires_at.isoformat(), record.status,
             record.closed_at.isoformat() if record.closed_at else None, record.record_origin))


class FakeAdapter(TestOnlyAdapter):
    def __init__(self, outcome=None, error=None):
        self.calls = 0
        self._lock = Lock()
        self.outcome = outcome
        self.error = error

    def execute(self, request):
        with self._lock:
            self.calls += 1
        if self.error:
            raise self.error
        return self.outcome if self.outcome is not None else AdapterOutcome(status="COMPLETED")


class FakeVerifier(TestOnlyOutcomeVerifier):
    def __init__(self, status="COMPLETED", *, wrong_key=False, order_id="order_001", capture_id=None):
        self.status = status
        self.wrong_key = wrong_key
        self.order_id = order_id
        self.capture_id = capture_id
        self.calls = 0

    def verify(self, snapshot):
        self.calls += 1
        return VerifiedOutcome(operation_id=snapshot.operation_id, status=self.status,
                               attempt_id=snapshot.attempt_id,
                               idempotency_key=("as-create-" + "0" * 32) if self.wrong_key else snapshot.idempotency_key,
                               provider_order_id=self.order_id, provider_capture_id=self.capture_id,
                               verified_at=NOW)


def store(db, *, timeout=1.0):
    return SQLitePaymentStore(db, trusted_issuer_id="issuer_001", clock=lambda: NOW, timeout=timeout)


def gateway(repository, adapter=None):
    fake = adapter or FakeAdapter()
    return PaymentGateway(PaymentPolicy(repository, clock=lambda: NOW), repository, repository, fake), fake


def operation_row(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT state,attempt_id,idempotency_key,provider_order_id,provider_capture_id FROM operations").fetchone()


def authorization_status(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT status FROM authorizations").fetchone()[0]


def test_persists_completed_result_and_one_claim_across_reinitialization(tmp_path):
    db = tmp_path / "payment.sqlite"
    first_store = store(db)
    seed_test_authorization(db, authorization())
    first_gateway, adapter = gateway(first_store)
    first = first_gateway.submit(intent(), CONTEXT)
    assert first.status == "COMPLETED" and adapter.calls == 1
    assert authorization_status(db) == "consumed"
    assert operation_row(db)[0] == "COMPLETED"
    assert operation_row(db)[2].startswith("as-create-")
    second_store = store(db)
    second_gateway, new_adapter = gateway(second_store)
    assert second_gateway.submit(intent(), CONTEXT) == first
    assert new_adapter.calls == 0


def test_duplicate_reservation_across_two_sqlite_connections(tmp_path):
    db = tmp_path / "payment.sqlite"
    one, two = store(db), store(db)
    request = intent()
    fp = _fingerprint(request, CONTEXT)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda repository: repository.reserve_intent(request, CONTEXT, fp), (one, two)))
    assert sorted(result.state for result in results) == ["NEW", "RESERVED"]
    assert operation_row(db)[0] == "RESERVED"


def test_store_rejects_unvalidated_intent_copy_without_persistence(tmp_path):
    db = tmp_path / "payment.sqlite"
    repository = store(db)
    bad = intent().model_copy(update={"action": "refund"})
    with pytest.raises(StoreUnavailable):
        repository.reserve_intent(bad, CONTEXT, _fingerprint(bad, CONTEXT))
    assert operation_row(db) is None


def test_separate_process_reservation_does_not_duplicate(tmp_path):
    db = tmp_path / "payment.sqlite"
    store(db)
    request = intent()
    script = (
        "import sys; from pathlib import Path; "
        "from agentsentinel.payment_sqlite import SQLitePaymentStore; "
        "from agentsentinel.payment_gateway import _fingerprint; "
        "from agentsentinel.payment_policy import AuthenticatedScope; "
        "from agentsentinel.schema.payment import PaymentIntent; "
        "r=PaymentIntent.model_validate_json(sys.argv[2]); "
        "c=AuthenticatedScope.model_validate_json(sys.argv[3]); "
        "s=SQLitePaymentStore(Path(sys.argv[1]),trusted_issuer_id='issuer_001'); "
        "print(s.reserve_intent(r,c,_fingerprint(r,c)).state)"
    )
    env = {name: os.environ[name] for name in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")
           if name in os.environ}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    args = [sys.executable, "-c", script, str(db), request.model_dump_json(), CONTEXT.model_dump_json()]
    processes = [subprocess.Popen(args, cwd=tmp_path, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(2)]
    outputs = [process.communicate(timeout=15) for process in processes]
    assert all(process.returncode == 0 for process in processes)
    assert sorted(output[0].strip() for output in outputs) == ["NEW", "RESERVED"]


def test_concurrent_gateway_submissions_execute_fake_at_most_once(tmp_path):
    db = tmp_path / "payment.sqlite"
    first_store = store(db)
    seed_test_authorization(db, authorization())
    second_store = store(db)
    adapter = FakeAdapter()
    one, _ = gateway(first_store, adapter)
    two, _ = gateway(second_store, adapter)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda component: component.submit(intent(), CONTEXT), (one, two)))
    assert adapter.calls == 1
    assert any(result.status == "COMPLETED" for result in results)
    assert all(result.status in {"COMPLETED", "IN_PROGRESS", "UNCERTAIN"} for result in results)


def test_concurrent_authorization_claims_one_winner(tmp_path):
    db = tmp_path / "payment.sqlite"
    first = store(db)
    seed_test_authorization(db, authorization())
    second = store(db)
    request = intent()
    assert first.reserve_intent(request, CONTEXT, _fingerprint(request, CONTEXT)).state == "NEW"
    decision = PaymentPolicy(first, clock=lambda: NOW).evaluate(request, CONTEXT)
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda repository: repository.claim(CONTEXT, request, decision), (first, second)))
    assert sorted(answers) == [False, True]
    assert authorization_status(db) == "consumed"
    assert operation_row(db)[0] == "CLAIMED"


@pytest.mark.parametrize("change", [
    {"intent_id": "intent_002"}, {"scope_id": "scope_002"},
    {"merchant_id": "merchant_002"}, {"money": Money(amount="3.01", currency="USD")},
])
def test_wrong_authorization_binding_never_executes(tmp_path, change):
    db = tmp_path / "payment.sqlite"
    repository = store(db)
    seed_test_authorization(db, authorization(**change))
    component, adapter = gateway(repository)
    assert component.submit(intent(), CONTEXT).status == "REJECTED"
    assert adapter.calls == 0


@pytest.mark.parametrize("change", [
    {"status": "revoked", "closed_at": NOW},
    {"status": "consumed", "closed_at": NOW},
    {"issued_at": NOW - timedelta(minutes=5), "expires_at": NOW},
])
def test_inactive_or_expired_authorization_never_executes(tmp_path, change):
    db = tmp_path / "payment.sqlite"
    repository = store(db)
    seed_test_authorization(db, authorization(**change))
    component, adapter = gateway(repository)
    assert component.submit(intent(), CONTEXT).status == "REJECTED"
    assert adapter.calls == 0


def test_revocation_and_trusted_scope_lookup(tmp_path):
    db = tmp_path / "payment.sqlite"
    repository = store(db)
    seed_test_authorization(db, authorization())
    wrong = AuthenticatedScope(principal_id="principal_002", scope_id="scope_001")
    assert repository.find(wrong, "intent_001", "operation_001") is None
    assert not repository.revoke("authorization_001", wrong)
    assert repository.revoke("authorization_001", CONTEXT)
    assert authorization_status(db) == "revoked"
    assert not repository.claim(CONTEXT, intent(), PaymentPolicy(repository, clock=lambda: NOW).evaluate(intent(), CONTEXT))


def test_database_lock_and_missing_database_fail_closed(tmp_path):
    db = tmp_path / "payment.sqlite"
    repository = store(db, timeout=0.01)
    seed_test_authorization(db, authorization())
    held = sqlite3.connect(db, isolation_level=None)
    try:
        held.execute("BEGIN IMMEDIATE")
        component, adapter = gateway(repository)
        result = component.submit(intent(), CONTEXT)
        assert result.status == "REJECTED" and result.reason_codes == ("OPERATION_RESERVATION_FAILED",)
        assert adapter.calls == 0
    finally:
        held.execute("ROLLBACK")
        held.close()
    assert operation_row(db) is None

    db.unlink()
    component, adapter = gateway(repository)
    assert component.submit(intent(), CONTEXT).status == "REJECTED"
    assert adapter.calls == 0


def test_claim_transaction_rolls_back_on_operation_update_failure(tmp_path):
    db = tmp_path / "payment.sqlite"
    repository = store(db)
    seed_test_authorization(db, authorization())
    request = intent()
    repository.reserve_intent(request, CONTEXT, _fingerprint(request, CONTEXT))
    decision = PaymentPolicy(repository, clock=lambda: NOW).evaluate(request, CONTEXT)
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TRIGGER fail_claim BEFORE UPDATE OF state ON operations
            WHEN NEW.state='CLAIMED' BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
    with pytest.raises(StoreUnavailable):
        repository.claim(CONTEXT, request, decision)
    assert authorization_status(db) == "issued"
    assert operation_row(db)[0] == "RESERVED"


def test_authorization_expiring_between_policy_and_claim_fails_closed(tmp_path):
    db = tmp_path / "payment.sqlite"
    current = [NOW]
    repository = SQLitePaymentStore(db, trusted_issuer_id="issuer_001", clock=lambda: current[0])
    seed_test_authorization(db, authorization(expires_at=NOW + timedelta(seconds=1)))
    request = intent()
    repository.reserve_intent(request, CONTEXT, _fingerprint(request, CONTEXT))
    decision = PaymentPolicy(repository, clock=lambda: current[0]).evaluate(request, CONTEXT)
    assert decision.outcome == "ALLOW"
    current[0] = NOW + timedelta(seconds=1)
    assert not repository.claim(CONTEXT, request, decision)
    assert authorization_status(db) == "issued"
    assert operation_row(db)[0] == "RESERVED"


def test_interruption_after_claim_requires_reconciliation_and_no_retry(tmp_path):
    db = tmp_path / "payment.sqlite"
    repository = store(db)
    seed_test_authorization(db, authorization())
    request = intent()
    repository.reserve_intent(request, CONTEXT, _fingerprint(request, CONTEXT))
    decision = PaymentPolicy(repository, clock=lambda: NOW).evaluate(request, CONTEXT)
    assert repository.claim(CONTEXT, request, decision)
    restarted, adapter = gateway(store(db))
    result = restarted.submit(request, CONTEXT)
    assert result.status == "UNCERTAIN" and result.reason_codes == ("RECONCILIATION_REQUIRED",)
    assert adapter.calls == 0
    assert operation_row(db)[1] is not None and operation_row(db)[2] is not None


def test_verified_reconciliation_and_forged_input_rejected(tmp_path):
    db = tmp_path / "payment.sqlite"
    repository = store(db)
    seed_test_authorization(db, authorization())
    request = intent()
    repository.reserve_intent(request, CONTEXT, _fingerprint(request, CONTEXT))
    decision = PaymentPolicy(repository, clock=lambda: NOW).evaluate(request, CONTEXT)
    assert repository.claim(CONTEXT, request, decision)
    verifier = FakeVerifier()
    reconciler = SQLiteReconciler(store(db), verifier)
    forged = VerifiedOutcome(operation_id=request.operation_id, status="COMPLETED",
                             attempt_id=operation_row(db)[1], idempotency_key=operation_row(db)[2],
                             provider_order_id="order_001", verified_at=NOW)
    with pytest.raises(TypeError):
        reconciler.reconcile(request.operation_id, forged)
    assert operation_row(db)[0] == "CLAIMED"
    completed = reconciler.reconcile(request.operation_id)
    assert completed.status == "COMPLETED" and verifier.calls == 1
    assert operation_row(db)[3] == "order_001"
    restarted, adapter = gateway(store(db))
    assert restarted.submit(request, CONTEXT) == completed
    assert adapter.calls == 0
    with pytest.raises(StoreUnavailable):
        reconciler.reconcile(request.operation_id)


def test_mismatched_verified_evidence_cannot_finalize(tmp_path):
    db = tmp_path / "payment.sqlite"
    repository = store(db)
    seed_test_authorization(db, authorization())
    request = intent()
    repository.reserve_intent(request, CONTEXT, _fingerprint(request, CONTEXT))
    decision = PaymentPolicy(repository, clock=lambda: NOW).evaluate(request, CONTEXT)
    assert repository.claim(CONTEXT, request, decision)
    with pytest.raises(StoreUnavailable):
        SQLiteReconciler(store(db), FakeVerifier(wrong_key=True)).reconcile(request.operation_id)
    assert operation_row(db)[0] == "CLAIMED"


def test_capture_target_is_unverified_until_reconciliation(tmp_path):
    db = tmp_path / "payment.sqlite"
    repository = store(db)
    request = intent(action="capture_order", target="order_001")
    seed_test_authorization(db, authorization(request))
    repository.reserve_intent(request, CONTEXT, _fingerprint(request, CONTEXT))
    assert operation_row(db)[3] is None
    decision = PaymentPolicy(repository, clock=lambda: NOW).evaluate(request, CONTEXT)
    assert repository.claim(CONTEXT, request, decision)
    with pytest.raises(StoreUnavailable):
        SQLiteReconciler(repository, FakeVerifier(order_id="order_002", capture_id="capture_001")).reconcile(
            request.operation_id)
    assert operation_row(db)[0] == "CLAIMED"
    result = SQLiteReconciler(repository, FakeVerifier(order_id="order_001", capture_id="capture_001")).reconcile(
        request.operation_id)
    assert result.status == "COMPLETED"
    assert operation_row(db)[3:] == ("order_001", "capture_001")


def test_uncertain_outcome_persists_and_no_automatic_retry(tmp_path):
    db = tmp_path / "payment.sqlite"
    repository = store(db)
    seed_test_authorization(db, authorization())
    adapter = FakeAdapter(error=TimeoutError("RAW-SECRET-RESPONSE"))
    component, _ = gateway(repository, adapter)
    first = component.submit(intent(), CONTEXT)
    assert first.status == "UNCERTAIN"
    restarted, fresh_adapter = gateway(store(db))
    assert restarted.submit(intent(), CONTEXT) == first
    assert adapter.calls == 1 and fresh_adapter.calls == 0
    for path in tmp_path.glob("payment.sqlite*"):
        content = path.read_bytes()
        assert b"RAW-SECRET-RESPONSE" not in content
        assert b"Notebook" not in content
        assert b"client_secret" not in content


def test_still_uncertain_reconciliation_persists_without_retry(tmp_path):
    db = tmp_path / "payment.sqlite"
    repository = store(db)
    seed_test_authorization(db, authorization())
    adapter = FakeAdapter(error=TimeoutError("private provider detail"))
    component, _ = gateway(repository, adapter)
    assert component.submit(intent(), CONTEXT).status == "UNCERTAIN"
    result = SQLiteReconciler(store(db), FakeVerifier("STILL_UNCERTAIN", order_id=None)).reconcile("operation_001")
    assert result.status == "UNCERTAIN"
    restarted, fresh_adapter = gateway(store(db))
    assert restarted.submit(intent(), CONTEXT) == result
    assert fresh_adapter.calls == 0


def test_raw_fake_provider_payload_is_not_persisted(tmp_path):
    db = tmp_path / "payment.sqlite"
    repository = store(db)
    seed_test_authorization(db, authorization())
    adapter = FakeAdapter(outcome={"status": "COMPLETED", "raw_response": "RAW-PRIVATE-PAYMENT-DATA"})
    component, _ = gateway(repository, adapter)
    result = component.submit(intent(), CONTEXT)
    assert result.status == "UNCERTAIN"
    assert "RAW-PRIVATE" not in result.model_dump_json()
    for path in tmp_path.glob("payment.sqlite*"):
        assert b"RAW-PRIVATE-PAYMENT-DATA" not in path.read_bytes()


def test_reconciliation_can_record_no_payment_and_audit_transition(tmp_path):
    db = tmp_path / "payment.sqlite"
    repository = store(db)
    seed_test_authorization(db, authorization())
    request = intent()
    repository.reserve_intent(request, CONTEXT, _fingerprint(request, CONTEXT))
    result = SQLiteReconciler(repository, FakeVerifier("NO_PAYMENT", order_id=None)).reconcile(request.operation_id)
    assert result.status == "REJECTED"
    with sqlite3.connect(db) as conn:
        events = conn.execute("SELECT from_state,to_state,event_type FROM operation_events ORDER BY event_id").fetchall()
    assert events == [(None, "RESERVED", "reserve"), ("RESERVED", "REJECTED", "verified_reconciliation")]


def test_trusted_context_facade_has_no_agent_scope_argument(tmp_path):
    db = tmp_path / "payment.sqlite"
    repository = store(db)
    seed_test_authorization(db, authorization())
    component, adapter = gateway(repository)

    class TestContextProvider:
        def current_context(self):
            return CONTEXT

    entry = TrustedGatewayEntryPoint(component, TestContextProvider())
    with pytest.raises(TypeError):
        entry.submit(intent(), AuthenticatedScope(principal_id="principal_002", scope_id="scope_002"))
    assert entry.submit(intent()).status == "COMPLETED"
    assert adapter.calls == 1
