"""Diagnostic output never echoes provider or transport exception details."""

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentsentinel.paypal_sandbox import SandboxError, UncertainOutcome
from agentsentinel.payment_gateway import GatewayResult
from agentsentinel.payment_sqlite import SQLitePaymentStore

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo_purchase.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("demo_purchase", SCRIPT)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


@pytest.mark.parametrize(("error", "expected"), [
    (SandboxError("Sandbox GET request failed (HTTP 404)"), "GET_HTTP_404"),
    (SandboxError("Sandbox OAuth failed (HTTP 401)"), "OAUTH_HTTP_401"),
    (UncertainOutcome("private transport detail"), "NETWORK_OUTCOME_UNKNOWN"),
    (SandboxError("Bearer private-token"), "SANDBOX_OPERATION_UNAVAILABLE"),
    (ValueError("approval URL private-token"), "INVALID_INPUT_OR_RESPONSE"),
])
def test_safe_failure_codes_are_bounded(error, expected):
    assert demo._safe_failure_code(error) == expected
    assert "private" not in demo._safe_failure_code(error)


def seeded_create(tmp_path):
    db = tmp_path / "demo.sqlite"
    SQLitePaymentStore(db, trusted_issuer_id="demo_issuer")
    with sqlite3.connect(db) as conn:
        conn.execute("""INSERT INTO operations
            (operation_id,fingerprint,principal_id,scope_id,intent_id,merchant_id,amount_text,currency,
             action,target_order_id,state,result_json,authorization_id,attempt_id,idempotency_key,
             provider_order_id,provider_capture_id,updated_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("demo_create_001", "f" * 64, "demo_user", "demo_scope", "intent_001", "ABCDEFGH",
             "1.00", "USD", "create_order", None, "COMPLETED", None, "auth_001", "attempt_001",
             "as-create-" + "a" * 32, "ORDER123", None,
             datetime(2026, 10, 13, tzinfo=timezone.utc).isoformat()))
    return db


class ReadOnlyFakeClient:
    def __init__(self, *, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get_order(self, order_id):
        self.calls.append("GET_ORDER")
        if self.error:
            raise self.error
        return self.response

    def get_capture(self, capture_id):
        self.calls.append("GET_CAPTURE")
        return {"capture_id": capture_id, "status": "COMPLETED"}


def test_created_order_later_404_keeps_local_states_and_db_unchanged(tmp_path):
    db = seeded_create(tmp_path)
    before = db.read_bytes()
    local = demo._local_snapshot(db, "demo_create_001")
    client = ReadOnlyFakeClient(error=SandboxError("Sandbox GET request failed (HTTP 404)"))
    result = demo._read_only_diagnostic(local, client, "ABCDEFGH")
    assert result["provider_http_status"] == 404
    assert result["provider_error_code"] == "GET_HTTP_404"
    assert result["order_creation_state"] == "CREATE_ACKNOWLEDGED"
    assert result["buyer_approval_state"] == "UNVERIFIED"
    assert result["capture_state"] == "NOT_ATTEMPTED_LOCALLY"
    assert result["provider_order_status"] is None
    assert result["local_claim_recorded"] and result["local_request_id_recorded"]
    assert client.calls == ["GET_ORDER"] and db.read_bytes() == before
    assert "ORDER123" not in json.dumps(result)


def test_initial_get_200_then_later_404_does_not_infer_expiry_or_capture(tmp_path):
    db = seeded_create(tmp_path)
    local = demo._local_snapshot(db, "demo_create_001")
    client = ReadOnlyFakeClient(response={
        "order_id": "ORDER123", "status": "CREATED", "merchant_id": "ABCDEFGH",
        "amount": "1.00", "currency": "USD", "capture_id": None,
    })
    first = demo._read_only_diagnostic(local, client, "ABCDEFGH")
    client.error = SandboxError("Sandbox GET request failed (HTTP 404)")
    later = demo._read_only_diagnostic(local, client, "ABCDEFGH")
    assert first["provider_http_status"] == 200 and first["buyer_approval_state"] == "AWAITING_BUYER"
    assert later["provider_http_status"] == 404 and later["buyer_approval_state"] == "UNVERIFIED"
    assert first["order_creation_state"] == later["order_creation_state"] == "CREATE_ACKNOWLEDGED"
    assert later["capture_state"] == "NOT_ATTEMPTED_LOCALLY"
    assert "expired" not in json.dumps(later).lower()
    assert client.calls == ["GET_ORDER", "GET_ORDER"]


def test_provider_approved_is_separate_from_create_and_capture(tmp_path):
    local = demo._local_snapshot(seeded_create(tmp_path), "demo_create_001")
    client = ReadOnlyFakeClient(response={
        "order_id": "ORDER123", "status": "APPROVED", "merchant_id": "ABCDEFGH",
        "amount": "1.00", "currency": "USD", "capture_id": None,
    })
    result = demo._read_only_diagnostic(local, client, "ABCDEFGH")
    assert result["provider_http_status"] == 200
    assert result["provider_binding"] == "MATCH"
    assert result["buyer_approval_state"] == "PROVIDER_APPROVED"
    assert result["capture_state"] == "NOT_ATTEMPTED_LOCALLY"
    assert client.calls == ["GET_ORDER"]


def test_external_capture_is_reported_without_claiming_local_capture(tmp_path):
    local = demo._local_snapshot(seeded_create(tmp_path), "demo_create_001")
    client = ReadOnlyFakeClient(response={
        "order_id": "ORDER123", "status": "COMPLETED", "merchant_id": "ABCDEFGH",
        "amount": "1.00", "currency": "USD", "capture_id": "CAPTURE123",
    })
    result = demo._read_only_diagnostic(local, client, "ABCDEFGH")
    assert result["provider_capture_http_status"] == 200
    assert result["provider_capture_status"] == "COMPLETED"
    assert result["buyer_approval_state"] == "NOT_SEPARATELY_OBSERVED"
    assert result["capture_state"] == "PROVIDER_CAPTURE_WITHOUT_LOCAL_OPERATION"
    assert client.calls == ["GET_ORDER", "GET_CAPTURE"]


def test_diagnose_command_never_constructs_writable_store(tmp_path, monkeypatch, capsys):
    db = seeded_create(tmp_path)
    client = ReadOnlyFakeClient(error=SandboxError("Sandbox GET request failed (HTTP 404)"))
    monkeypatch.setattr(demo, "DEMO_DB", db)
    monkeypatch.setattr(demo, "read_local_env", lambda path: {
        "PAYPAL_ENVIRONMENT": "sandbox", "PAYPAL_CLIENT_ID": "fake_client",
        "PAYPAL_CLIENT_SECRET": "fake_secret", "PAYPAL_MERCHANT_ID": "ABCDEFGH",
    })
    monkeypatch.setattr(demo, "PayPalSandboxClient", lambda config: client)
    monkeypatch.setattr(demo, "SQLitePaymentStore", lambda *args, **kwargs: pytest.fail("writable store opened"))
    assert demo.main(["diagnose", "--operation-id", "demo_create_001"]) == 2
    output = capsys.readouterr().out
    assert json.loads(output)["provider_http_status"] == 404
    assert "fake_secret" not in output and "ORDER123" not in output
    assert client.calls == ["GET_ORDER"]


def test_create_cli_result_labels_operation_completion_precisely(tmp_path):
    local = demo._local_snapshot(seeded_create(tmp_path), "demo_create_001")
    result = GatewayResult(operation_id="demo_create_001", decision=None,
                           status="COMPLETED", reason_codes=("EXECUTED_SANDBOX",),
                           provider_order_id="ORDER123")
    output = demo._safe_result(result, local)
    assert output["local_operation_state"] == "COMPLETED"
    assert output["order_creation_state"] == "CREATE_ACKNOWLEDGED"
    assert output["buyer_approval_state"] == "UNVERIFIED"
    assert output["capture_state"] == "NOT_ATTEMPTED_LOCALLY"
    assert "status" not in output
