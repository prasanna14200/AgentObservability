from __future__ import annotations

import sqlite3
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
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
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


def test_readiness_fails_when_persisted_kill_switch_state_is_unavailable(tmp_path):
    db_path = tmp_path / "readiness.sqlite"
    app = create_app(db_path=db_path, approver_secret="d" * 40)
    client = TestClient(app)

    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM kill_switch WHERE key='payment_kill_switch'")

    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "service not ready"}
    assert client.get("/health").status_code == 200


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


def test_approval_creation_and_issuance_audit_are_transactional(tmp_path):
    db = tmp_path / "approval_audit_transaction.sqlite"
    store = ApprovalStore(db, ttl_seconds=3600, clock=lambda: NOW)
    context = AuthenticatedScope(principal_id="principal_001", scope_id="scope_001")
    intent = make_intent(operation_id="op_approval_audit")

    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TRIGGER reject_approval_audit BEFORE INSERT ON approval_events
            WHEN NEW.action='approval_created'
            BEGIN SELECT RAISE(ABORT, 'test audit failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.create_approval(intent=intent, context=context)
    assert store.list_approvals() == []

    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER reject_approval_audit")
    pending = store.create_approval(intent=intent, context=context)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TRIGGER reject_approval_audit BEFORE INSERT ON approval_events
            WHEN NEW.action='approval_approved'
            BEGIN SELECT RAISE(ABORT, 'test audit failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.approve_approval(pending.approval_id)
    assert store.get_approval(pending.approval_id).status == "PENDING"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM authorizations WHERE operation_id=?",
            (pending.operation_id,),
        ).fetchone()[0] == 0


def test_approval_api_requires_matching_csrf_token(tmp_path):
    secret = "b" * 40
    app = create_app(db_path=tmp_path / "csrf.sqlite", approver_secret=secret)
    client = TestClient(app)
    store = app.state.approval_store
    context = AuthenticatedScope(principal_id="principal_001", scope_id="scope_001")
    record = store.create_approval(intent=make_intent(), context=context)
    headers = {"Authorization": "Bearer " + secret}

    missing = client.post(
        f"/approvals/{record.approval_id}/approve",
        json={"request_fingerprint": record.request_fingerprint},
        headers=headers,
    )
    mismatched = client.post(
        f"/approvals/{record.approval_id}/approve",
        json={"request_fingerprint": record.request_fingerprint},
        headers={**headers, "X-CSRF-Token": "token-a", "Cookie": "csrf_token=token-b"},
    )

    assert missing.status_code == mismatched.status_code == 403
    assert store.get_approval(record.approval_id).status == "PENDING"


@pytest.mark.parametrize(
    "reason",
    [
        "UNKNOWN_REASON",
        "",
        "X" * 65,
        "BAD\nREASON",
        "Bearer secret-token-value",
        "REJECTED_€",
    ],
)
def test_rejection_api_rejects_unsafe_reason_codes_without_reflection(tmp_path, reason):
    secret = "c" * 40
    app = create_app(db_path=tmp_path / "reason.sqlite", approver_secret=secret)
    store = app.state.approval_store
    record = store.create_approval(
        intent=make_intent(),
        context=AuthenticatedScope(principal_id="principal_001", scope_id="scope_001"),
    )
    client = TestClient(app)
    response = client.post(
        f"/approvals/{record.approval_id}/reject",
        json={"reason_code": reason},
        headers={
            "Authorization": "Bearer " + secret,
            "X-CSRF-Token": "csrf",
            "Cookie": "csrf_token=csrf",
        },
    )

    assert response.status_code == 400
    assert not reason or reason not in response.text
    assert store.get_approval(record.approval_id).status == "PENDING"


def test_rejection_api_accepts_a_known_reason_code(tmp_path):
    secret = "d" * 40
    app = create_app(db_path=tmp_path / "known_reason.sqlite", approver_secret=secret)
    store = app.state.approval_store
    record = store.create_approval(
        intent=make_intent(),
        context=AuthenticatedScope(principal_id="principal_001", scope_id="scope_001"),
    )
    response = TestClient(app).post(
        f"/approvals/{record.approval_id}/reject",
        json={"reason_code": "REJECTION_POLICY_CONCERN"},
        headers={
            "Authorization": "Bearer " + secret,
            "X-CSRF-Token": "csrf",
            "Cookie": "csrf_token=csrf",
        },
    )

    assert response.status_code == 200
    assert response.json()["decision_reason"] == "REJECTION_POLICY_CONCERN"


@pytest.mark.parametrize("enabled", [True, False])
def test_kill_switch_api_accepts_json_booleans(tmp_path, enabled):
    secret = "e" * 40
    app = create_app(db_path=tmp_path / "kill_bool.sqlite", approver_secret=secret)
    response = TestClient(app).post(
        "/killswitch",
        json={"enabled": enabled},
        headers={
            "Authorization": "Bearer " + secret,
            "X-CSRF-Token": "csrf",
            "Cookie": "csrf_token=csrf",
        },
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is enabled


@pytest.mark.parametrize("enabled", ["false", "true", 0, 1, None, ""])
def test_kill_switch_api_rejects_non_boolean_values(tmp_path, enabled):
    secret = "f" * 40
    app = create_app(db_path=tmp_path / "invalid_kill_bool.sqlite", approver_secret=secret)
    store = app.state.approval_store
    response = TestClient(app).post(
        "/killswitch",
        json={"enabled": enabled},
        headers={
            "Authorization": "Bearer " + secret,
            "X-CSRF-Token": "csrf",
            "Cookie": "csrf_token=csrf",
        },
    )

    assert response.status_code == 400
    assert store.read_kill_switch().enabled is False


def test_kill_switch_api_rejects_missing_enabled_field(tmp_path):
    secret = "g" * 40
    app = create_app(db_path=tmp_path / "missing_kill_bool.sqlite", approver_secret=secret)
    response = TestClient(app).post(
        "/killswitch",
        json={},
        headers={
            "Authorization": "Bearer " + secret,
            "X-CSRF-Token": "csrf",
            "Cookie": "csrf_token=csrf",
        },
    )

    assert response.status_code == 400
