"""Offline gateway tests; every PayPal call is handled by an in-process spy."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from agentsentinel.payment_gateway import PaymentGateway
from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy
from agentsentinel.payment_sandbox_adapter import SandboxPaymentAdapter, SandboxStatusVerifier
from agentsentinel.payment_sqlite import SQLitePaymentStore, SQLiteReconciler
from agentsentinel.paypal_sandbox import PayPalSandboxClient, SandboxConfig
from agentsentinel.schema.payment import Money, PaymentAuthorization, PaymentIntent

MERCHANT = "ABCDEFGH"
CONTEXT = AuthenticatedScope(principal_id="demo_user", scope_id="demo_scope")


class SpyTransport:
    def __init__(self, *, timeout_post=False, order_status="APPROVED", create_status="CREATED"):
        self.calls = []
        self.timeout_post = timeout_post
        self.order_status = order_status
        self.create_status = create_status

    def request(self, method, path, headers, body):
        self.calls.append((method, path, headers.get("PayPal-Request-Id")))
        if path == "/v1/oauth2/token":
            return 200, {"access_token": "fake_test_token"}
        if method == "POST" and path == "/v2/checkout/orders":
            if self.timeout_post:
                raise TimeoutError("spy timeout")
            return 201, {"id": "ORDER123", "status": self.create_status}
        if method == "GET" and path == "/v2/checkout/orders/ORDER123":
            return 200, {"id": "ORDER123", "status": self.order_status, "purchase_units": [{
                "amount": {"currency_code": "USD", "value": "1.00"},
                "payee": {"merchant_id": MERCHANT},
                "payments": {"captures": [{"id": "CAPTURE123", "status": "COMPLETED"}]},
            }]}
        if method == "POST" and path == "/v2/checkout/orders/ORDER123/capture":
            return 201, {"id": "ORDER123", "status": "COMPLETED", "purchase_units": [{
                "payments": {"captures": [{"id": "CAPTURE123", "status": "COMPLETED"}]}
            }]}
        if method == "GET" and path == "/v2/payments/captures/CAPTURE123":
            return 200, {"id": "CAPTURE123", "status": "COMPLETED"}
        raise AssertionError("unexpected PayPal request")


def make_intent(*, amount="1.00", merchant=MERCHANT, action="create_order", operation="demo_create",
                target=None):
    return PaymentIntent(intent_id="intent_001", operation_id=operation, trace_id="trace_001",
                         scope_id=CONTEXT.scope_id, money=Money(amount=amount, currency="USD"),
                         merchant_id=merchant, purpose="Notebook", action=action,
                         target_order_id=target, requested_at=datetime.now(timezone.utc))


def seed(db, intent, *, amount=None, merchant=None):
    now = datetime.now(timezone.utc)
    auth = PaymentAuthorization(
        authorization_id="auth_" + intent.operation_id, issuer_id="demo_issuer",
        scope_id=intent.scope_id, intent_id=intent.intent_id, operation_id=intent.operation_id,
        merchant_id=merchant or intent.merchant_id,
        money=Money(amount=amount or str(intent.money.amount), currency="USD"),
        action=intent.action, target_order_id=intent.target_order_id,
        issued_at=now - timedelta(minutes=1), expires_at=now + timedelta(minutes=5),
        status="issued", record_origin="application_issued")
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("""INSERT INTO authorizations
            (authorization_id,issuer_id,principal_id,scope_id,intent_id,operation_id,merchant_id,
             amount_text,currency,action,target_order_id,issued_at,expires_at,status,closed_at,record_origin)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (auth.authorization_id, auth.issuer_id, CONTEXT.principal_id, auth.scope_id,
             auth.intent_id, auth.operation_id, auth.merchant_id, str(auth.money.amount),
             auth.money.currency, auth.action, auth.target_order_id,
             auth.issued_at.isoformat(), auth.expires_at.isoformat(), auth.status,
             None, auth.record_origin))


def setup(tmp_path, *, timeout_post=False):
    db = tmp_path / "payment.sqlite"
    store = SQLitePaymentStore(db, trusted_issuer_id="demo_issuer")
    spy = SpyTransport(timeout_post=timeout_post)
    client = PayPalSandboxClient(SandboxConfig("fake_client", "fake_secret", MERCHANT), spy)
    gateway = PaymentGateway(PaymentPolicy(store), store, store, SandboxPaymentAdapter(client),
                             enable_sandbox_execution=True)
    return db, store, spy, client, gateway


@pytest.mark.parametrize("case", ["amount", "merchant", "missing"])
def test_block_cases_call_paypal_zero_times(tmp_path, case):
    db, store, spy, _, gateway = setup(tmp_path)
    intent = make_intent(amount="2.00" if case == "amount" else "1.00",
                         merchant="OTHER123" if case == "merchant" else MERCHANT)
    if case != "missing":
        seed(db, intent, amount="1.00" if case == "amount" else None,
             merchant=MERCHANT if case == "merchant" else None)
    result = gateway.submit(intent, CONTEXT)
    assert result.status == "REJECTED" and result.decision.outcome == "BLOCK"
    assert spy.calls == []
    assert store.audit_operation(intent.operation_id).state == "REJECTED"


def test_agent_supplied_authorization_cannot_enter_gateway(tmp_path):
    _, _, spy, _, gateway = setup(tmp_path)
    with pytest.raises(TypeError):
        gateway.submit(make_intent(), CONTEXT, object())
    assert spy.calls == []


