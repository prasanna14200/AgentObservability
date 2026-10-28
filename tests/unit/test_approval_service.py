from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agentsentinel.approval_service import ApprovalRecord, ApprovalStore, create_app
from agentsentinel.payment_policy import AuthenticatedScope
from agentsentinel.schema.payment import Money, PaymentIntent

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def make_intent(operation_id: str = "op_approval_001") -> PaymentIntent:
    return PaymentIntent(
        intent_id="intent_approval_001",
        operation_id=operation_id,
        trace_id="trace_approval_001",
        scope_id="scope_001",
        money=Money(amount="4.00", currency="USD"),
        merchant_id="merchant_001",
        purpose="Notebook",
        action="create_order",
        requested_at=NOW,
    )


def test_approval_store_approve_reject_and_expire(tmp_path):
    db = tmp_path / "approvals.sqlite"
    store = ApprovalStore(db, ttl_seconds=3600, clock=lambda: NOW)
    context = AuthenticatedScope(principal_id="principal_001", scope_id="scope_001")
    intent = make_intent()

    record = store.create_approval(intent=intent, context=context, reason_codes=("REVIEW_AMOUNT_THRESHOLD",))
    assert record.status == "PENDING"
    assert record.request_fingerprint

    approved = store.approve_approval(record.approval_id, request_fingerprint=record.request_fingerprint)
    assert approved.status == "APPROVED"
    assert store.get_approval(record.approval_id).status == "APPROVED"

    with pytest.raises(ValueError):
        store.reject_approval(record.approval_id)

    expired = store.create_approval(
        intent=make_intent(operation_id="op_approval_002"),
        context=context,
        expires_at=NOW - timedelta(minutes=1),
    )
    store.expire_pending_approvals()
    assert store.get_approval(expired.approval_id).status == "EXPIRED"


def test_fastapi_service_requires_bearer_auth_and_kill_switch_state(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVER_SECRET", "a" * 32)
    app = create_app(db_path=tmp_path / "service.sqlite", approver_secret="a" * 32)
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/approvals").status_code == 401

    resp = client.get("/approvals", headers={"Authorization": "Bearer " + "a" * 32})
    assert resp.status_code == 200

    state = client.get("/killswitch", headers={"Authorization": "Bearer " + "a" * 32})
    assert state.status_code == 200
    assert state.json()["enabled"] is False

    danger = client.post(
        "/killswitch",
        json={"enabled": True, "reason_code": "KILL_SWITCH_ENABLED"},
        headers={
            "Authorization": "Bearer " + "a" * 32,
            "X-CSRF-Token": "abc",
            "Cookie": "csrf_token=abc",
        },
    )
    assert danger.status_code == 200
    assert danger.json()["enabled"] is True

    approval = app.state.approval_store if hasattr(app, "state") else None
    if approval is not None:
        with pytest.raises(ValueError, match="kill switch active"):
            approval.create_approval(
                intent=make_intent(operation_id="op_approval_003"),
                context=AuthenticatedScope(principal_id="principal_001", scope_id="scope_001"),
            )


def test_approval_store_issuance_is_atomic_under_concurrency(tmp_path):
    db = tmp_path / "atomic_approvals.sqlite"
    store = ApprovalStore(db, ttl_seconds=3600, clock=lambda: NOW)
    context = AuthenticatedScope(principal_id="principal_001", scope_id="scope_001")
    intent = make_intent(operation_id="op_approval_concurrent")

    def create_once() -> ApprovalRecord:
        return store.create_approval(intent=intent, context=context)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create_once(), range(2)))

    approval_ids = {record.approval_id for record in results}
    assert len(approval_ids) == 1
    record = store.get_approval(next(iter(approval_ids)))
    assert record is not None and record.status == "PENDING"


def test_approval_store_kill_switch_fails_closed_and_blocks_new_issuance(tmp_path):
    db = tmp_path / "kill_switch.sqlite"
    store = ApprovalStore(db, ttl_seconds=3600, clock=lambda: NOW)
    context = AuthenticatedScope(principal_id="principal_001", scope_id="scope_001")

    state = store.set_kill_switch(enabled=True, actor_role="approver", reason_code="KILL_SWITCH_ENABLED")
    assert state.enabled is True

    with pytest.raises(ValueError, match="kill switch active"):
        store.create_approval(intent=make_intent(operation_id="op_approval_kill_1"), context=context)

    with pytest.raises(ValueError, match="kill switch active"):
        store.create_approval(intent=make_intent(operation_id="op_approval_kill_2"), context=context)
