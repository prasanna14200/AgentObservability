"""Offline crash-recovery tests; adapters and verifier are local test doubles."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, Lock, Thread

import pytest

from agentsentinel.approval_service import ApprovalStore
from agentsentinel.payment_gateway import (
    AdapterOutcome,
    GatewayResult,
    PaymentGateway,
    TestOnlyAdapter,
    _fingerprint,
)
from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy
from agentsentinel.payment_sqlite import (
    PaymentRecoveryWorker,
    SQLitePaymentStore,
    SQLiteReconciler,
    StoreUnavailable,
    TestOnlyOutcomeVerifier,
    VerifiedOutcome,
)
from agentsentinel.schema.payment import Money, PaymentAuthorization, PaymentIntent

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
CONTEXT = AuthenticatedScope(principal_id="recovery_user", scope_id="recovery_scope")


def make_intent(operation_id="operation_recovery"):
    return PaymentIntent(
        intent_id=f"intent_{operation_id}",
        operation_id=operation_id,
        trace_id=f"trace_{operation_id}",
        scope_id=CONTEXT.scope_id,
        money=Money(amount="2.00", currency="USD"),
        merchant_id="RECOVERYMERCHANT",
        purpose="Offline recovery test",
        action="create_order",
        requested_at=NOW,
    )


def seed_authorization(db, request):
    with sqlite3.connect(db) as conn:
        initialized = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='authorizations'"
        ).fetchone()
    if initialized is None:
        payment_store(db)
    authorization = PaymentAuthorization(
        authorization_id=f"auth_{request.operation_id}",
        issuer_id="recovery_issuer",
        scope_id=request.scope_id,
        intent_id=request.intent_id,
        operation_id=request.operation_id,
        merchant_id=request.merchant_id,
        money=request.money,
        action=request.action,
        target_order_id=request.target_order_id,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
        status="issued",
        record_origin="application_issued",
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO authorizations
            (authorization_id,issuer_id,principal_id,scope_id,intent_id,operation_id,merchant_id,
             amount_text,currency,action,target_order_id,issued_at,expires_at,status,closed_at,record_origin)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                authorization.authorization_id,
                authorization.issuer_id,
                CONTEXT.principal_id,
                authorization.scope_id,
                authorization.intent_id,
                authorization.operation_id,
                authorization.merchant_id,
                str(authorization.money.amount),
                authorization.money.currency,
                authorization.action,
                authorization.target_order_id,
                authorization.issued_at.isoformat(),
                authorization.expires_at.isoformat(),
                authorization.status,
                None,
                authorization.record_origin,
            ),
        )


def payment_store(db, clock=lambda: NOW):
    return SQLitePaymentStore(db, trusted_issuer_id="recovery_issuer", clock=clock)


def reserve_and_claim(repository, request, *, mark_dispatched=False):
    fingerprint = _fingerprint(request, CONTEXT)
    assert repository.reserve_intent(request, CONTEXT, fingerprint).state == "NEW"
    decision = PaymentPolicy(repository, clock=lambda: NOW).evaluate(request, CONTEXT)
    assert decision.outcome == "ALLOW"
    assert repository.claim(CONTEXT, request, decision)
    if mark_dispatched:
        assert repository.mark_dispatch_intent(request.operation_id, fingerprint)
    return fingerprint, decision


def row_state(db, operation_id="operation_recovery"):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT state,dispatch_state FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()


def recovery_events(db, operation_id="operation_recovery"):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            """SELECT from_state,to_state,event_type,actor_role,reason_code
            FROM operation_events WHERE operation_id=? AND event_type='recovery'""",
            (operation_id,),
        ).fetchall()


class FakeAdapter(TestOnlyAdapter):
    def __init__(self, *, error=None):
        self.calls = 0
        self._lock = Lock()
        self.error = error

    def execute(self, request):
        with self._lock:
            self.calls += 1
        if self.error:
            raise self.error
        return AdapterOutcome(status="COMPLETED", provider_order_id="order_recovery")


class FakeVerifier(TestOnlyOutcomeVerifier):
    def __init__(
        self,
        status="STILL_UNCERTAIN",
        *,
        error=None,
        wrong_operation=False,
        wrong_attempt=False,
        wrong_key=False,
        no_payment_evidence=None,
    ):
        self.status = status
        self.error = error
        self.wrong_operation = wrong_operation
        self.wrong_attempt = wrong_attempt
        self.wrong_key = wrong_key
        self.no_payment_evidence = no_payment_evidence
        self.calls = 0

    def verify(self, snapshot):
        self.calls += 1
        if self.error:
            raise self.error
        return VerifiedOutcome(
            operation_id=("operation_other" if self.wrong_operation else snapshot.operation_id),
            status=self.status,
            attempt_id=("attempt_other" if self.wrong_attempt else snapshot.attempt_id),
            idempotency_key=(
                "as-create-" + "0" * 32 if self.wrong_key else snapshot.idempotency_key
            ),
            provider_order_id="order_recovery" if self.status == "COMPLETED" else None,
            no_payment_evidence=self.no_payment_evidence,
            verified_at=NOW,
        )


def test_recovery_after_reservation_before_claim_rejects_without_consuming_auth(tmp_path):
    db = tmp_path / "recovery.sqlite"
    request = make_intent()
    seed_authorization(db, request)
    repository = payment_store(db)
    repository.reserve_intent(request, CONTEXT, _fingerprint(request, CONTEXT))

    report = PaymentRecoveryWorker(repository, stale_after=timedelta(0)).run_once()

    assert report.scanned == 1 and report.recovered_not_dispatched == 1
    assert row_state(db) == ("REJECTED", "NOT_DISPATCHED")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM authorizations").fetchone()[0] == "issued"
    assert recovery_events(db) == [
        ("RESERVED", "REJECTED", "recovery", "recovery", "OPERATION_NOT_DISPATCHED")
    ]


def test_recovery_after_claim_before_dispatch_never_retries(tmp_path):
    db = tmp_path / "recovery.sqlite"
    request = make_intent()
    seed_authorization(db, request)
    repository = payment_store(db)
    reserve_and_claim(repository, request)

    PaymentRecoveryWorker(repository, stale_after=timedelta(0)).run_once()
    adapter = FakeAdapter()
    result = PaymentGateway(PaymentPolicy(payment_store(db)), payment_store(db), payment_store(db), adapter).submit(
        request, CONTEXT
    )

    assert row_state(db) == ("REJECTED", "NOT_DISPATCHED")
    assert result.status == "REJECTED"
    assert adapter.calls == 0


def test_dispatch_marker_before_adapter_crash_recovers_as_uncertain(tmp_path):
    db = tmp_path / "recovery.sqlite"
    request = make_intent()
    seed_authorization(db, request)
    repository = payment_store(db)
    reserve_and_claim(repository, request, mark_dispatched=True)

    report = PaymentRecoveryWorker(repository, stale_after=timedelta(0)).run_once()
    adapter = FakeAdapter()
    result = PaymentGateway(PaymentPolicy(payment_store(db)), payment_store(db), payment_store(db), adapter).submit(
        request, CONTEXT
    )

    assert report.marked_uncertain == 1
    assert row_state(db) == ("UNCERTAIN", "MAY_HAVE_DISPATCHED")
    assert result.status == "UNCERTAIN"
    assert adapter.calls == 0


def test_provider_acceptance_then_crash_does_not_resubmit(tmp_path):
    db = tmp_path / "recovery.sqlite"
    request = make_intent()
    seed_authorization(db, request)
    repository = payment_store(db)
    reserve_and_claim(repository, request, mark_dispatched=True)
    accepted_transport = FakeAdapter()
    accepted_transport.execute(request)

    PaymentRecoveryWorker(repository, stale_after=timedelta(0)).run_once()
    restarted_adapter = FakeAdapter()
    result = PaymentGateway(
        PaymentPolicy(payment_store(db)),
        payment_store(db),
        payment_store(db),
        restarted_adapter,
    ).submit(request, CONTEXT)

    assert accepted_transport.calls == 1
    assert restarted_adapter.calls == 0
    assert result.status == "UNCERTAIN"


def test_provider_timeout_is_uncertain_and_never_blindly_retried(tmp_path):
    db = tmp_path / "recovery.sqlite"
    request = make_intent()
    seed_authorization(db, request)
    adapter = FakeAdapter(error=TimeoutError("private provider response"))
    repository = payment_store(db)
    gateway = PaymentGateway(
        PaymentPolicy(repository, clock=lambda: NOW),
        repository,
        repository,
        adapter,
    )

    first = gateway.submit(request, CONTEXT)
    second = PaymentGateway(
        PaymentPolicy(payment_store(db), clock=lambda: NOW),
        payment_store(db),
        payment_store(db),
        FakeAdapter(),
    ).submit(request, CONTEXT)

    assert first.status == second.status == "UNCERTAIN"
    assert adapter.calls == 1
    assert "private provider response" not in first.model_dump_json()


def test_dispatch_marker_event_failure_prevents_adapter_invocation(tmp_path):
    db = tmp_path / "recovery.sqlite"
    request = make_intent()
    seed_authorization(db, request)
    repository = payment_store(db)
    adapter = FakeAdapter()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TRIGGER fail_dispatch_event BEFORE INSERT ON operation_events
            WHEN NEW.event_type='dispatch_intent'
            BEGIN SELECT RAISE(ABORT,'simulated marker event failure'); END"""
        )
    gateway = PaymentGateway(
        PaymentPolicy(repository, clock=lambda: NOW),
        repository,
        repository,
        adapter,
    )

    result = gateway.submit(request, CONTEXT)

    assert result.status == "REJECTED"
    assert result.reason_codes == ("DISPATCH_INTENT_FAILED",)
    assert row_state(db) == ("REJECTED", "NOT_DISPATCHED")
    assert adapter.calls == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM operation_events WHERE event_type='dispatch_intent'"
        ).fetchone()[0] == 0


