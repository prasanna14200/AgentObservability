from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentsentinel.approval_service import create_app
from agentsentinel.payment_gateway import _fingerprint
from agentsentinel.payment_policy import AuthenticatedScope
from agentsentinel.payment_sqlite import SQLitePaymentStore
from agentsentinel.paypal_sandbox import UncertainOutcome
from agentsentinel.schema.payment import Money, PaymentIntent
from scripts import paypal_sandbox_spike

APPROVER_SECRET = "test-approver-secret-for-sandbox-dashboard-001"
CLIENT_ID = "TEST_ONLY_SANDBOX_CLIENT"
CLIENT_SECRET = "TEST_ONLY_SANDBOX_SECRET"
MERCHANT_ID = "TESTMERCHANT001"


class MockPayPalClient:
    def __init__(self) -> None:
        self.orders: dict[str, dict[str, object]] = {}
        self.captures: dict[str, dict[str, object]] = {}
        self.failure: Exception | None = None

    def create_order(self, operation_id: str, amount: str, currency: str = "USD"):
        order_id = f"order_{operation_id}"
        return {"order_id": order_id, "status": "CREATED"}

    def get_order(self, order_id: str):
        if self.failure is not None:
            raise self.failure
        return self.orders[order_id]

    def get_capture(self, capture_id: str):
        return self.captures[capture_id]


def _install_spike_fakes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: MockPayPalClient,
) -> Path:
    journal_path = tmp_path / "operations.json"
    database_path = tmp_path / "application.sqlite"
    monkeypatch.setattr(paypal_sandbox_spike, "JOURNAL", journal_path)
    monkeypatch.setattr(
        paypal_sandbox_spike,
        "_application_database_path",
        lambda: database_path,
    )
    monkeypatch.setattr(
        paypal_sandbox_spike,
        "read_local_env",
        lambda _path: {
            "PAYPAL_ENVIRONMENT": "SANDBOX",
            "PAYPAL_CLIENT_ID": CLIENT_ID,
            "PAYPAL_CLIENT_SECRET": CLIENT_SECRET,
            "PAYPAL_MERCHANT_ID": MERCHANT_ID,
        },
    )
    monkeypatch.setattr(paypal_sandbox_spike, "PayPalSandboxClient", lambda _config: client)
    return database_path


def _create_spike_order(operation_id: str, amount: str) -> None:
    assert paypal_sandbox_spike.main(
        ["create", "--operation-id", operation_id, "--amount", amount]
    ) == 0


def _reconcile_spike_order(operation_id: str) -> int:
    return paypal_sandbox_spike.main(["reconcile", "--operation-id", operation_id])


def _authenticated_client(database_path: Path) -> TestClient:
    return TestClient(create_app(db_path=database_path, approver_secret=APPROVER_SECRET))


