from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentsentinel.approval_service import ApprovalRecord, ApprovalStore, create_app
from agentsentinel.payment_policy import AuthenticatedScope
from agentsentinel.schema.payment import Money, PaymentIntent

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def test_import_does_not_initialize_default_approval_database(tmp_path):
    source_root = Path(__file__).resolve().parents[2] / "src"
    environment = os.environ.copy()
    environment.pop("APPROVAL_DB_PATH", None)
    environment["PYTHONPATH"] = str(source_root)
    script = """
import sqlite3
from pathlib import Path

def reject_connection(*args, **kwargs):
    raise AssertionError("module import connected to SQLite")

sqlite3.connect = reject_connection
import agentsentinel.approval_service
assert not Path("data/paypal_sandbox").exists()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "data" / "paypal_sandbox").exists()


def test_create_app_initializes_explicit_temporary_database(tmp_path):
    database_path = tmp_path / "approval.sqlite"

    app = create_app(db_path=database_path, approver_secret="a" * 32)

    assert database_path.is_file()
    assert app.state.approval_store.path == database_path


def test_uvicorn_factory_loads_create_app_with_temporary_database(
    tmp_path, monkeypatch
):
    from uvicorn import Config

    database_path = tmp_path / "uvicorn-factory.sqlite"
    monkeypatch.setenv("APPROVAL_DB_PATH", str(database_path))
    config = Config("agentsentinel.approval_service:create_app", factory=True)

    config.load()

    assert config.loaded_app is not None
    assert database_path.is_file()


def test_module_execution_creates_app_without_starting_server(tmp_path):
    source_root = Path(__file__).resolve().parents[2] / "src"
    database_path = tmp_path / "module-startup.sqlite"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    environment["APPROVAL_DB_PATH"] = str(database_path)
    script = """
import os
import runpy
import sys
import types
from pathlib import Path

calls = []
uvicorn = types.ModuleType("uvicorn")
uvicorn.run = lambda app, **kwargs: calls.append((app, kwargs))
sys.modules["uvicorn"] = uvicorn
runpy.run_module("agentsentinel.approval_service", run_name="__main__")
assert len(calls) == 1
app, options = calls[0]
assert app.state.approval_store.path == Path(os.environ["APPROVAL_DB_PATH"])
assert options == {"host": "127.0.0.1", "port": 8000}
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert database_path.is_file()


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
    assert [
        event.action
        for event in store.list_events(approval_id=expired.approval_id)
    ].count("approval_expired") == 1

    expired_on_approval = store.create_approval(
        intent=make_intent(operation_id="op_approval_expire_on_approve"),
        context=context,
        expires_at=NOW - timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="approval expired"):
        store.approve_approval(
            expired_on_approval.approval_id,
            request_fingerprint=expired_on_approval.request_fingerprint,
        )
    assert store.get_approval(expired_on_approval.approval_id).status == "EXPIRED"


