from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from scripts import run_demo_dashboard
from scripts import demo_support
from scripts.run_demo_dashboard import (
    DEMO_DATA_PARENT,
    DEMO_MARKER,
    DEMO_MARKER_CONTENT,
    build_parser,
    main,
    seed_demo_data,
)
from agentsentinel.approval_service import ApprovalStore, create_app
from agentsentinel.observability.collector import (
    PaymentTelemetryCapture,
    TraceCollector,
)
from agentsentinel.observability.payment_audit import PaymentAuditChain
from agentsentinel.payment_gateway import TrustedGatewayEntryPoint

TEST_APPROVER_SECRET = "seeded-dashboard-test-operator-secret"


def test_dashboard_and_tests_share_canonical_spy_transport():
    assert not run_demo_dashboard._demo_seeding_enabled()
    run_demo_dashboard._load_demo_dependencies()

    assert run_demo_dashboard.SpyTransport is demo_support.SpyTransport
    assert not run_demo_dashboard._demo_seeding_enabled()


def _configure_demo_parent(tmp_path: Path, monkeypatch) -> Path:
    parent = tmp_path / "demo-data"
    parent.mkdir()
    monkeypatch.setattr(run_demo_dashboard, "DEMO_DATA_PARENT", parent)
    monkeypatch.setattr(run_demo_dashboard, "_demo_seeding_enabled", lambda: True)
    run_demo_dashboard._load_demo_dependencies()
    return parent


def _client_for_demo(state) -> TestClient:
    app = create_app(
        db_path=state.database_path,
        approver_secret=TEST_APPROVER_SECRET,
        viewer_secret="demo-viewer-disabled",
        telemetry_db_path=state.telemetry_path,
        failures_path=state.failures_path,
        demo_mode=True,
    )
    return TestClient(app)


