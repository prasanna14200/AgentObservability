from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from agentsentinel.approval_service import ApprovalStore
from agentsentinel.payment_policy import AuthenticatedScope
from agentsentinel.payment_sqlite import SQLitePaymentStore
from agentsentinel.schema.payment import Money, PaymentIntent
from scripts import issue_authorization

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def make_intent(operation_id: str = "issue_auth_test") -> PaymentIntent:
    return PaymentIntent(
        intent_id=f"intent_{operation_id}",
        operation_id=operation_id,
        trace_id=f"trace_{operation_id}",
        scope_id="demo_local_scope",
        money=Money(amount="2.50", currency="USD"),
        merchant_id="TESTMERCHANT",
        purpose="Authorized local demo",
        action="create_order",
        requested_at=NOW,
    )


def initialized_database(tmp_path):
    db = tmp_path / "issuer.sqlite"
    ApprovalStore(db)
    SQLitePaymentStore(db, trusted_issuer_id=issue_authorization.DEMO_ISSUER)
    return db


def authorization_count(db, operation_id: str) -> int:
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM authorizations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    return row[0]


def test_issuer_rejects_active_kill_switch(tmp_path):
    db = initialized_database(tmp_path)
    ApprovalStore(db).set_kill_switch(enabled=True, actor_role="approver")
    intent = make_intent()

    with pytest.raises(RuntimeError, match="blocked by kill switch"):
        issue_authorization.issue_after_local_confirmation(db, intent)

    assert authorization_count(db, intent.operation_id) == 0


@pytest.mark.parametrize("state", ["missing_row", "missing_table", "invalid_value"])
def test_issuer_fails_closed_for_invalid_kill_switch_state(tmp_path, state):
    db = initialized_database(tmp_path)
    with sqlite3.connect(db) as conn:
        if state == "missing_row":
            conn.execute("DELETE FROM kill_switch WHERE key='payment_kill_switch'")
        elif state == "missing_table":
            conn.execute("DROP TABLE kill_switch")
        else:
            conn.execute(
                "UPDATE kill_switch SET enabled=2 WHERE key='payment_kill_switch'"
            )
    intent = make_intent(f"issue_auth_{state}")

    with pytest.raises(RuntimeError):
        issue_authorization.issue_after_local_confirmation(db, intent)

    assert authorization_count(db, intent.operation_id) == 0


def test_issuer_fails_closed_when_database_is_unreadable(tmp_path, monkeypatch):
    db = initialized_database(tmp_path)
    intent = make_intent("issue_auth_unreadable")
    connect = sqlite3.connect

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("private database detail")

    with monkeypatch.context() as patch:
        patch.setattr(issue_authorization.sqlite3, "connect", unavailable)
        with pytest.raises(RuntimeError, match="authorization issuance unavailable"):
            issue_authorization.issue_after_local_confirmation(db, intent)

    with connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM authorizations WHERE operation_id=?",
            (intent.operation_id,),
        ).fetchone()[0] == 0


def test_issuer_allows_disabled_switch_after_local_confirmation(tmp_path, monkeypatch, capsys):
    db = tmp_path / "issuer.sqlite"
    monkeypatch.setattr(issue_authorization, "DEMO_DB", db)
    monkeypatch.setattr(issue_authorization, "read_local_env", lambda path: {})
    monkeypatch.setattr(
        issue_authorization.SandboxConfig,
        "from_values",
        lambda values: type("Config", (), {"merchant_id": "TESTMERCHANT"})(),
    )
    confirmation_calls = []

    def confirm_after_inspecting_store(intent):
        confirmation_calls.append(intent.operation_id)
        assert authorization_count(db, intent.operation_id) == 0
        return True

    monkeypatch.setattr(
        issue_authorization,
        "confirm_local_user",
        confirm_after_inspecting_store,
    )
    expected_intent = issue_authorization.demo_intent(
        "issue_auth_confirmed", "TESTMERCHANT", "2.50", "create_order"
    )

    result = issue_authorization.main(
        [
            "--operation-id",
            expected_intent.operation_id,
            "--amount",
            "2.50",
            "--action",
            "create_order",
        ]
    )

    assert result == 0
    assert confirmation_calls == [expected_intent.operation_id]
    assert authorization_count(db, expected_intent.operation_id) == 1
    issued = SQLitePaymentStore(
        db, trusted_issuer_id=issue_authorization.DEMO_ISSUER
    ).find(
        AuthenticatedScope(
            principal_id=issue_authorization.DEMO_PRINCIPAL,
            scope_id=issue_authorization.DEMO_SCOPE,
        ),
        expected_intent.intent_id,
        expected_intent.operation_id,
    )
    assert issued is not None
    assert issued.provider_mode == "SANDBOX"
    assert issued.binds_to(expected_intent, "SANDBOX")
    assert (issued.expires_at - issued.issued_at).total_seconds() == 600
    assert capsys.readouterr().out.endswith(
        "Bound, expiring demo authorization issued for the operation.\n"
    )