def test_approval_reads_report_effective_expiry_without_persisting_it(tmp_path):
    secret = "read-only-approval-test-secret-0001"
    app = create_app(
        db_path=tmp_path / "approval_read_only.sqlite",
        approver_secret=secret,
    )
    store = app.state.approval_store
    records = [
        store.create_approval(
            intent=make_intent(f"op_read_expired_{index}"),
            context=AuthenticatedScope(principal_id="principal_001", scope_id="scope_001"),
            expires_at=NOW - timedelta(minutes=index + 1),
        )
        for index in range(2)
    ]
    events_before = store.list_events()

    assert [record.status for record in store.list_approvals()] == ["PENDING", "PENDING"]

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {secret}"}
    listed = client.get("/approvals", headers=headers)
    filtered_pending = client.get("/approvals?status=PENDING", headers=headers)
    filtered_expired = client.get("/approvals?status=EXPIRED", headers=headers)
    detailed = client.get(f"/approvals/{records[0].approval_id}", headers=headers)
    audit = client.get("/audit", headers=headers)
    kill_switch = client.get("/killswitch", headers=headers)

    assert listed.status_code == filtered_pending.status_code == filtered_expired.status_code == 200
    assert audit.status_code == kill_switch.status_code == 200
    assert [item["status"] for item in listed.json()] == ["EXPIRED", "EXPIRED"]
    assert filtered_pending.json() == []
    assert {
        item["approval_id"] for item in filtered_expired.json()
    } == {record.approval_id for record in records}
    assert detailed.status_code == 200
    assert detailed.json()["status"] == "EXPIRED"
    assert store.list_events() == events_before
    assert [store.get_approval(record.approval_id).status for record in records] == [
        "PENDING",
        "PENDING",
    ]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM approvals WHERE status='PENDING'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_events WHERE action='approval_expired'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("accept", "expected_status"),
    [
        pytest.param(None, 200, id="missing-accept"),
        pytest.param("*/*", 200, id="wildcard-accept"),
        pytest.param("application/json", 200, id="json-only"),
        pytest.param("application/json, text/html", 200, id="ambiguous-equal"),
        pytest.param("text/html, application/json;q=0.9", 303, id="html-preferred"),
        pytest.param("text/html", 303, id="html-only"),
    ],
)
def test_approvals_route_preserves_json_clients_and_redirects_html(
    tmp_path, accept, expected_status
):
    secret = "html-redirect-approval-test-secret"
    app = create_app(
        db_path=tmp_path / "approval_content_negotiation.sqlite",
        approver_secret=secret,
    )
    client = TestClient(app)
    if accept is None:
        client.headers.pop("accept", None)
        request_headers = {"Authorization": f"Bearer {secret}"}
    else:
        request_headers = {
            "Accept": accept,
            "Authorization": f"Bearer {secret}",
        }

    response = client.get(
        "/approvals",
        headers=request_headers,
        follow_redirects=False,
    )

    assert response.status_code == expected_status
    if expected_status == 303:
        assert response.headers["location"] == "/dashboard/approvals"
        assert "Pending approvals" not in response.text
    else:
        assert isinstance(response.json(), list)


def test_approvals_route_redirects_unauthenticated_html_but_rejects_json(tmp_path):
    app = create_app(
        db_path=tmp_path / "approval_unauthenticated_content.sqlite",
        approver_secret="unauthenticated-approval-test-secret",
    )
    store = app.state.approval_store
    record = store.create_approval(
        intent=make_intent("op_unauthenticated_html_redirect"),
        context=AuthenticatedScope(principal_id="principal_001", scope_id="scope_001"),
        expires_at=NOW - timedelta(minutes=1),
    )
    events_before = store.list_events()
    client = TestClient(app)

    html = client.get("/approvals", headers={"Accept": "text/html"}, follow_redirects=False)
    json_response = client.get("/approvals", headers={"Accept": "application/json"})
    dashboard = client.get("/dashboard/approvals", follow_redirects=False)

    assert html.status_code == 303
    assert html.headers["location"] == "/dashboard/approvals"
    assert json_response.status_code == 401
    assert dashboard.status_code == 303
    assert dashboard.headers["location"] == "/dashboard/login"
    assert store.get_approval(record.approval_id).status == "PENDING"
    assert store.list_events() == events_before


def test_wrong_fingerprint_does_not_expire_target_or_other_pending_approvals(tmp_path):
    store = ApprovalStore(tmp_path / "approval_wrong_fingerprint.sqlite", clock=lambda: NOW)
    context = AuthenticatedScope(principal_id="principal_001", scope_id="scope_001")
    target = store.create_approval(
        intent=make_intent("op_approval_expired_target"),
        context=context,
        expires_at=NOW - timedelta(minutes=2),
    )
    unrelated = store.create_approval(
        intent=make_intent("op_approval_expired_unrelated"),
        context=context,
        expires_at=NOW - timedelta(minutes=1),
    )
    events_before = store.list_events()
    wrong_fingerprint = (
        "0" * 64
        if target.request_fingerprint != "0" * 64
        else "1" * 64
    )

    with pytest.raises(ValueError, match="approval request fingerprint mismatch"):
        store.approve_approval(
            target.approval_id,
            request_fingerprint=wrong_fingerprint,
        )

    assert store.get_approval(target.approval_id).status == "PENDING"
    assert store.get_approval(unrelated.approval_id).status == "PENDING"
    assert store.list_events() == events_before
    assert all(
        event.action != "approval_approved"
        for event in store.list_events(approval_id=target.approval_id)
    )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0] == 0


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_expiry_write_processes_only_target_approval(tmp_path, decision):
    store = ApprovalStore(
        tmp_path / f"target_expiry_{decision}.sqlite",
        clock=lambda: NOW,
    )
    context = AuthenticatedScope(principal_id="principal_001", scope_id="scope_001")
    target = store.create_approval(
        intent=make_intent(f"op_target_expiry_{decision}"),
        context=context,
        expires_at=NOW - timedelta(minutes=2),
    )
    unrelated = store.create_approval(
        intent=make_intent(f"op_unrelated_expiry_{decision}"),
        context=context,
        expires_at=NOW - timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="approval expired"):
        if decision == "approve":
            store.approve_approval(
                target.approval_id,
                request_fingerprint=target.request_fingerprint,
            )
        else:
            store.reject_approval(target.approval_id)

    assert store.get_approval(target.approval_id).status == "EXPIRED"
    assert store.get_approval(unrelated.approval_id).status == "PENDING"
    target_events = store.list_events(approval_id=target.approval_id)
    assert [event.action for event in target_events].count("approval_expired") == 1
    assert all(event.action != "approval_approved" for event in target_events)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM authorizations WHERE operation_id=?",
            (target.operation_id,),
        ).fetchone()[0] == 0