def test_create_duplicate_has_one_payment_post_and_persisted_audit(tmp_path):
    db, store, spy, _, gateway = setup(tmp_path)
    intent = make_intent()
    seed(db, intent)
    first = gateway.submit(intent, CONTEXT)
    second = gateway.submit(intent, CONTEXT)
    assert first == second and first.status == "COMPLETED"
    assert first.provider_order_id == "ORDER123"
    assert store.audit_operation(intent.operation_id).provider_order_id == "ORDER123"
    assert len([c for c in spy.calls if c[0] == "POST" and c[1] != "/v1/oauth2/token"]) == 1


def test_create_accepts_documented_payer_action_required_state(tmp_path):
    db, store, spy, _, gateway = setup(tmp_path)
    spy.create_status = "PAYER_ACTION_REQUIRED"
    intent = make_intent()
    seed(db, intent)
    result = gateway.submit(intent, CONTEXT)
    assert result.status == "COMPLETED" and result.provider_order_id == "ORDER123"
    assert len([c for c in spy.calls if c[1] == "/v2/checkout/orders"]) == 1


def test_capture_is_separately_authorized_and_has_distinct_key(tmp_path):
    db, store, spy, _, gateway = setup(tmp_path)
    create = make_intent()
    seed(db, create)
    assert gateway.submit(create, CONTEXT).status == "COMPLETED"
    capture = make_intent(action="capture_order", operation="demo_capture", target="ORDER123")
    seed(db, capture)
    result = gateway.submit(capture, CONTEXT)
    assert result.status == "COMPLETED" and result.provider_capture_id == "CAPTURE123"
    posts = [c for c in spy.calls if c[0] == "POST" and c[1] != "/v1/oauth2/token"]
    assert len(posts) == 2 and posts[0][2] != posts[1][2]
    assert store.audit_operation(capture.operation_id).state == "COMPLETED"


def test_uncertain_never_retries_automatically(tmp_path):
    db, store, spy, _, gateway = setup(tmp_path, timeout_post=True)
    intent = make_intent()
    seed(db, intent)
    first = gateway.submit(intent, CONTEXT)
    second = gateway.submit(intent, CONTEXT)
    assert first.status == second.status == "UNCERTAIN"
    assert store.audit_operation(intent.operation_id).state == "UNCERTAIN"
    assert len([c for c in spy.calls if c[1] == "/v2/checkout/orders"]) == 1
    # An unknown order ID cannot be discovered by read-only GET; still no retry.
    reconciled = SQLiteReconciler(store, SandboxStatusVerifier(
        PayPalSandboxClient(SandboxConfig("fake_client", "fake_secret", MERCHANT), spy)
    )).reconcile(intent.operation_id)
    assert reconciled.status == "UNCERTAIN"
    assert len([c for c in spy.calls if c[1] == "/v2/checkout/orders"]) == 1


def test_advisory_escalates_allow_and_cannot_relax_block(tmp_path):
    db, _, spy, _, gateway = setup(tmp_path)
    allowed = make_intent()
    seed(db, allowed)
    review = gateway.submit(allowed, CONTEXT, advisory_signals=("request_review",))
    assert review.status == "REVIEW_REQUIRED" and review.decision.reason_codes == ("REVIEW_ADVISORY_SIGNAL",)
    blocked = make_intent(operation="demo_blocked", amount="2.00")
    seed(db, blocked, amount="1.00")
    result = gateway.submit(blocked, CONTEXT, advisory_signals=("request_review",))
    assert result.status == "REJECTED" and result.decision.outcome == "BLOCK"
    assert spy.calls == []


def test_manual_issuer_binds_fields_without_a_provider_call(tmp_path):
    from scripts.issue_authorization import demo_context, issue_after_local_confirmation

    db, store, spy, _, _ = setup(tmp_path)
    intent = make_intent().model_copy(update={"scope_id": demo_context().scope_id})
    record = issue_after_local_confirmation(db, intent)
    trusted = SQLitePaymentStore(db, trusted_issuer_id=record.issuer_id)
    found = trusted.find(demo_context(), intent.intent_id, intent.operation_id)
    assert found == record and found.binds_to(intent)
    assert trusted.find(CONTEXT, intent.intent_id, intent.operation_id) is None
    assert spy.calls == []


def test_read_only_verifier_checks_capture_status_without_post(tmp_path):
    from agentsentinel.payment_sqlite import ReconciliationSnapshot

    db, store, spy, client, gateway = setup(tmp_path)
    capture = make_intent(action="capture_order", operation="demo_capture", target="ORDER123")
    seed(db, capture)
    assert gateway.submit(capture, CONTEXT).status == "COMPLETED"
    spy.order_status = "COMPLETED"
    before = len([call for call in spy.calls if call[0] == "POST"])
    snapshot = ReconciliationSnapshot(
        operation_id=capture.operation_id, state="UNCERTAIN", action="capture_order",
        merchant_id=MERCHANT, amount_text="1.00", currency="USD",
        target_order_id="ORDER123", attempt_id="attempt_001", idempotency_key=None,
        provider_order_id="ORDER123", provider_capture_id="CAPTURE123", fingerprint="f" * 64,
    )
    verified = SandboxStatusVerifier(client).verify(snapshot)
    assert verified.status == "COMPLETED" and verified.provider_capture_id == "CAPTURE123"
    assert len([call for call in spy.calls if call[0] == "POST"]) == before