def test_unavailable_verifier_is_sanitized_and_does_not_change_operation(tmp_path):
    db = tmp_path / "recovery.sqlite"
    request = make_intent()
    seed_authorization(db, request)
    repository = payment_store(db)
    reserve_and_claim(repository, request, mark_dispatched=True)
    before_events = len(recovery_events(db))

    with pytest.raises(StoreUnavailable, match="provider verification unavailable"):
        SQLiteReconciler(repository, FakeVerifier(error=RuntimeError("secret response"))).reconcile(
            request.operation_id
        )

    assert row_state(db) == ("CLAIMED", "MAY_HAVE_DISPATCHED")
    assert len(recovery_events(db)) == before_events


@pytest.mark.parametrize(
    "mismatch",
    ["wrong_operation", "wrong_attempt", "wrong_key"],
)
def test_reconciliation_rejects_mismatched_operation_attempt_or_key(tmp_path, mismatch):
    db = tmp_path / f"{mismatch}.sqlite"
    request = make_intent()
    seed_authorization(db, request)
    repository = payment_store(db)
    reserve_and_claim(repository, request, mark_dispatched=True)
    verifier = FakeVerifier("COMPLETED", **{mismatch: True})

    with pytest.raises(StoreUnavailable):
        SQLiteReconciler(repository, verifier).reconcile(request.operation_id)

    assert row_state(db) == ("CLAIMED", "MAY_HAVE_DISPATCHED")


