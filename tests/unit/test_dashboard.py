from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from agentsentinel.approval_service import create_app
from agentsentinel.observability.payment_audit import PaymentAuditChain
from agentsentinel.observability.payment_events import build_payment_event
from agentsentinel.payment_policy import AuthenticatedScope
from agentsentinel.schema.payment import Money, PaymentIntent

APPROVER_SECRET = "approver-secret-for-dashboard-tests-0001"
VIEWER_SECRET = "viewer-secret-for-dashboard-tests-0001"
NOW = datetime.now(timezone.utc)


def make_app(tmp_path: Path):
    return create_app(
        db_path=tmp_path / "application.sqlite",
        telemetry_db_path=tmp_path / "telemetry.sqlite",
        approver_secret=APPROVER_SECRET,
        viewer_secret=VIEWER_SECRET,
    )


def sign_in(client: TestClient, credential: str = APPROVER_SECRET) -> str:
    response = client.get("/dashboard/login")
    assert response.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert csrf is not None
    token = csrf.group(1)
    response = client.post(
        "/dashboard/login",
        data={"csrf_token": token, "credential": credential},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.cookies.get("agentsentinel_session")
    return client.cookies.get("csrf_token") or token


def append_trace(
    path: Path,
    trace_id: str,
    operation_id: str,
    events: tuple[tuple[str, str | None, tuple[str, ...]], ...],
) -> None:
    chain = PaymentAuditChain(path)
    for index, (event_type, decision, reasons) in enumerate(events):
        chain.append(
            build_payment_event(
                event_type=event_type,
                trace_id=trace_id,
                operation_id=operation_id,
                payment_intent_id=f"intent_{operation_id}",
                action="create_order",
                gateway_decision=decision,
                reason_codes=reasons,
                timestamp=NOW + timedelta(milliseconds=index),
                actor_role="application",
            )
        )


def make_intent(operation_id: str, *, expires_at: datetime | None = None) -> PaymentIntent:
    return PaymentIntent(
        intent_id=f"intent_{operation_id}",
        operation_id=operation_id,
        trace_id=f"trace_{operation_id}",
        scope_id="dashboard_scope",
        money=Money(amount="4.00", currency="USD"),
        merchant_id="MERCHANT001",
        purpose="Dashboard test",
        action="create_order",
        requested_at=NOW,
    )


def test_unauthenticated_dashboard_access_is_rejected(tmp_path):
    client = TestClient(make_app(tmp_path))

    assert client.get("/dashboard").status_code == 401
    assert client.get("/dashboard/traces").status_code == 401
    assert client.get("/approvals").status_code == 401


def test_authenticated_read_only_overview_shows_sources_and_ranges(tmp_path):
    client = TestClient(make_app(tmp_path))
    sign_in(client)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "AgentSentinel" in response.text
    assert "Payment operations" in response.text
    assert "operations table" in response.text
    assert "Last 24 hours" in response.text
    assert "Persisted operational data only" in response.text


def test_overview_counts_are_derived_from_persisted_sources(tmp_path):
    telemetry = tmp_path / "telemetry.sqlite"
    append_trace(
        telemetry,
        "trace_allowed",
        "operation_allowed",
        (("POLICY_EVALUATED", "ALLOW", ()), ("PAYMENT_COMPLETED", "ALLOW", ())),
    )
    append_trace(
        telemetry,
        "trace_blocked",
        "operation_blocked",
        (("POLICY_EVALUATED", "BLOCK", ("AUTHORIZATION_MISSING",)),),
    )
    append_trace(
        telemetry,
        "trace_review",
        "operation_review",
        (("POLICY_EVALUATED", "REVIEW", ("REVIEW_AMOUNT_THRESHOLD",)),),
    )
    append_trace(
        telemetry,
        "trace_uncertain",
        "operation_uncertain",
        (("PAYMENT_UNCERTAIN", "ALLOW", ("PROVIDER_OUTCOME_UNCERTAIN",)),),
    )
    client = TestClient(make_app(tmp_path))
    with sqlite3.connect(tmp_path / "application.sqlite") as conn:
        conn.execute("CREATE TABLE operations (operation_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO operations VALUES(?)", [("operation_1",), ("operation_2",)])
    sign_in(client)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "<strong>2</strong>" in response.text
    assert "ALLOW: 1" in response.text
    assert "BLOCK: 1" in response.text
    assert "REVIEW: 1" in response.text
    assert "Completed outcomes" in response.text
    assert "Uncertain outcomes" in response.text


def test_empty_database_uses_no_data_without_inventing_counts(tmp_path):
    client = TestClient(make_app(tmp_path))
    sign_in(client)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "No data" in response.text
    assert "No chain file" in response.text
    assert "No persisted state (gateway default is disabled)" in response.text


def test_trace_pagination_filter_and_bounded_page_size(tmp_path):
    telemetry = tmp_path / "telemetry.sqlite"
    for index in range(3):
        append_trace(
            telemetry,
            f"trace_page_{index}",
            f"operation_page_{index}",
            (("POLICY_EVALUATED", "BLOCK", ("AUTHORIZATION_MISSING",)),),
        )
    client = TestClient(make_app(tmp_path))
    sign_in(client)

    first = client.get("/dashboard/traces?page_size=2&page=1&days=30")
    second = client.get("/dashboard/traces?page_size=2&page=2&days=30")
    filtered = client.get("/dashboard/traces?q=operation_page_2&days=30")
    too_large = client.get("/dashboard/traces?page_size=1000")

    assert first.status_code == second.status_code == filtered.status_code == 200
    assert "3 matching traces" in first.text
    assert "trace_page_0" in first.text and "trace_page_1" in first.text
    assert "trace_page_2" not in first.text
    assert "trace_page_2" in second.text
    assert "trace_page_2" in filtered.text
    assert too_large.status_code == 400


def test_trace_timeline_shows_correlated_ordered_spans(tmp_path):
    telemetry = tmp_path / "telemetry.sqlite"
    append_trace(
        telemetry,
        "trace_timeline",
        "operation_timeline",
        (
            ("PAYMENT_PROPOSED", None, ()),
            ("POLICY_EVALUATED", "BLOCK", ("AUTHORIZATION_AMOUNT_MISMATCH",)),
            ("PAYMENT_BLOCKED", "BLOCK", ("AUTHORIZATION_AMOUNT_MISMATCH",)),
        ),
    )
    client = TestClient(make_app(tmp_path))
    sign_in(client)

    response = client.get("/dashboard/traces/trace_timeline")

    assert response.status_code == 200
    assert response.text.index("PAYMENT_PROPOSED") < response.text.index("POLICY_EVALUATED")
    assert response.text.index("POLICY_EVALUATED") < response.text.index("PAYMENT_BLOCKED")
    assert "parent span" in response.text


def test_search_string_is_escaped_in_html(tmp_path):
    client = TestClient(make_app(tmp_path))
    sign_in(client)
    payload = "<script>alert(1)</script>"

    response = client.get(f"/dashboard/traces?q={quote(payload)}")

    assert response.status_code == 200
    assert payload not in response.text
    assert "&lt;script&gt;" in response.text


def test_dashboard_never_renders_authentication_secret(tmp_path):
    client = TestClient(make_app(tmp_path))
    sign_in(client)

    response = client.get("/dashboard")

    assert APPROVER_SECRET not in response.text
    assert VIEWER_SECRET not in response.text
    assert client.cookies.get("agentsentinel_session") not in response.text


def test_viewer_role_cannot_mutate_approval_or_kill_switch(tmp_path):
    client = TestClient(make_app(tmp_path))
    csrf = sign_in(client, VIEWER_SECRET)

    response = client.post(
        "/dashboard/killswitch",
        data={"csrf_token": csrf, "enabled": "true", "confirm": "yes"},
    )

    assert response.status_code == 403
    assert client.app.state.approval_store.read_kill_switch().enabled is False


def test_browser_state_change_requires_csrf(tmp_path):
    client = TestClient(make_app(tmp_path))
    sign_in(client)

    response = client.post(
        "/dashboard/killswitch",
        data={"enabled": "true", "confirm": "yes"},
    )

    assert response.status_code == 403
    assert client.app.state.approval_store.read_kill_switch().enabled is False


def test_approval_action_rechecks_fingerprint_and_expiry(tmp_path):
    client = TestClient(make_app(tmp_path))
    csrf = sign_in(client)
    store = client.app.state.approval_store
    context = AuthenticatedScope(principal_id="dashboard_user", scope_id="dashboard_scope")
    mismatch = store.create_approval(intent=make_intent("operation_fp"), context=context)
    expired = store.create_approval(
        intent=make_intent("operation_expired"),
        context=context,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    bad_fingerprint = client.post(
        f"/dashboard/approvals/{mismatch.approval_id}/approve",
        data={"csrf_token": csrf, "request_fingerprint": "0" * 64, "confirm": "yes"},
    )
    stale = client.post(
        f"/dashboard/approvals/{expired.approval_id}/approve",
        data={
            "csrf_token": csrf,
            "request_fingerprint": expired.request_fingerprint,
            "confirm": "yes",
        },
    )

    assert bad_fingerprint.status_code == 409
    assert stale.status_code == 409
    assert store.get_approval(mismatch.approval_id).status == "PENDING"
    assert store.get_approval(expired.approval_id).status == "EXPIRED"


def test_approver_action_uses_existing_atomic_approval_store(tmp_path):
    client = TestClient(make_app(tmp_path))
    csrf = sign_in(client)
    store = client.app.state.approval_store
    context = AuthenticatedScope(principal_id="dashboard_user", scope_id="dashboard_scope")
    request = store.create_approval(intent=make_intent("operation_approved"), context=context)

    response = client.post(
        f"/dashboard/approvals/{request.approval_id}/approve",
        data={
            "csrf_token": csrf,
            "request_fingerprint": request.request_fingerprint,
            "confirm": "yes",
        },
    )

    assert response.status_code == 200
    assert store.get_approval(request.approval_id).status == "APPROVED"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM authorizations WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone()[0] == 1


def test_kill_switch_control_requires_confirmation_and_audits_transition(tmp_path):
    client = TestClient(make_app(tmp_path))
    csrf = sign_in(client)

    not_confirmed = client.post(
        "/dashboard/killswitch",
        data={"csrf_token": csrf, "enabled": "true"},
    )
    enabled = client.post(
        "/dashboard/killswitch",
        data={"csrf_token": csrf, "enabled": "true", "confirm": "yes"},
    )

    assert not_confirmed.status_code == 400
    assert enabled.status_code == 200
    assert "ENABLED" in enabled.text
    assert client.app.state.approval_store.is_kill_switch_active()
    events = client.app.state.approval_store.list_events()
    assert any(event.action == "kill_switch_enabled" and event.actor_role == "approver" for event in events)


def test_audit_success_and_tampering_are_reported_as_live(tmp_path):
    telemetry = tmp_path / "telemetry.sqlite"
    append_trace(
        telemetry,
        "trace_audit",
        "operation_audit",
        (("PAYMENT_PROPOSED", None, ()),),
    )
    client = TestClient(make_app(tmp_path))
    csrf = sign_in(client)

    valid = client.get("/dashboard")
    assert "VALID" in valid.text
    assert "Live verification; result not persisted" in valid.text
    with sqlite3.connect(telemetry) as conn:
        conn.execute("UPDATE payment_audit_chain SET event_type='PAYMENT_BLOCKED' WHERE sequence_number=1")
    tampered = client.get("/dashboard")

    assert tampered.status_code == 200
    assert "INVALID" in tampered.text
    assert "First broken sequence: 1" in tampered.text
    verify = client.post(
        "/dashboard/audit/verify",
        data={"csrf_token": csrf, "confirm": "yes"},
    )
    assert verify.status_code == 200
    assert "Live verification; result not persisted" in verify.text


def test_demo_is_separate_and_does_not_issue_authorizations_or_call_paypal(tmp_path, monkeypatch):
    from agentsentinel.payment_gateway import PaymentGateway

    def forbidden_submit(*args, **kwargs):
        raise AssertionError("offline dashboard demo must not execute a payment")

    monkeypatch.setattr(PaymentGateway, "submit", forbidden_submit)
    client = TestClient(make_app(tmp_path))
    sign_in(client)
    db_path = tmp_path / "application.sqlite"
    with sqlite3.connect(db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0]

    response = client.get("/dashboard/demo")

    with sqlite3.connect(db_path) as conn:
        after = conn.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0]
    assert response.status_code == 200
    assert "OFFLINE DEMO MODE" in response.text
    assert "Merchant mismatch blocked" in response.text
    assert "Amount mismatch blocked" in response.text
    assert "Human review required" in response.text
    assert "Kill switch enabled then action blocked" in response.text
    assert "Uncertain provider outcome" in response.text
    assert "Advisory anomaly" in response.text
    assert "payment-iforest-synthetic-v1" in response.text
    assert "VALID" in response.text
    assert "no authorization created" in response.text
    assert before == after == 0


def test_read_only_audit_verification_does_not_initialize_missing_database(tmp_path):
    missing = tmp_path / "telemetry.sqlite"
    client = TestClient(make_app(tmp_path))
    sign_in(client)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert not missing.exists()
    assert "No chain file" in response.text


def test_dashboard_stylesheet_is_static_and_safe(tmp_path):
    client = TestClient(make_app(tmp_path))

    response = client.get("/dashboard/static/dashboard.css")

    assert response.status_code == 200
    assert "color-scheme: dark" in response.text
    assert client.get("/dashboard/static/dashboard_login.html").status_code == 404


def test_alerts_distinguish_deterministic_blocks_and_advisory_signals(tmp_path):
    telemetry = tmp_path / "telemetry.sqlite"
    append_trace(
        telemetry,
        "trace_alert_block",
        "operation_alert_block",
        (("POLICY_EVALUATED", "BLOCK", ("AUTHORIZATION_MERCHANT_MISMATCH",)),),
    )
    chain = PaymentAuditChain(telemetry)
    for index in range(6):
        chain.append(
            build_payment_event(
                event_type="PAYMENT_DISPATCHED",
                trace_id="trace_retry_alert",
                operation_id=f"operation_retry_{index}",
                timestamp=NOW + timedelta(seconds=index),
                action="create_order",
            )
        )
    chain.append(
        build_payment_event(
            event_type="PAYMENT_PROPOSED",
            trace_id="trace_iforest_alert",
            operation_id="operation_iforest_alert",
            timestamp=NOW,
            action="create_order",
            anomaly_score=0.99,
            detector_version="payment-iforest-synthetic-v1",
            reason_codes=("ISOLATION_FOREST_THRESHOLD",),
        )
    )
    client = TestClient(make_app(tmp_path))
    sign_in(client)

    response = client.get("/dashboard/alerts")

    assert response.status_code == 200
    assert "Deterministic enforcement" in response.text
    assert "Policy BLOCK" in response.text
    assert "payment-rules-v1" in response.text
    assert "payment-iforest-synthetic-v1" in response.text
    assert "not confirmed fraud" in response.text
    assert "AUTHORIZATION_MERCHANT_MISMATCH" in response.text


def test_dashboard_login_rejects_invalid_credentials_without_reflection(tmp_path):
    client = TestClient(make_app(tmp_path))
    response = client.get("/dashboard/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
    credential = "<script>not-a-secret</script>"

    rejected = client.post(
        "/dashboard/login",
        data={"csrf_token": csrf, "credential": credential},
    )

    assert rejected.status_code == 401
    assert credential not in rejected.text
    assert client.get("/dashboard").status_code == 401