def _sign_in(client: TestClient) -> None:
    login = client.get("/dashboard/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
    response = client.post(
        "/dashboard/login",
        data={"csrf_token": token, "credential": APPROVER_SECRET},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_reconcile_persists_pending_and_completed_records_and_dashboard_refreshes_totals(
    tmp_path, monkeypatch, capsys
):
    client = MockPayPalClient()
    database_path = _install_spike_fakes(tmp_path, monkeypatch, client)
    client.orders = {
        "order_tx_pending": {
            "order_id": "order_tx_pending",
            "status": "PAYER_ACTION_REQUIRED",
            "amount": "4.00",
            "currency": "USD",
            "merchant_id": MERCHANT_ID,
            "capture_id": None,
            "capture_status": None,
        },
        "order_tx_completed": {
            "order_id": "order_tx_completed",
            "status": "COMPLETED",
            "amount": "6.00",
            "currency": "USD",
            "merchant_id": MERCHANT_ID,
            "capture_id": "capture_tx_completed",
            "capture_status": "COMPLETED",
        },
    }
    client.captures = {
        "capture_tx_completed": {
            "capture_id": "capture_tx_completed",
            "status": "COMPLETED",
            "amount": "6.00",
            "currency": "USD",
            "related_order_id": "order_tx_completed",
        }
    }

    _create_spike_order("tx_pending", "4.00")
    assert _reconcile_spike_order("tx_pending") == 0
    pending_output = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert pending_output["status"] == "PENDING"

    dashboard = _authenticated_client(database_path)
    _sign_in(dashboard)
    initial = dashboard.get("/dashboard/api/transactions")
    assert initial.status_code == 200
    assert initial.json()["summary"]["transaction_count"] == 1
    assert initial.json()["summary"]["totals_by_currency"] == {"USD": "4.00"}

    _create_spike_order("tx_completed", "6.00")
    assert _reconcile_spike_order("tx_completed") == 0
    completed_output = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert completed_output["status"] == "COMPLETED"

    response = dashboard.get("/dashboard/api/transactions")
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["transaction_count"] == 2
    assert payload["summary"]["status_counts"]["PENDING"] == 1
    assert payload["summary"]["status_counts"]["COMPLETED"] == 1
    assert payload["summary"]["totals_by_currency"] == {"USD": "10.00"}
    assert {item["operation_id"] for item in payload["transactions"]} == {
        "tx_pending",
        "tx_completed",
    }
    assert all(item["source"] == "manual_sandbox_evidence" for item in payload["transactions"])
    by_id = {item["operation_id"]: item for item in payload["transactions"]}
    assert by_id["tx_pending"]["provider_order_status"] == "PAYER_ACTION_REQUIRED"
    assert by_id["tx_completed"]["provider_order_status"] == "COMPLETED"
    assert by_id["tx_completed"]["provider_capture_status"] == "COMPLETED"
    serialized = response.text
    assert CLIENT_ID not in serialized
    assert CLIENT_SECRET not in serialized
    assert "access_token" not in serialized
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='operations'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sandbox_reconciliation_evidence"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT order_status,capture_status FROM sandbox_reconciliation_evidence "
            "WHERE operation_id='tx_completed'"
        ).fetchone() == ("COMPLETED", "COMPLETED")


def test_reconciliation_failure_is_persisted_as_sanitized_error(tmp_path, monkeypatch, capsys):
    client = MockPayPalClient()
    database_path = _install_spike_fakes(tmp_path, monkeypatch, client)
    _create_spike_order("tx_error", "2.00")
    capsys.readouterr()
    client.failure = UncertainOutcome("sensitive provider response")

    assert _reconcile_spike_order("tx_error") == 2
    output = capsys.readouterr()
    assert "sensitive provider response" not in output.out + output.err

    dashboard = _authenticated_client(database_path)
    _sign_in(dashboard)
    payload = dashboard.get("/dashboard/api/transactions").json()
    assert payload["summary"]["transaction_count"] == 1
    assert payload["summary"]["status_counts"]["RECONCILIATION_ERROR"] == 1
    assert payload["transactions"][0]["status"] == "RECONCILIATION_ERROR"


def test_verified_voided_order_is_failed_and_binding_mismatch_is_error(
    tmp_path, monkeypatch, capsys
):
    client = MockPayPalClient()
    database_path = _install_spike_fakes(tmp_path, monkeypatch, client)
    client.orders = {
        "order_tx_failed": {
            "order_id": "order_tx_failed",
            "status": "VOIDED",
            "amount": "3.00",
            "currency": "USD",
            "merchant_id": MERCHANT_ID,
            "capture_id": None,
            "capture_status": None,
        },
        "order_tx_mismatch": {
            "order_id": "order_tx_mismatch",
            "status": "APPROVED",
            "amount": "9.00",
            "currency": "USD",
            "merchant_id": MERCHANT_ID,
            "capture_id": None,
            "capture_status": None,
        },
    }
    _create_spike_order("tx_failed", "3.00")
    _create_spike_order("tx_mismatch", "3.00")
    capsys.readouterr()

    assert _reconcile_spike_order("tx_failed") == 0
    assert _reconcile_spike_order("tx_mismatch") == 2

    dashboard = _authenticated_client(database_path)
    _sign_in(dashboard)
    payload = dashboard.get("/dashboard/api/transactions").json()
    by_id = {item["operation_id"]: item for item in payload["transactions"]}
    assert by_id["tx_failed"]["status"] == "FAILED"
    assert by_id["tx_mismatch"]["status"] == "RECONCILIATION_ERROR"
    assert payload["summary"]["status_counts"]["FAILED"] == 1
    assert payload["summary"]["status_counts"]["RECONCILIATION_ERROR"] == 1


def test_transaction_endpoint_requires_dashboard_auth_and_overview_uses_backend_refresh(tmp_path):
    database_path = tmp_path / "application.sqlite"
    client = _authenticated_client(database_path)
    assert client.get("/dashboard/api/transactions").status_code == 401

    client = _authenticated_client(database_path)
    _sign_in(client)
    overview = client.get("/dashboard")
    assert overview.status_code == 200
    assert "/dashboard/static/transactions.js" in overview.text
    assert 'id="transaction-rows"' in overview.text
    assert "Loading persisted records" in overview.text
    policy = overview.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "connect-src 'self'" in policy


def test_dashboard_api_reads_gateway_operations_from_the_application_sqlite(tmp_path):
    database_path = tmp_path / "application.sqlite"
    payment_store = SQLitePaymentStore(
        database_path,
        trusted_issuer_id="dashboard_test_issuer",
    )
    intent = PaymentIntent(
        intent_id="dashboard_gateway_intent",
        operation_id="dashboard_gateway_operation",
        trace_id="dashboard_gateway_trace",
        scope_id="dashboard_gateway_scope",
        money=Money(amount="2.50", currency="USD"),
        merchant_id="TESTMERCHANT001",
        purpose="Dashboard integration test",
        action="create_order",
        requested_at=datetime.now(timezone.utc),
    )
    context = AuthenticatedScope(
        principal_id="dashboard_gateway_principal",
        scope_id=intent.scope_id,
    )
    reservation = payment_store.reserve_intent(
        intent,
        context,
        _fingerprint(intent, context, "MOCK"),
        "MOCK",
    )
    assert reservation.state == "NEW"

    dashboard = _authenticated_client(database_path)
    _sign_in(dashboard)
    response = dashboard.get("/dashboard/api/transactions")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["transaction_count"] == 1
    assert payload["summary"]["totals_by_currency"] == {"USD": "2.50"}
    assert payload["transactions"][0]["source"] == "gateway_operation"
    assert payload["transactions"][0]["status"] == "PENDING"
    assert payload["transactions"][0]["operation_id"] == "dashboard_gateway_operation"