def test_current_dispatch_marker_survives_store_restart(tmp_path):
    db = tmp_path / "existing_dispatch_marker.sqlite"
    request = make_intent("operation_existing_marker")
    seed_authorization(db, request)
    repository = payment_store(db)
    reserve_and_claim(repository, request, mark_dispatched=False)

    payment_store(db)

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT state,dispatch_state FROM operations WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone() == ("CLAIMED", "NOT_DISPATCHED")


def test_repeated_reconciliation_does_not_duplicate_transition_events(tmp_path):
    db = tmp_path / "recovery.sqlite"
    request = make_intent()
    seed_authorization(db, request)
    repository = payment_store(db)
    reserve_and_claim(repository, request, mark_dispatched=True)
    verifier = FakeVerifier()
    reconciler = SQLiteReconciler(repository, verifier)

    first = reconciler.reconcile(request.operation_id)
    second = reconciler.reconcile(request.operation_id)

    assert first == second and second.status == "UNCERTAIN"
    assert verifier.calls == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM operation_events WHERE event_type='verified_reconciliation'"
        ).fetchone()[0] == 1


def test_two_recovery_workers_atomically_process_one_operation_once(tmp_path):
    db = tmp_path / "recovery.sqlite"
    request = make_intent()
    seed_authorization(db, request)
    one, two = payment_store(db), payment_store(db)
    one.reserve_intent(request, CONTEXT, _fingerprint(request, CONTEXT))
    barrier = Barrier(2)

    def recover(repository):
        barrier.wait(timeout=5)
        return PaymentRecoveryWorker(repository, stale_after=timedelta(0)).run_once()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(recover, (one, two)))

    assert sum(report.scanned for report in reports) == 1
    assert sum(report.recovered_not_dispatched for report in reports) == 1
    assert len(recovery_events(db)) == 1


