"""Tests for the public proposal-to-gateway decorator."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentsentinel import TrustedGatewayEntryPoint, guard
from agentsentinel.approval_service import ApprovalStore
from agentsentinel.payment_gateway import (
    AdapterOutcome,
    GatewayResult,
    PaymentGateway,
    TestOnlyAdapter,
)
from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy
from agentsentinel.payment_sqlite import SQLitePaymentStore
from agentsentinel.schema.payment import Money, PaymentAuthorization, PaymentIntent

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
CONTEXT = AuthenticatedScope(principal_id="guard_user", scope_id="guard_scope")
ISSUER = "guard_issuer"
MERCHANT = "GUARDMERCHANT"


def make_intent(operation_id="guard_operation", *, amount="2.00", merchant=MERCHANT):
    return PaymentIntent(
        intent_id=f"intent_{operation_id}",
        operation_id=operation_id,
        trace_id=f"trace_{operation_id}",
        scope_id=CONTEXT.scope_id,
        money=Money(amount=amount, currency="USD"),
        merchant_id=merchant,
        purpose="Guard decorator test",
        action="create_order",
        requested_at=NOW,
    )


def seed_authorization(db, request, *, merchant=None):
    record = PaymentAuthorization(
        authorization_id=f"auth_{request.operation_id}",
        issuer_id=ISSUER,
        scope_id=request.scope_id,
        intent_id=request.intent_id,
        operation_id=request.operation_id,
        merchant_id=merchant or request.merchant_id,
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
                record.authorization_id,
                record.issuer_id,
                CONTEXT.principal_id,
                record.scope_id,
                record.intent_id,
                record.operation_id,
                record.merchant_id,
                str(record.money.amount),
                record.money.currency,
                record.action,
                record.target_order_id,
                record.issued_at.isoformat(),
                record.expires_at.isoformat(),
                record.status,
                None,
                record.record_origin,
            ),
        )


class FakeAdapter(TestOnlyAdapter):
    def __init__(self, *, error=None):
        self.calls = 0
        self.error = error

    def execute(self, _request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return AdapterOutcome(status="COMPLETED", provider_order_id="order_guard")


class Session:
    def current_context(self):
        return CONTEXT


def build_guard(tmp_path, request=None, *, seed=True, authorized_merchant=None, switch=False, adapter=None):
    request = request or make_intent()
    db = tmp_path / f"{request.operation_id}.sqlite"
    approvals = ApprovalStore(db)
    store = SQLitePaymentStore(db, trusted_issuer_id=ISSUER, clock=lambda: NOW)
    if seed:
        seed_authorization(db, request, merchant=authorized_merchant)
    if switch:
        approvals.set_kill_switch(enabled=True, actor_role="operator")
    fake = adapter or FakeAdapter()
    payment_gateway = PaymentGateway(
        PaymentPolicy(store, clock=lambda: NOW),
        store,
        store,
        fake,
        kill_switch=approvals,
    )
    entry_point = TrustedGatewayEntryPoint(payment_gateway, Session())

    @guard(entry_point)
    def purchase(proposal):
        return proposal

    return purchase, payment_gateway, store, approvals, fake


def test_guard_routes_proposal_through_trusted_entry_point(tmp_path):
    request = make_intent()
    purchase, _, _, _, adapter = build_guard(tmp_path, request)

    result = purchase(request)
    replay = purchase(request)

    assert isinstance(result, GatewayResult)
    assert result.status == "COMPLETED"
    assert result.decision.outcome == "ALLOW"
    assert replay == result
    assert adapter.calls == 1


def test_guard_denies_missing_authorization_without_adapter_call(tmp_path):
    purchase, _, _, _, adapter = build_guard(tmp_path, seed=False)

    result = purchase(make_intent())

    assert result.status == "REJECTED"
    assert result.decision.outcome == "BLOCK"
    assert "AUTHORIZATION_MISSING" in result.decision.reason_codes
    assert adapter.calls == 0


def test_guard_preserves_deterministic_policy_rejection(tmp_path):
    request = make_intent()
    purchase, _, _, _, adapter = build_guard(
        tmp_path, request, authorized_merchant="OTHERMERCHANT"
    )

    result = purchase(request)

    assert result.status == "REJECTED"
    assert result.decision.outcome == "BLOCK"
    assert "AUTHORIZATION_MERCHANT_MISMATCH" in result.decision.reason_codes
    assert adapter.calls == 0


def test_guard_does_not_bypass_kill_switch(tmp_path):
    purchase, _, _, _, adapter = build_guard(tmp_path, switch=True)

    result = purchase(make_intent())

    assert result.status == "REJECTED"
    assert result.reason_codes == ("KILL_SWITCH_ACTIVE",)
    assert adapter.calls == 0


def test_guard_returns_review_requirement_without_issuing_approval(tmp_path):
    request = make_intent(amount="6.00")
    purchase, _, store, _, adapter = build_guard(tmp_path, request)

    result = purchase(request)

    assert result.status == "REVIEW_REQUIRED"
    assert result.decision.outcome == "REVIEW"
    assert "REVIEW_AMOUNT_THRESHOLD" in result.decision.reason_codes
    assert adapter.calls == 0
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT status FROM authorizations WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone()[0] == "issued"


def test_guard_provider_failure_is_uncertain_and_never_retried(tmp_path):
    request = make_intent()
    adapter = FakeAdapter(error=TimeoutError("private provider response"))
    purchase, _, _, _, _ = build_guard(tmp_path, request, adapter=adapter)

    first = purchase(request)
    second = purchase(request)

    assert first.status == second.status == "UNCERTAIN"
    assert "private provider response" not in first.model_dump_json()
    assert adapter.calls == 1


def test_guard_does_not_expose_adapter_in_callable_interface(tmp_path):
    purchase, _, _, _, adapter = build_guard(tmp_path)

    assert not hasattr(purchase, "adapter")
    assert not hasattr(purchase, "_adapter")
    assert "adapter" not in vars(purchase)
    assert purchase.__wrapped__ is not None
    assert adapter.calls == 0


def test_guard_rejects_non_entrypoint_and_non_intent_proposals(tmp_path):
    _, payment_gateway, _, _, adapter = build_guard(tmp_path)

    @guard(TrustedGatewayEntryPoint(
        payment_gateway,
        Session(),
    ))
    def invalid_proposal():
        return "not a payment intent"

    with pytest.raises(TypeError, match="must return a PaymentIntent"):
        invalid_proposal()
    assert adapter.calls == 0


def test_direct_gateway_submission_semantics_remain_available(tmp_path):
    request = make_intent()
    purchase, payment_gateway, _, _, adapter = build_guard(tmp_path, request)

    decorated = purchase(request)
    cached = payment_gateway.submit(request, CONTEXT)

    assert decorated == cached
    assert cached.status == "COMPLETED"
    assert adapter.calls == 1