def _sign_in(client: TestClient) -> str:
    login_page = client.get("/dashboard/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert token is not None
    response = client.post(
        "/dashboard/login",
        data={
            "csrf_token": token.group(1),
            "credential": TEST_APPROVER_SECRET,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client.cookies.get("csrf_token") or token.group(1)


def test_seeded_dashboard_scenarios_are_visible_and_use_safe_adapters(
    tmp_path, caplog, monkeypatch
):
    _configure_demo_parent(tmp_path, monkeypatch)
    state = seed_demo_data()
    expected = {
        ("ALLOW - completed operation", "ALLOW", "COMPLETED"),
        ("REVIEW - pending human approval", "REVIEW", "PENDING"),
        ("BLOCK - amount mismatch", "BLOCK", "REJECTED"),
        ("BLOCK - merchant mismatch", "BLOCK", "REJECTED"),
        ("UNCERTAIN - simulated ambiguous outcome", "ALLOW", "UNCERTAIN"),
        (
            "Kill switch - blocked while ON; final state OFF",
            "BLOCK",
            "REJECTED",
        ),
        (
            "Advisory alert - existing rules and Isolation Forest",
            "ADVISORY",
            "NOT_AUTHORIZATION",
        ),
    }
    assert {
        (scenario.name, scenario.decision, scenario.status)
        for scenario in state.scenarios
    } == expected
    assert state.mock_adapter.calls == 1
    assert type(state.spy_transport) is run_demo_dashboard.SpyTransport
    assert [path for _, path, _ in state.spy_transport.calls] == [
        "/v1/oauth2/token",
        "/v2/checkout/orders",
    ]
    assert state.database_path != (
        Path(__file__).resolve().parents[2]
        / "data"
        / "paypal_sandbox"
        / "gateway_demo.sqlite"
    )
    assert PaymentAuditChain.open_readonly(state.telemetry_path).verify().valid
    with PaymentAuditChain.open_readonly(state.telemetry_path).verified_snapshot() as (
        connection,
        verification,
    ):
        assert verification.valid
        policy_events = connection.execute(
            "SELECT event_type, canonical_event FROM payment_audit_chain "
            "WHERE event_type='POLICY_EVALUATED'"
        ).fetchall()
    observed_policy_events = {
        (
            event["operation_id"],
            event["trace_id"],
        )
        for row in policy_events
        for event in (json.loads(row["canonical_event"]),)
        if row["event_type"] == event["event_type"] == "POLICY_EVALUATED"
    }
    assert {
        ("demo_op_allow", "demo_trace_allow"),
        ("demo_op_review", "demo_trace_review"),
        ("demo_op_amount_mismatch", "demo_trace_amount_mismatch"),
        ("demo_op_merchant_mismatch", "demo_trace_merchant_mismatch"),
        ("demo_op_uncertain", "demo_trace_uncertain"),
        ("demo_op_kill_switch", "demo_trace_kill_switch"),
    } <= observed_policy_events

    client = _client_for_demo(state)
    assert "LOCAL MOCK DEMONSTRATION" in client.get("/dashboard/login").text
    csrf = _sign_in(client)
    overview = client.get("/dashboard")
    approvals_page = client.get("/dashboard/approvals")
    traces_page = client.get("/dashboard/traces")
    alerts_page = client.get("/dashboard/alerts")
    synthetic_demo_page = client.get("/dashboard/demo")

    assert overview.status_code == approvals_page.status_code == 200
    assert traces_page.status_code == alerts_page.status_code == 200
    assert synthetic_demo_page.status_code == 200
    assert "LOCAL MOCK DEMONSTRATION" in overview.text
    assert "VALID" in overview.text
    assert "LOCAL MOCK DEMONSTRATION" in approvals_page.text
    assert "REVIEW_AMOUNT_THRESHOLD" in approvals_page.text
    assert "LOCAL MOCK DEMONSTRATION" in traces_page.text
    for trace_id in (
        "demo_trace_allow",
        "demo_trace_review",
        "demo_trace_amount_mismatch",
        "demo_trace_merchant_mismatch",
        "demo_trace_uncertain",
        "demo_trace_kill_switch",
        "pay_trace_0075",
    ):
        assert trace_id in traces_page.text
    assert "LOCAL MOCK DEMONSTRATION" in alerts_page.text
    assert "AUTHORIZATION_AMOUNT_MISMATCH" in alerts_page.text
    assert "AUTHORIZATION_MERCHANT_MISMATCH" in alerts_page.text
    assert "Uncertain payment outcome" in alerts_page.text
    assert "PAYMENT_RETRY_STORM" in alerts_page.text
    assert "payment-iforest-synthetic-v1" in alerts_page.text
    assert "OFFLINE DEMO" in synthetic_demo_page.text
    assert "separate from persisted overview metrics" in synthetic_demo_page.text
    assert "LOCAL MOCK DEMONSTRATION" in synthetic_demo_page.text

    for trace_id in (
        "demo_trace_allow",
        "demo_trace_review",
        "demo_trace_amount_mismatch",
        "demo_trace_merchant_mismatch",
        "demo_trace_uncertain",
        "demo_trace_kill_switch",
        "pay_trace_0075",
    ):
        timeline = client.get(f"/dashboard/traces/{trace_id}")
        assert timeline.status_code == 200
        assert "Chronological event and span timeline" in timeline.text

    store = ApprovalStore(state.database_path)
    pending = store.get_approval(state.pending_approval_id)
    assert pending is not None and pending.status == "PENDING"
    approve_response = client.post(
        f"/dashboard/approvals/{pending.approval_id}/approve",
        data={
            "csrf_token": csrf,
            "request_fingerprint": pending.request_fingerprint,
            "confirm": "yes",
        },
        follow_redirects=False,
    )
    assert approve_response.status_code == 303
    assert store.get_approval(pending.approval_id).status == "APPROVED"
    with sqlite3.connect(state.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM authorizations WHERE approval_id=?",
            (pending.approval_id,),
        ).fetchone()[0] == 1

    verified = client.post(
        "/dashboard/audit/verify",
        data={"csrf_token": csrf, "confirm": "yes"},
    )
    assert verified.status_code == 200
    assert "VALID" in verified.text
    assert PaymentAuditChain.open_readonly(state.telemetry_path).verify().valid
    assert store.read_kill_switch().enabled is False
    assert TEST_APPROVER_SECRET not in caplog.text
    for path in (state.database_path, state.telemetry_path):
        contents = path.read_bytes()
        assert b"mock_demo_client" not in contents
        assert b"mock_demo_secret" not in contents
        assert TEST_APPROVER_SECRET.encode("ascii") not in contents


def test_demo_runs_use_fresh_directories_and_reset_never_deletes_data(
    tmp_path, monkeypatch
):
    parent = _configure_demo_parent(tmp_path, monkeypatch)
    first = seed_demo_data()
    first_database = first.database_path.read_bytes()
    second = seed_demo_data()
    assert first.root.parent == second.root.parent == parent
    assert first.root != second.root
    assert first.root.name.startswith("run-")
    assert second.root.name.startswith("run-")
    assert first.database_path.read_bytes() == first_database
    assert PaymentAuditChain.open_readonly(second.telemetry_path).verify().valid

    preserved = parent / "user-data.txt"
    preserved.write_text("preserve", encoding="utf-8")
    with pytest.raises(RuntimeError, match="--reset is unsupported"):
        seed_demo_data(reset=True)
    assert preserved.read_text(encoding="utf-8") == "preserve"
    assert first.database_path.read_bytes() == first_database
    assert "--reset" in build_parser().format_help()


def test_demo_cli_reset_fails_without_touching_existing_data(
    tmp_path, monkeypatch, capsys
):
    parent = _configure_demo_parent(tmp_path, monkeypatch)
    existing = parent / "old-run"
    existing.mkdir()
    protected = existing / "keep.txt"
    protected.write_text("preserve", encoding="utf-8")
    protected_database = existing / "existing.sqlite"
    protected_database.write_bytes(b"existing database bytes")
    assert main(["--reset"]) == 2
    assert capsys.readouterr().err.find("never delete data") >= 0
    assert protected.read_text(encoding="utf-8") == "preserve"
    assert protected_database.read_bytes() == b"existing database bytes"


def test_demo_parent_must_be_directory(tmp_path, monkeypatch):
    parent = _configure_demo_parent(tmp_path, monkeypatch)
    parent.rmdir()
    parent.write_text("preserve", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not a directory"):
        seed_demo_data()


def test_demo_parent_must_be_operator_provisioned(tmp_path, monkeypatch):
    parent = tmp_path / "not-created-by-runner"
    monkeypatch.setattr(run_demo_dashboard, "DEMO_DATA_PARENT", parent)
    monkeypatch.setattr(run_demo_dashboard, "_demo_seeding_enabled", lambda: True)
    with pytest.raises(RuntimeError, match="must already exist"):
        seed_demo_data()
    assert not parent.exists()


def test_demo_parent_configuration_is_required(tmp_path, monkeypatch):
    monkeypatch.setattr(run_demo_dashboard, "DEMO_DATA_PARENT", None)
    monkeypatch.setattr(run_demo_dashboard, "_demo_seeding_enabled", lambda: True)
    with pytest.raises(RuntimeError, match="AGENTSENTINEL_DEMO_DATA_PARENT"):
        seed_demo_data()
    assert not (tmp_path / "data").exists()


def test_demo_parent_must_be_absolute(tmp_path, monkeypatch):
    monkeypatch.setattr(run_demo_dashboard, "DEMO_DATA_PARENT", Path("relative-demo"))
    monkeypatch.setattr(run_demo_dashboard, "_demo_seeding_enabled", lambda: True)
    with pytest.raises(RuntimeError, match="absolute path"):
        seed_demo_data()


def test_demo_unique_name_collision_does_not_reuse_existing_directory(
    tmp_path, monkeypatch
):
    parent = _configure_demo_parent(tmp_path, monkeypatch)
    existing = parent / "run-fixed"
    existing.mkdir()
    protected = existing / "keep.txt"
    protected.write_text("preserve", encoding="utf-8")
    names = iter(["fixed", "fresh"])
    monkeypatch.setattr(
        run_demo_dashboard, "uuid4", lambda: SimpleNamespace(hex=next(names))
    )
    state = seed_demo_data()
    assert state.root == parent / "run-fresh"
    assert protected.read_text(encoding="utf-8") == "preserve"


def _create_windows_junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows junction test requires Windows")
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Windows junction creation is unavailable in this environment")


@pytest.mark.parametrize("location", ["parent", "ancestor"])
def test_demo_directory_creation_rejects_junctions(tmp_path, monkeypatch, location):
    physical_parent = tmp_path / "physical-parent"
    physical_parent.mkdir()
    target_file = physical_parent / "preserve.txt"
    target_file.write_text("preserve", encoding="utf-8")
    linked = tmp_path / "linked"
    _create_windows_junction(linked, physical_parent)
    parent = linked if location == "parent" else linked / "demo-data"
    monkeypatch.setattr(run_demo_dashboard, "DEMO_DATA_PARENT", parent)
    monkeypatch.setattr(run_demo_dashboard, "_demo_seeding_enabled", lambda: True)
    with pytest.raises(RuntimeError, match="reparse point"):
        seed_demo_data()
    assert target_file.read_text(encoding="utf-8") == "preserve"
    assert not (physical_parent / "demo-data").exists()
    os.rmdir(linked)


def test_demo_parent_rejects_symlink_on_any_platform(tmp_path, monkeypatch):
    parent = _configure_demo_parent(tmp_path, monkeypatch)
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "demo-data"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    monkeypatch.setattr(run_demo_dashboard, "DEMO_DATA_PARENT", link)
    monkeypatch.setattr(run_demo_dashboard, "_demo_seeding_enabled", lambda: True)
    with pytest.raises(RuntimeError, match="reparse point"):
        seed_demo_data()
    assert not list(target.iterdir())
    link.unlink()


def test_demo_reset_argument_fails_closed_before_seeding(tmp_path, monkeypatch):
    parent = _configure_demo_parent(tmp_path, monkeypatch)
    before = sorted(path.name for path in parent.iterdir())
    with pytest.raises(RuntimeError, match="--reset is unsupported"):
        run_demo_dashboard._prepare_demo_directory(parent, reset=True)
    assert sorted(path.name for path in parent.iterdir()) == before


def test_reset_cli_does_not_import_or_initialize_approval_database(tmp_path):
    approval_db = tmp_path / "approval.sqlite"
    environment = os.environ.copy()
    environment["APPROVAL_DB_PATH"] = str(approval_db)
    result = subprocess.run(
        [
            sys.executable,
            str(run_demo_dashboard.ROOT / "scripts" / "run_demo_dashboard.py"),
            "--reset",
        ],
        cwd=run_demo_dashboard.ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--reset is unsupported" in result.stderr
    assert not approval_db.exists()


def test_default_invocation_fails_before_touching_configured_data(tmp_path):
    parent = tmp_path / "operator-demo-data"
    parent.mkdir()
    existing_file = parent / "keep.txt"
    existing_file.write_bytes(b"preserve")
    existing_db = parent / "existing.sqlite"
    existing_db.write_bytes(b"existing database bytes")
    approval_db = tmp_path / "application-approval.sqlite"
    environment = os.environ.copy()
    environment["AGENTSENTINEL_DEMO_DATA_PARENT"] = str(parent)
    environment["APPROVAL_DB_PATH"] = str(approval_db)
    result = subprocess.run(
        [
            sys.executable,
            str(run_demo_dashboard.ROOT / "scripts" / "run_demo_dashboard.py"),
        ],
        cwd=run_demo_dashboard.ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "Demo seeding is disabled" in result.stderr
    assert "Temporary local operator credential" not in result.stdout
    assert sorted(path.name for path in parent.iterdir()) == [
        "existing.sqlite",
        "keep.txt",
    ]
    assert existing_file.read_bytes() == b"preserve"
    assert existing_db.read_bytes() == b"existing database bytes"
    assert not approval_db.exists()
    assert not (parent / "run-*").exists()


def test_seed_function_is_disabled_before_import_or_filesystem_access(tmp_path, monkeypatch):
    parent = tmp_path / "untrusted-demo-data"
    monkeypatch.setattr(run_demo_dashboard, "DEMO_DATA_PARENT", parent)
    monkeypatch.setattr(run_demo_dashboard, "_DEMO_DEPENDENCIES_LOADED", False)
    with pytest.raises(RuntimeError, match="demo seeding is disabled"):
        seed_demo_data()
    assert not parent.exists()
    assert not run_demo_dashboard._DEMO_DEPENDENCIES_LOADED


def test_demo_seeding_requires_expected_gateway_policy_events(
    tmp_path, monkeypatch
):
    _configure_demo_parent(tmp_path, monkeypatch)
    original_publish = run_demo_dashboard.PaymentTelemetryPublisher.publish

    def omit_policy_event(publisher, event):
        if event.event_type == "POLICY_EVALUATED":
            return True
        return original_publish(publisher, event)

    monkeypatch.setattr(
        run_demo_dashboard.PaymentTelemetryPublisher, "publish", omit_policy_event
    )
    with pytest.raises(RuntimeError, match="policy telemetry is incomplete"):
        seed_demo_data()


def test_demo_seeding_fails_when_gateway_policy_telemetry_publication_fails(
    tmp_path, monkeypatch
):
    _configure_demo_parent(tmp_path, monkeypatch)
    original_capture = TraceCollector.capture_payment_event

    def reject_policy_event(collector, raw_event):
        event_type = (
            raw_event.event_type
            if hasattr(raw_event, "event_type")
            else raw_event.get("event_type")
        )
        trace_id = (
            raw_event.trace_id
            if hasattr(raw_event, "trace_id")
            else raw_event.get("trace_id")
        )
        if event_type == "POLICY_EVALUATED" and trace_id.startswith("demo_trace_"):
            return PaymentTelemetryCapture("unavailable", "TEST_FAILURE")
        return original_capture(collector, raw_event)

    monkeypatch.setattr(TraceCollector, "capture_payment_event", reject_policy_event)
    with pytest.raises(RuntimeError, match="telemetry publication failed"):
        seed_demo_data()


def _persisted_demo_kill_switch(parent: Path) -> bool:
    run_directory = next(parent.glob("run-*"))
    return ApprovalStore(run_directory / "payments.sqlite").read_kill_switch().enabled


def test_kill_switch_restored_after_enable_raises(tmp_path, monkeypatch):
    parent = _configure_demo_parent(tmp_path, monkeypatch)
    original_set = ApprovalStore.set_kill_switch

    def enable_then_raise(store, *, enabled, actor_role):
        result = original_set(store, enabled=enabled, actor_role=actor_role)
        if enabled:
            raise RuntimeError("simulated enable failure")
        return result

    monkeypatch.setattr(ApprovalStore, "set_kill_switch", enable_then_raise)
    with pytest.raises(RuntimeError, match="simulated enable failure"):
        seed_demo_data()
    assert _persisted_demo_kill_switch(parent) is False


def test_kill_switch_restored_after_gateway_submission_failure(
    tmp_path, monkeypatch
):
    parent = _configure_demo_parent(tmp_path, monkeypatch)
    original_submit = TrustedGatewayEntryPoint.submit

    def fail_kill_switch_submission(entrypoint, intent, **kwargs):
        if intent.trace_id == "demo_trace_kill_switch":
            raise RuntimeError("simulated submission failure")
        return original_submit(entrypoint, intent, **kwargs)

    monkeypatch.setattr(
        TrustedGatewayEntryPoint, "submit", fail_kill_switch_submission
    )
    with pytest.raises(RuntimeError, match="simulated submission failure"):
        seed_demo_data()

    assert _persisted_demo_kill_switch(parent) is False


def test_kill_switch_cleanup_failure_preserves_submission_exception(
    tmp_path, monkeypatch
):
    parent = _configure_demo_parent(tmp_path, monkeypatch)
    original_submit = TrustedGatewayEntryPoint.submit
    original_set = ApprovalStore.set_kill_switch

    def fail_submission(entrypoint, intent, **kwargs):
        if intent.trace_id == "demo_trace_kill_switch":
            raise RuntimeError("original submission failure")
        return original_submit(entrypoint, intent, **kwargs)

    def fail_cleanup(store, *, enabled, actor_role):
        result = original_set(store, enabled=enabled, actor_role=actor_role)
        if not enabled:
            raise RuntimeError("cleanup failure")
        return result

    monkeypatch.setattr(TrustedGatewayEntryPoint, "submit", fail_submission)
    monkeypatch.setattr(ApprovalStore, "set_kill_switch", fail_cleanup)
    with pytest.raises(RuntimeError, match="original submission failure"):
        seed_demo_data()
    assert _persisted_demo_kill_switch(parent) is False


def test_kill_switch_cleanup_failure_propagates_after_success(
    tmp_path, monkeypatch
):
    parent = _configure_demo_parent(tmp_path, monkeypatch)
    original_set = ApprovalStore.set_kill_switch

    def fail_cleanup(store, *, enabled, actor_role):
        result = original_set(store, enabled=enabled, actor_role=actor_role)
        if not enabled:
            raise RuntimeError("cleanup failure")
        return result

    monkeypatch.setattr(ApprovalStore, "set_kill_switch", fail_cleanup)
    with pytest.raises(RuntimeError, match="cleanup failure"):
        seed_demo_data()
    assert _persisted_demo_kill_switch(parent) is False


def test_demo_cli_does_not_display_credentials_without_a_terminal(capsys):
    assert main([]) == 2
    output = capsys.readouterr()
    assert "Demo seeding is disabled" in output.err
    assert "Temporary local operator credential" not in output.out


def test_demo_mode_does_not_use_configured_viewer_credentials(tmp_path, monkeypatch):
    _configure_demo_parent(tmp_path, monkeypatch)
    viewer_secret = "external-viewer-credential-that-is-not-used"
    monkeypatch.setenv("DASHBOARD_VIEWER_SECRET", viewer_secret)
    state = seed_demo_data()
    client = _client_for_demo(state)
    login_page = client.get("/dashboard/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert token is not None

    response = client.post(
        "/dashboard/login",
        data={"csrf_token": token.group(1), "credential": viewer_secret},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert viewer_secret not in response.text


def test_demo_mode_label_is_opt_in(tmp_path):
    app = create_app(
        db_path=tmp_path / "normal_dashboard.sqlite",
        approver_secret=TEST_APPROVER_SECRET,
        telemetry_db_path=tmp_path / "normal_telemetry.sqlite",
    )
    client = TestClient(app)
    _sign_in(client)

    overview = client.get("/dashboard")
    synthetic_demo = client.get("/dashboard/demo")

    assert overview.status_code == synthetic_demo.status_code == 200
    assert "LOCAL MOCK DEMONSTRATION" not in overview.text
    assert "LOCAL MOCK DEMONSTRATION" not in synthetic_demo.text
    assert "OFFLINE DEMO" in synthetic_demo.text