def test_kill_switch_activation_serializes_with_recovery_and_blocks_later_admission(tmp_path):
    db = tmp_path / "recovery.sqlite"
    stale_request = make_intent()
    seed_authorization(db, stale_request)
    approvals = ApprovalStore(db)
    initial_store = payment_store(db)
    reserve_and_claim(initial_store, stale_request, mark_dispatched=True)
    recovery_entered, release_recovery = Event(), Event()

    class PausingRecoveryStore(SQLitePaymentStore):
        def _event(self, conn, operation_id, before, after, kind, now, **kwargs):
            if kind == "recovery":
                recovery_entered.set()
                if not release_recovery.wait(timeout=5):
                    raise TimeoutError("test recovery gate timed out")
            return super()._event(
                conn, operation_id, before, after, kind, now, **kwargs
            )

    repository = PausingRecoveryStore(
        db, trusted_issuer_id="recovery_issuer", clock=lambda: NOW
    )
    recovery_thread = Thread(
        target=lambda: PaymentRecoveryWorker(
            repository, stale_after=timedelta(0)
        ).run_once()
    )
    recovery_thread.start()
    assert recovery_entered.wait(timeout=5)

    activation_started, activation_done = Event(), Event()

    def activate_switch():
        activation_started.set()
        approvals.set_kill_switch(enabled=True)
        activation_done.set()

    activation_thread = Thread(target=activate_switch)
    activation_thread.start()
    assert activation_started.wait(timeout=5)
    try:
        assert not activation_done.wait(timeout=0.05)
    finally:
        release_recovery.set()
    recovery_thread.join(timeout=5)
    activation_thread.join(timeout=5)
    assert not recovery_thread.is_alive() and not activation_thread.is_alive()
    assert activation_done.is_set()

    next_request = make_intent("operation_after_switch")
    seed_authorization(db, next_request)
    adapter = FakeAdapter()
    gateway = PaymentGateway(
        PaymentPolicy(repository),
        repository,
        repository,
        adapter,
        kill_switch=approvals,
    )
    result = gateway.submit(next_request, CONTEXT)

    assert row_state(db) == ("UNCERTAIN", "MAY_HAVE_DISPATCHED")
    assert result.status == "REJECTED" and result.reason_codes == ("KILL_SWITCH_ACTIVE",)
    assert adapter.calls == 0