def test_reject_api_persists_only_target_expiry(tmp_path):
    secret = "expired-rejection-api-test-secret"
    app = create_app(
        db_path=tmp_path / "expired_rejection_api.sqlite",
        approver_secret=secret,
    )
    store = app.state.approval_store
    context = AuthenticatedScope(principal_id="principal_001", scope_id="scope_001")
    target = store.create_approval(
        intent=make_intent("op_api_expired_reject_target"),
        context=context,
        expires_at=NOW - timedelta(minutes=2),
    )
    unrelated = store.create_approval(
        intent=make_intent("op_api_expired_reject_unrelated"),
        context=context,
        expires_at=NOW - timedelta(minutes=1),
    )
    client = TestClient(app)

    response = client.post(
        f"/approvals/{target.approval_id}/reject",
        json={"reason_code": "HUMAN_REJECTED"},
        headers={
            "Authorization": f"Bearer {secret}",
            "X-CSRF-Token": "csrf-token",
            "Cookie": "csrf_token=csrf-token",
        },
    )

    assert response.status_code == 409
    assert store.get_approval(target.approval_id).status == "EXPIRED"
    assert store.get_approval(unrelated.approval_id).status == "PENDING"
    assert [
        event.action for event in store.list_events(approval_id=target.approval_id)
    ].count("approval_expired") == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM authorizations"
        ).fetchone()[0] == 0


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


@pytest.mark.parametrize("approver_secret", [None, "too-short"])
def test_readiness_requires_valid_dashboard_approver_configuration(
    tmp_path, monkeypatch, approver_secret
):
    monkeypatch.delenv("APPROVER_SECRET", raising=False)
    app = create_app(
        db_path=tmp_path / "readiness_auth.sqlite",
        approver_secret=approver_secret,
    )
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "service not ready"}
    assert approver_secret is None or approver_secret not in response.text
    assert client.get("/health").status_code == 200