def test_database_write_failure_during_recovery_leaves_operation_unchanged(tmp_path):
    db = tmp_path / "recovery.sqlite"
    request = make_intent()
    seed_authorization(db, request)
    repository = payment_store(db)
    repository.reserve_intent(request, CONTEXT, _fingerprint(request, CONTEXT))
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TRIGGER fail_recovery_update BEFORE UPDATE OF state ON operations
            WHEN NEW.operation_id='operation_recovery' AND NEW.state='REJECTED'
            BEGIN SELECT RAISE(ABORT,'simulated recovery failure'); END"""
        )

    with pytest.raises(StoreUnavailable):
        PaymentRecoveryWorker(repository, stale_after=timedelta(0)).run_once()

    assert row_state(db) == ("RESERVED", "NOT_DISPATCHED")
    assert recovery_events(db) == []


def test_invalid_dispatch_state_fails_closed_during_recovery(tmp_path):
    db = tmp_path / "recovery.sqlite"
    request = make_intent()
    repository = payment_store(db)
    repository.reserve_intent(request, CONTEXT, _fingerprint(request, CONTEXT))
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "UPDATE operations SET dispatch_state='INVALID' WHERE operation_id=?",
            (request.operation_id,),
        )

    with pytest.raises(StoreUnavailable, match="dispatch state unavailable"):
        PaymentRecoveryWorker(repository, stale_after=timedelta(0)).run_once()

    assert row_state(db) == ("RESERVED", "INVALID")
    assert recovery_events(db) == []


def test_recovery_event_failure_rolls_back_state_transition(tmp_path):
    db = tmp_path / "recovery.sqlite"
    request = make_intent()
    seed_authorization(db, request)
    repository = payment_store(db)
    repository.reserve_intent(request, CONTEXT, _fingerprint(request, CONTEXT))
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TRIGGER fail_recovery_event BEFORE INSERT ON operation_events
            WHEN NEW.event_type='recovery'
            BEGIN SELECT RAISE(ABORT,'simulated audit failure'); END"""
        )

    with pytest.raises(StoreUnavailable):
        PaymentRecoveryWorker(repository, stale_after=timedelta(0)).run_once()

    assert row_state(db) == ("RESERVED", "NOT_DISPATCHED")
    assert recovery_events(db) == []


@pytest.mark.parametrize(
    ("persisted_state", "dispatch_state", "expected_status"),
    [
        ("RESERVED", "NOT_DISPATCHED", "IN_PROGRESS"),
        ("CLAIMED", "NOT_DISPATCHED", "UNCERTAIN"),
        ("CLAIMED", "MAY_HAVE_DISPATCHED", "UNCERTAIN"),
        ("COMPLETED", "MAY_HAVE_DISPATCHED", "COMPLETED"),
        ("UNCERTAIN", "MAY_HAVE_DISPATCHED", "UNCERTAIN"),
        ("REJECTED", "NOT_DISPATCHED", "REJECTED"),
        ("REVIEW_REQUIRED", "NOT_DISPATCHED", "REVIEW_REQUIRED"),
    ],
)
def test_restart_never_retries_any_persisted_lifecycle_state(
    tmp_path, persisted_state, dispatch_state, expected_status
):
    db = tmp_path / "restart.sqlite"
    request = make_intent()
    seed_authorization(db, request)
    repository = payment_store(db)
    fingerprint = _fingerprint(request, CONTEXT)
    if persisted_state == "RESERVED":
        repository.reserve_intent(request, CONTEXT, fingerprint)
    elif persisted_state == "REVIEW_REQUIRED":
        repository.reserve_intent(request, CONTEXT, fingerprint)
        repository.finish(
            request.operation_id,
            fingerprint,
            GatewayResult(
                operation_id=request.operation_id,
                decision=None,
                status="REVIEW_REQUIRED",
                reason_codes=("POLICY_REVIEW",),
            ),
        )
    else:
        _, decision = reserve_and_claim(
            repository, request, mark_dispatched=dispatch_state == "MAY_HAVE_DISPATCHED"
        )
        if persisted_state in {"COMPLETED", "UNCERTAIN", "REJECTED"}:
            final = GatewayResult(
                operation_id=request.operation_id,
                decision=decision if persisted_state == "COMPLETED" else None,
                status=persisted_state,
                reason_codes=("EXECUTED_FAKE",)
                if persisted_state == "COMPLETED"
                else ("PROVIDER_OUTCOME_UNCERTAIN",)
                if persisted_state == "UNCERTAIN"
                else ("RECOVERY_NOT_DISPATCHED",),
                provider_order_id="order_recovery" if persisted_state == "COMPLETED" else None,
            )
            repository.finish(request.operation_id, fingerprint, final)

    restarted = payment_store(db)
    adapter = FakeAdapter()
    result = PaymentGateway(
        PaymentPolicy(restarted), restarted, restarted, adapter
    ).submit(request, CONTEXT)

    assert result.status == expected_status
    assert adapter.calls == 0
    assert row_state(db) == (persisted_state, dispatch_state)