def test_readiness_accepts_valid_dashboard_approver_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("APPROVER_SECRET", raising=False)
    app = create_app(
        db_path=tmp_path / "readiness_auth_valid.sqlite",
        approver_secret="valid-readiness-secret-" + "x" * 32,
    )

    response = TestClient(app).get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


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
        store.approve_approval(
            pending.approval_id,
            request_fingerprint=pending.request_fingerprint,
        )
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
    ("csrf_headers", "csrf_cookie"),
    [
        pytest.param({}, "valid-csrf", id="missing-header"),
        pytest.param({"X-CSRF-Token": "valid-csrf"}, None, id="missing-cookie"),
        pytest.param(
            {"X-CSRF-Token": "header-token"},
            "cookie-token",
            id="mismatched-header-and-cookie",
        ),
    ],
)
def test_approval_api_rejects_invalid_csrf_without_mutation(
    tmp_path, csrf_headers, csrf_cookie
):
    approver_secret = "a" * 40
    app = create_app(
        db_path=tmp_path / "approval_csrf_api.sqlite",
        approver_secret=approver_secret,
    )
    client = TestClient(app)
    store = app.state.approval_store
    record = store.create_approval(
        intent=make_intent(),
        context=AuthenticatedScope(principal_id="principal_001", scope_id="scope_001"),
    )
    events_before = store.list_events()
    headers = {"Authorization": f"Bearer {approver_secret}", **csrf_headers}
    if csrf_cookie is not None:
        headers["Cookie"] = f"csrf_token={csrf_cookie}"

    response = client.post(
        f"/approvals/{record.approval_id}/approve",
        json={"request_fingerprint": record.request_fingerprint},
        headers=headers,
    )

    assert response.status_code == 403
    assert store.get_approval(record.approval_id).status == "PENDING"
    assert store.list_events() == events_before
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM authorizations WHERE operation_id=?",
            (record.operation_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("csrf_headers", "csrf_cookie"),
    [
        pytest.param({}, "valid-csrf", id="missing-header"),
        pytest.param({"X-CSRF-Token": "valid-csrf"}, None, id="missing-cookie"),
        pytest.param(
            {"X-CSRF-Token": "header-token"},
            "cookie-token",
            id="mismatched-header-and-cookie",
        ),
    ],
)
def test_kill_switch_api_rejects_invalid_csrf_without_mutation(
    tmp_path, csrf_headers, csrf_cookie
):
    approver_secret = "b" * 40
    app = create_app(
        db_path=tmp_path / "killswitch_csrf_api.sqlite",
        approver_secret=approver_secret,
    )
    client = TestClient(app)
    store = app.state.approval_store
    state_before = store.read_kill_switch()
    events_before = store.list_events()
    headers = {"Authorization": f"Bearer {approver_secret}", **csrf_headers}
    if csrf_cookie is not None:
        headers["Cookie"] = f"csrf_token={csrf_cookie}"

    response = client.post(
        "/killswitch",
        json={"enabled": True, "reason_code": "KILL_SWITCH_ENABLED"},
        headers=headers,
    )

    assert response.status_code == 403
    assert store.read_kill_switch() == state_before
    assert store.list_events() == events_before


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="missing"),
        pytest.param({"request_fingerprint": None}, id="null"),
        pytest.param({"request_fingerprint": ""}, id="empty"),
        pytest.param({"request_fingerprint": "   "}, id="whitespace-only"),
        pytest.param({"request_fingerprint": "g" * 64}, id="malformed"),
        pytest.param({"request_fingerprint": "0" * 64}, id="incorrect"),
    ],
)
def test_approval_api_rejects_invalid_request_fingerprint_without_mutation(
    tmp_path, payload
):
    approver_secret = "f" * 40
    app = create_app(
        db_path=tmp_path / "approval_fingerprint.sqlite",
        approver_secret=approver_secret,
    )
    client = TestClient(app)
    store = app.state.approval_store
    record = store.create_approval(
        intent=make_intent(),
        context=AuthenticatedScope(principal_id="principal_001", scope_id="scope_001"),
    )
    events_before = store.list_events()

    response = client.post(
        f"/approvals/{record.approval_id}/approve",
        json=payload,
        headers={
            "Authorization": f"Bearer {approver_secret}",
            "X-CSRF-Token": "csrf-token",
            "Cookie": "csrf_token=csrf-token",
        },
    )

    assert response.status_code == 409
    assert store.get_approval(record.approval_id).status == "PENDING"
    assert store.list_events() == events_before
    assert all(event.action != "approval_approved" for event in store.list_events())
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM authorizations WHERE operation_id=?",
            (record.operation_id,),
        ).fetchone()[0] == 0


def test_approval_api_accepts_matching_request_fingerprint(tmp_path):
    approver_secret = "g" * 40
    app = create_app(
        db_path=tmp_path / "approval_fingerprint_valid.sqlite",
        approver_secret=approver_secret,
    )
    client = TestClient(app)
    store = app.state.approval_store
    record = store.create_approval(
        intent=make_intent(),
        context=AuthenticatedScope(principal_id="principal_001", scope_id="scope_001"),
    )

    response = client.post(
        f"/approvals/{record.approval_id}/approve",
        json={"request_fingerprint": record.request_fingerprint},
        headers={
            "Authorization": f"Bearer {approver_secret}",
            "X-CSRF-Token": "csrf-token",
            "Cookie": "csrf_token=csrf-token",
        },
    )

    assert response.status_code == 200
    assert store.get_approval(record.approval_id).status == "APPROVED"
    assert [event.action for event in store.list_events(approval_id=record.approval_id)].count(
        "approval_approved"
    ) == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM authorizations WHERE operation_id=?",
            (record.operation_id,),
        ).fetchone()[0] == 1


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