def test_old_claimed_rows_migrate_conservatively_to_may_have_dispatched(tmp_path):
    db = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE operations (
                operation_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,principal_id TEXT NOT NULL,
                scope_id TEXT NOT NULL,intent_id TEXT NOT NULL,merchant_id TEXT NOT NULL,
                amount_text TEXT NOT NULL,currency TEXT NOT NULL,action TEXT NOT NULL,target_order_id TEXT,
                state TEXT NOT NULL,result_json TEXT,authorization_id TEXT,attempt_id TEXT,
                idempotency_key TEXT,provider_order_id TEXT,provider_capture_id TEXT,updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE operation_events (
                event_id INTEGER PRIMARY KEY,operation_id TEXT NOT NULL,from_state TEXT,
                to_state TEXT NOT NULL,event_type TEXT NOT NULL,occurred_at TEXT NOT NULL
            )"""
        )
        for operation_id, state in (
            ("legacy_reserved", "RESERVED"),
            ("legacy_claimed", "CLAIMED"),
            ("legacy_uncertain", "UNCERTAIN"),
            ("legacy_completed", "COMPLETED"),
        ):
            conn.execute(
                """INSERT INTO operations
                (operation_id,fingerprint,principal_id,scope_id,intent_id,merchant_id,amount_text,
                 currency,action,state,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    operation_id,
                    "f" * 64,
                    CONTEXT.principal_id,
                    CONTEXT.scope_id,
                    f"intent_{operation_id}",
                    "RECOVERYMERCHANT",
                    "2.00",
                    "USD",
                    "create_order",
                    state,
                    NOW.isoformat(),
                ),
            )

    payment_store(db)

    with sqlite3.connect(db) as conn:
        states = dict(
            conn.execute("SELECT operation_id,dispatch_state FROM operations").fetchall()
        )
        event_columns = {row[1] for row in conn.execute("PRAGMA table_info(operation_events)")}

    assert states == {
        "legacy_reserved": "NOT_DISPATCHED",
        "legacy_claimed": "MAY_HAVE_DISPATCHED",
        "legacy_uncertain": "MAY_HAVE_DISPATCHED",
        "legacy_completed": "MAY_HAVE_DISPATCHED",
    }
    assert {"actor_role", "reason_code"} <= event_columns


def test_recovery_fails_closed_when_database_cannot_be_opened(tmp_path):
    db = tmp_path / "payment.sqlite"
    repository = payment_store(db)
    repository.path = tmp_path

    with pytest.raises(StoreUnavailable):
        PaymentRecoveryWorker(repository, stale_after=timedelta(0)).run_once()


def test_no_payment_after_dispatch_requires_operation_attempt_bound_verifier_evidence(tmp_path):
    db = tmp_path / "recovery.sqlite"
    request = make_intent()
    seed_authorization(db, request)
    repository = payment_store(db)
    reserve_and_claim(repository, request, mark_dispatched=True)

    with pytest.raises(StoreUnavailable):
        SQLiteReconciler(repository, FakeVerifier("NO_PAYMENT")).reconcile(request.operation_id)

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT state FROM operations WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone()[0] == "CLAIMED"

    result = SQLiteReconciler(
        repository,
        FakeVerifier("NO_PAYMENT", no_payment_evidence="PROVIDER_CONFIRMED"),
    ).reconcile(request.operation_id)

    assert result.status == "REJECTED"
