from __future__ import annotations

import sqlite3
import json
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from agentsentinel.approval_service import ApprovalStore
from agentsentinel.payment_gateway import (
    AdapterOutcome,
    PaymentGateway,
    LiveOnlyAdapter,
    SandboxOnlyAdapter,
    TestOnlyAdapter as BaseTestOnlyAdapter,
    _fingerprint,
    _legacy_fingerprint,
)
from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy
from agentsentinel.payment_provider import ProviderModeConfig
from agentsentinel.payment_sqlite import (
    PaymentRecoveryWorker,
    SQLitePaymentStore,
    SQLiteReconciler,
    StoreUnavailable,
    TestOnlyOutcomeVerifier,
    VerifiedOutcome,
)
from agentsentinel.paypal_sandbox import BASE_URL, SandboxConfig, SandboxError
from agentsentinel.payment_live_adapter import LivePaymentAdapter
from agentsentinel.paypal_live import LiveExecutionDisabled
from agentsentinel.schema.payment import Money, PaymentAuthorization, PaymentIntent

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
CONTEXT = AuthenticatedScope(principal_id="mode_test_user", scope_id="mode_test_scope")


def _intent(operation_id: str = "mode_operation") -> PaymentIntent:
    return PaymentIntent(
        intent_id="mode_intent",
        operation_id=operation_id,
        trace_id="mode_trace",
        scope_id=CONTEXT.scope_id,
        money=Money(amount="1.00", currency="USD"),
        merchant_id="MODEMERCHANT",
        purpose="Offline provider-mode test",
        action="create_order",
        requested_at=NOW,
    )


def _seed(db_path, intent: PaymentIntent, provider_mode: str = "MOCK") -> None:
    auth = PaymentAuthorization(
        authorization_id=f"auth_{intent.operation_id}",
        issuer_id="mode_issuer",
        scope_id=intent.scope_id,
        intent_id=intent.intent_id,
        operation_id=intent.operation_id,
        merchant_id=intent.merchant_id,
        money=intent.money,
        action=intent.action,
        provider_mode=provider_mode,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
        status="issued",
        record_origin="application_issued",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO authorizations(
                authorization_id,issuer_id,principal_id,scope_id,intent_id,operation_id,
                merchant_id,amount_text,currency,action,provider_mode,target_order_id,
                issued_at,expires_at,status,closed_at,record_origin,approval_id,
                approval_reason_codes
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                auth.authorization_id,
                auth.issuer_id,
                CONTEXT.principal_id,
                auth.scope_id,
                auth.intent_id,
                auth.operation_id,
                auth.merchant_id,
                str(auth.money.amount),
                auth.money.currency,
                auth.action,
                auth.provider_mode,
                auth.target_order_id,
                auth.issued_at.isoformat(),
                auth.expires_at.isoformat(),
                auth.status,
                None,
                auth.record_origin,
                None,
                "[]",
            ),
        )


class CountingAdapter(BaseTestOnlyAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, intent: PaymentIntent) -> AdapterOutcome:
        self.calls += 1
        return AdapterOutcome(status="COMPLETED")


class CountingSandboxAdapter(SandboxOnlyAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, intent: PaymentIntent) -> AdapterOutcome:
        self.calls += 1
        return AdapterOutcome(status="ORDER_CREATED", provider_order_id="OFFLINE_ORDER")


class CountingLiveAdapter(LiveOnlyAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, intent: PaymentIntent) -> AdapterOutcome:
        self.calls += 1
        raise LiveExecutionDisabled("LIVE payment execution is disabled")


class CountingVerifier(TestOnlyOutcomeVerifier):
    def __init__(self) -> None:
        self.calls = 0

    def verify(self, snapshot):
        self.calls += 1
        return VerifiedOutcome(
            operation_id=snapshot.operation_id,
            provider_mode=snapshot.provider_mode,
            status="STILL_UNCERTAIN",
            attempt_id=snapshot.attempt_id,
            idempotency_key=snapshot.idempotency_key,
            verified_at=NOW,
        )


@pytest.mark.parametrize(
    ("values", "mode"),
    [
        ({}, "MOCK"),
        ({"PAYPAL_ENVIRONMENT": "mock"}, "MOCK"),
        ({"PAYPAL_ENVIRONMENT": "SANDBOX"}, "SANDBOX"),
        ({"PAYPAL_ENVIRONMENT": "live"}, "LIVE"),
    ],
)
def test_provider_mode_configuration_is_explicit_and_normalized(values, mode):
    assert ProviderModeConfig.from_values(values).mode == mode


def test_missing_environment_mode_defaults_only_to_mock(monkeypatch):
    monkeypatch.delenv("PAYPAL_ENVIRONMENT", raising=False)
    for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_MERCHANT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert ProviderModeConfig.from_environment().mode == "MOCK"
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "LIVE")
    assert ProviderModeConfig.from_environment().mode == "LIVE"


@pytest.mark.parametrize("value", ["", " LIVE", "SANDBOX ", "production", "sandbox/live"])
def test_invalid_provider_mode_is_rejected(value):
    with pytest.raises(ValueError, match="PAYPAL_ENVIRONMENT"):
        ProviderModeConfig.from_values({"PAYPAL_ENVIRONMENT": value})


def test_malformed_provider_mode_value_is_not_treated_as_missing():
    with pytest.raises(ValueError, match="PAYPAL_ENVIRONMENT"):
        ProviderModeConfig.from_values({"PAYPAL_ENVIRONMENT": None})


def test_sandbox_credentials_do_not_infer_or_switch_provider_mode():
    credentials = {
        "PAYPAL_CLIENT_ID": "offline-client",
        "PAYPAL_CLIENT_SECRET": "offline-secret",
        "PAYPAL_MERCHANT_ID": "MODEMERCHANT",
    }
    with pytest.raises(ValueError, match="require PAYPAL_ENVIRONMENT=SANDBOX"):
        ProviderModeConfig.from_values(credentials)
    with pytest.raises(ValueError, match="require PAYPAL_ENVIRONMENT=SANDBOX"):
        ProviderModeConfig.from_values({"PAYPAL_ENVIRONMENT": "MOCK", **credentials})
    assert ProviderModeConfig.from_values(
        {"PAYPAL_ENVIRONMENT": "SANDBOX", **credentials}
    ).mode == "SANDBOX"


def test_partial_sandbox_credentials_fail_closed():
    with pytest.raises(ValueError, match="configured together"):
        ProviderModeConfig.from_values(
            {"PAYPAL_ENVIRONMENT": "SANDBOX", "PAYPAL_CLIENT_ID": "offline-client"}
        )


def test_sandbox_client_accepts_authoritative_uppercase_mode_without_network():
    config = SandboxConfig.from_values(
        {
            "PAYPAL_ENVIRONMENT": "SANDBOX",
            "PAYPAL_CLIENT_ID": "offline-client",
            "PAYPAL_CLIENT_SECRET": "offline-secret",
            "PAYPAL_MERCHANT_ID": "MODEMERCHANT",
        }
    )
    assert config.merchant_id == "MODEMERCHANT"
    with pytest.raises(SandboxError):
        SandboxConfig.from_values(
            {
                "PAYPAL_ENVIRONMENT": "LIVE",
                "PAYPAL_CLIENT_ID": "offline-client",
                "PAYPAL_CLIENT_SECRET": "offline-secret",
                "PAYPAL_MERCHANT_ID": "MODEMERCHANT",
            }
        )


def test_sandbox_endpoint_is_fixed_and_never_uses_live_host():
    assert BASE_URL == "https://api-m.sandbox.paypal.com"
    assert "api-m.paypal.com" not in BASE_URL


def test_mock_mode_rejects_sandbox_adapter_before_any_call(tmp_path):
    store = SQLitePaymentStore(tmp_path / "mock-adapter.sqlite", trusted_issuer_id="mode_issuer")
    adapter = CountingSandboxAdapter()
    with pytest.raises(ValueError, match="Sandbox adapter requires explicit SANDBOX"):
        PaymentGateway(
            PaymentPolicy(store),
            store,
            store,
            adapter,
            provider_mode=ProviderModeConfig.mock(),
        )
    assert adapter.calls == 0


def test_sandbox_mode_retains_separate_execution_opt_in(tmp_path):
    database = tmp_path / "sandbox-guard.sqlite"
    store = SQLitePaymentStore(database, trusted_issuer_id="mode_issuer", clock=lambda: NOW)
    intent = _intent("sandbox_guard")
    _seed(database, intent, "SANDBOX")
    adapter = CountingSandboxAdapter()
    gateway = PaymentGateway(
        PaymentPolicy(store, clock=lambda: NOW),
        store,
        store,
        adapter,
        provider_mode=ProviderModeConfig.sandbox(),
    )

    result = gateway.submit(intent, CONTEXT)

    assert result.status == "REJECTED"
    assert result.reason_codes == ("EXECUTION_DISABLED",)
    assert adapter.calls == 0


@pytest.mark.parametrize(
    ("gateway_mode", "authorization_mode"),
    [
        ("MOCK", "SANDBOX"),
        ("SANDBOX", "MOCK"),
        ("SANDBOX", "LIVE"),
        ("LIVE", "SANDBOX"),
    ],
)
def test_authorizations_cannot_cross_provider_modes(
    tmp_path, gateway_mode, authorization_mode
):
    database = tmp_path / "mode.sqlite"
    store = SQLitePaymentStore(database, trusted_issuer_id="mode_issuer", clock=lambda: NOW)
    intent = _intent()
    _seed(database, intent, authorization_mode)
    gateway = PaymentGateway(
        PaymentPolicy(store, clock=lambda: NOW),
        store,
        store,
        provider_mode=ProviderModeConfig(gateway_mode),
    )

    result = gateway.submit(intent, CONTEXT)

    assert result.status == "REJECTED"
    assert result.provider_mode == gateway_mode
    assert result.decision is not None
    assert result.decision.outcome == "BLOCK"
    assert "AUTHORIZATION_PROVIDER_MODE_MISMATCH" in result.decision.reason_codes
    assert store.audit_operation(intent.operation_id).provider_mode == gateway_mode


def test_live_mode_is_recognized_but_never_executes_an_adapter(tmp_path):
    database = tmp_path / "live.sqlite"
    store = SQLitePaymentStore(database, trusted_issuer_id="mode_issuer", clock=lambda: NOW)
    intent = _intent("live_disabled")
    _seed(database, intent, "LIVE")
    gateway = PaymentGateway(
        PaymentPolicy(store, clock=lambda: NOW),
        store,
        store,
        provider_mode=ProviderModeConfig.live(),
    )

    result = gateway.submit(intent, CONTEXT)

    assert result.status == "REJECTED"
    assert result.reason_codes == ("LIVE_EXECUTION_DISABLED",)
    assert result.provider_mode == "LIVE"
    assert result.decision is not None and result.decision.outcome == "ALLOW"
    assert store.audit_operation(intent.operation_id).provider_mode == "LIVE"


def test_live_mode_rejects_an_adapter_at_construction(tmp_path):
    store = SQLitePaymentStore(tmp_path / "live-adapter.sqlite", trusted_issuer_id="mode_issuer")
    adapter = CountingAdapter()
    with pytest.raises(ValueError, match="LIVE payment execution is disabled"):
        PaymentGateway(
            PaymentPolicy(store),
            store,
            store,
            adapter,
            provider_mode=ProviderModeConfig.live(),
        )
    assert adapter.calls == 0


def test_approval_is_bound_to_mode_and_cannot_be_reused_after_switch(tmp_path):
    database = tmp_path / "approval-mode.sqlite"
    approvals = ApprovalStore(database, clock=lambda: NOW, trusted_issuer_id="mode_issuer")
    payments = SQLitePaymentStore(database, trusted_issuer_id="mode_issuer", clock=lambda: NOW)
    intent = _intent("approved_sandbox")
    approval = approvals.create_approval(
        intent=intent,
        context=CONTEXT,
        reason_codes=("REVIEW_AMOUNT_THRESHOLD",),
        provider_mode="SANDBOX",
    )
    approved = approvals.approve_approval(
        approval.approval_id,
        request_fingerprint=approval.request_fingerprint,
    )
    assert approved.provider_mode == "SANDBOX"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT provider_mode FROM authorizations WHERE operation_id=?",
            (intent.operation_id,),
        ).fetchone()[0] == "SANDBOX"

    switched_gateway = PaymentGateway(
        PaymentPolicy(payments, clock=lambda: NOW),
        payments,
        payments,
        provider_mode=ProviderModeConfig.mock(),
    )
    result = switched_gateway.submit(intent, CONTEXT)
    assert result.status == "REJECTED"
    assert result.decision is not None and result.decision.outcome == "BLOCK"
    assert "AUTHORIZATION_PROVIDER_MODE_MISMATCH" in result.decision.reason_codes


def test_existing_mock_operation_fingerprint_remains_readable(tmp_path):
    database = tmp_path / "legacy-operation.sqlite"
    store = SQLitePaymentStore(database, trusted_issuer_id="mode_issuer", clock=lambda: NOW)
    intent = _intent("legacy_mock_operation")
    current_fingerprint = _fingerprint(intent, CONTEXT, "MOCK")
    assert store.reserve_intent(intent, CONTEXT, current_fingerprint).state == "NEW"
    previous_fingerprint = _legacy_fingerprint(intent, CONTEXT)
    stored_result = {
        "operation_id": intent.operation_id,
        "provider_mode": "MOCK",
        "decision": None,
        "status": "COMPLETED",
        "reason_codes": ["EXECUTED_FAKE"],
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE operations SET fingerprint=?,state='COMPLETED',result_json=? "
            "WHERE operation_id=?",
            (
                previous_fingerprint,
                json.dumps(stored_result),
                intent.operation_id,
            ),
        )

    gateway = PaymentGateway(
        PaymentPolicy(store, clock=lambda: NOW),
        store,
        store,
        provider_mode=ProviderModeConfig.mock(),
    )
    result = gateway.submit(intent, CONTEXT)

    assert result.status == "COMPLETED"
    assert result.provider_mode == "MOCK"
    assert store.audit_operation(intent.operation_id).provider_mode == "MOCK"


def test_existing_mock_approval_fingerprint_remains_readable(tmp_path):
    database = tmp_path / "legacy-approval.sqlite"
    approvals = ApprovalStore(database, clock=lambda: NOW, trusted_issuer_id="mode_issuer")
    intent = _intent("legacy_mock_approval")
    approval = approvals.create_approval(
        intent=intent,
        context=CONTEXT,
        reason_codes=("REVIEW_AMOUNT_THRESHOLD",),
    )
    legacy_fingerprint = _legacy_fingerprint(intent, CONTEXT)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE approvals SET request_fingerprint=? WHERE approval_id=?",
            (legacy_fingerprint, approval.approval_id),
        )

    existing = approvals.create_approval(
        intent=intent,
        context=CONTEXT,
        reason_codes=("REVIEW_AMOUNT_THRESHOLD",),
    )
    approved = approvals.approve_approval(
        existing.approval_id,
        request_fingerprint=legacy_fingerprint,
    )

    assert approved.provider_mode == "MOCK"
    assert approvals.approved_payment(approved.approval_id) == (intent, CONTEXT)


def test_legacy_approval_and_authorization_columns_migrate_to_mock(tmp_path):
    database = tmp_path / "legacy-approval-schema.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE approvals (
                approval_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
                trace_id TEXT NOT NULL, intent_id TEXT NOT NULL, principal_id TEXT NOT NULL,
                scope_id TEXT NOT NULL, action TEXT NOT NULL, merchant_id TEXT NOT NULL,
                amount_text TEXT NOT NULL, currency TEXT NOT NULL, target_order_id TEXT,
                request_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL, status TEXT NOT NULL, reason_codes TEXT NOT NULL,
                decision_reason TEXT, intent_snapshot TEXT
            );
            CREATE TABLE authorizations (
                authorization_id TEXT PRIMARY KEY, issuer_id TEXT NOT NULL,
                principal_id TEXT NOT NULL, scope_id TEXT NOT NULL, intent_id TEXT NOT NULL,
                operation_id TEXT NOT NULL, merchant_id TEXT NOT NULL, amount_text TEXT NOT NULL,
                currency TEXT NOT NULL, action TEXT NOT NULL, target_order_id TEXT,
                issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL,
                closed_at TEXT, record_origin TEXT NOT NULL, approval_id TEXT,
                approval_reason_codes TEXT,
                UNIQUE(principal_id, scope_id, intent_id, operation_id)
            );
            """
        )
        connection.execute(
            """INSERT INTO approvals(
                approval_id,operation_id,trace_id,intent_id,principal_id,scope_id,action,
                merchant_id,amount_text,currency,request_fingerprint,created_at,expires_at,
                status,reason_codes
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "legacy_approval",
                "legacy_operation",
                "legacy_trace",
                "legacy_intent",
                CONTEXT.principal_id,
                CONTEXT.scope_id,
                "create_order",
                "MODEMERCHANT",
                "1.00",
                "USD",
                "f" * 64,
                NOW.isoformat(),
                (NOW + timedelta(minutes=5)).isoformat(),
                "PENDING",
                "[]",
            ),
        )
        connection.execute(
            """INSERT INTO authorizations(
                authorization_id,issuer_id,principal_id,scope_id,intent_id,operation_id,
                merchant_id,amount_text,currency,action,issued_at,expires_at,status,record_origin
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "legacy_auth",
                "mode_issuer",
                CONTEXT.principal_id,
                CONTEXT.scope_id,
                "legacy_intent",
                "legacy_operation",
                "MODEMERCHANT",
                "1.00",
                "USD",
                "create_order",
                NOW.isoformat(),
                (NOW + timedelta(minutes=5)).isoformat(),
                "issued",
                "application_issued",
            ),
        )

    ApprovalStore(database, clock=lambda: NOW, trusted_issuer_id="mode_issuer")
    with sqlite3.connect(database) as connection:
        approval_mode = connection.execute(
            "SELECT provider_mode FROM approvals WHERE approval_id='legacy_approval'"
        ).fetchone()[0]
        authorization_mode = connection.execute(
            "SELECT provider_mode FROM authorizations WHERE authorization_id='legacy_auth'"
        ).fetchone()[0]

    assert approval_mode == "MOCK"
    assert authorization_mode == "MOCK"


def test_policy_block_still_wins_over_live_execution_denial(tmp_path):
    database = tmp_path / "live-block.sqlite"
    store = SQLitePaymentStore(database, trusted_issuer_id="mode_issuer", clock=lambda: NOW)
    intent = _intent("live_block")
    _seed(database, intent, "LIVE")
    changed_intent = intent.model_copy(
        update={"money": Money(amount="2.00", currency="USD")}
    )
    gateway = PaymentGateway(
        PaymentPolicy(store, clock=lambda: NOW),
        store,
        store,
        provider_mode=ProviderModeConfig.live(),
    )

    result = gateway.submit(changed_intent, CONTEXT)

    assert result.status == "REJECTED"
    assert result.reason_codes == ("POLICY_BLOCK",)
    assert result.decision is not None
    assert result.decision.outcome == "BLOCK"
    assert "AUTHORIZATION_AMOUNT_MISMATCH" in result.decision.reason_codes


@pytest.mark.parametrize(
    ("mode", "adapter_type"),
    [
        ("MOCK", "live"),
        ("SANDBOX", "live"),
        ("LIVE", "sandbox"),
        ("LIVE", "live"),
    ],
)
def test_adapter_routing_rejects_cross_mode_and_live_dispatch(tmp_path, mode, adapter_type):
    store = SQLitePaymentStore(tmp_path / f"route-{mode}-{adapter_type}.sqlite", trusted_issuer_id="mode_issuer")
    adapter = CountingLiveAdapter() if adapter_type == "live" else CountingSandboxAdapter()
    with pytest.raises(ValueError):
        PaymentGateway(
            PaymentPolicy(store),
            store,
            store,
            adapter,
            enable_sandbox_execution=True,
            provider_mode=ProviderModeConfig(mode),
        )
    assert adapter.calls == 0


def test_live_adapter_is_constructible_but_never_executes(tmp_path):
    adapter = LivePaymentAdapter()
    with pytest.raises(LiveExecutionDisabled, match="LIVE payment execution is disabled"):
        adapter.execute(_intent("live_adapter_direct"))


def test_live_approval_creation_and_approval_replay_are_blocked(tmp_path):
    database = tmp_path / "live-approval.sqlite"
    approvals = ApprovalStore(database, clock=lambda: NOW, trusted_issuer_id="mode_issuer")
    intent = _intent("live_approval")

    with pytest.raises(ValueError, match="approvals are unavailable"):
        approvals.create_approval(
            intent=intent, context=CONTEXT, provider_mode="LIVE"
        )

    pending = approvals.create_approval(
        intent=intent,
        context=CONTEXT,
        reason_codes=("REVIEW_AMOUNT_THRESHOLD",),
        provider_mode="SANDBOX",
    )
    live_fingerprint = hashlib.sha256(
        (intent.model_dump_json() + "|" + CONTEXT.model_dump_json() + "|LIVE").encode("utf-8")
    ).hexdigest()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE approvals SET provider_mode='LIVE',request_fingerprint=? WHERE approval_id=?",
            (live_fingerprint, pending.approval_id),
        )
    with pytest.raises(ValueError, match="LIVE approvals are disabled"):
        approvals.approve_approval(
            pending.approval_id, request_fingerprint=live_fingerprint
        )


def test_live_approval_request_is_blocked_after_policy_evaluation(tmp_path):
    database = tmp_path / "live-review.sqlite"
    approvals = ApprovalStore(database, clock=lambda: NOW, trusted_issuer_id="mode_issuer")
    store = SQLitePaymentStore(database, trusted_issuer_id="mode_issuer", clock=lambda: NOW)
    gateway = PaymentGateway(
        PaymentPolicy(store, clock=lambda: NOW),
        store,
        store,
        kill_switch=approvals,
        provider_mode=ProviderModeConfig.live(),
    )
    decision = gateway.evaluate_approval_request(
        _intent("live_review"), CONTEXT, advisory_signals=("request_review",)
    )
    assert decision.outcome == "BLOCK"
    assert decision.reason_codes == ("LIVE_EXECUTION_DISABLED",)
    with pytest.raises(ValueError, match="approvals are unavailable"):
        approvals.create_approval(
            intent=_intent("live_review"),
            context=CONTEXT,
            provider_mode=decision.provider_mode,
        )


def test_live_claim_and_dispatch_marker_cannot_be_bypassed(tmp_path):
    database = tmp_path / "live-claim.sqlite"
    store = SQLitePaymentStore(database, trusted_issuer_id="mode_issuer", clock=lambda: NOW)
    intent = _intent("live_claim")
    _seed(database, intent, "LIVE")
    fingerprint = _fingerprint(intent, CONTEXT, "LIVE")
    assert store.reserve_intent(intent, CONTEXT, fingerprint, "LIVE").state == "NEW"
    decision = PaymentPolicy(store, clock=lambda: NOW).evaluate(
        intent, CONTEXT, provider_mode="LIVE"
    )
    assert decision.outcome == "ALLOW"
    assert store.claim(CONTEXT, intent, decision) is False
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE operations SET state='CLAIMED',attempt_id='attempt_live',"
            "attempt_provider_mode='LIVE',idempotency_key='as-create-" + "0" * 32 + "' "
            "WHERE operation_id=?",
            (intent.operation_id,),
        )
    assert store.mark_dispatch_intent(intent.operation_id, fingerprint) is False
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT dispatch_state FROM operations WHERE operation_id=?",
            (intent.operation_id,),
        ).fetchone()[0] == "NOT_DISPATCHED"
        assert connection.execute(
            "SELECT status FROM authorizations WHERE operation_id=?",
            (intent.operation_id,),
        ).fetchone()[0] == "issued"


def test_live_reconciliation_and_recovery_cannot_invoke_verifiers(tmp_path):
    database = tmp_path / "live-reconciliation.sqlite"
    store = SQLitePaymentStore(database, trusted_issuer_id="mode_issuer", clock=lambda: NOW)
    intent = _intent("live_recovery")
    fingerprint = _fingerprint(intent, CONTEXT, "LIVE")
    assert store.reserve_intent(intent, CONTEXT, fingerprint, "LIVE").state == "NEW"
    verifier = CountingVerifier()

    with pytest.raises(StoreUnavailable, match="LIVE reconciliation is disabled"):
        SQLiteReconciler(store, verifier).reconcile(intent.operation_id)
    assert verifier.calls == 0
    report = PaymentRecoveryWorker(store, stale_after=timedelta(0)).run_once()
    assert report.scanned == 1
    assert report.recovered_not_dispatched == 1
    assert verifier.calls == 0
    assert store.audit_operation(intent.operation_id).state == "REJECTED"


def test_reconciliation_verifier_must_match_persisted_mode(tmp_path):
    database = tmp_path / "sandbox-reconcile-route.sqlite"
    store = SQLitePaymentStore(database, trusted_issuer_id="mode_issuer", clock=lambda: NOW)
    intent = _intent("sandbox_wrong_verifier")
    fingerprint = _fingerprint(intent, CONTEXT, "SANDBOX")
    assert store.reserve_intent(intent, CONTEXT, fingerprint, "SANDBOX").state == "NEW"
    verifier = CountingVerifier()

    with pytest.raises(StoreUnavailable, match="provider verifier does not match"):
        SQLiteReconciler(store, verifier).reconcile(intent.operation_id)
    assert verifier.calls == 0


def test_retrying_operation_in_a_different_mode_never_uses_second_adapter(tmp_path):
    database = tmp_path / "cross-mode-retry.sqlite"
    store = SQLitePaymentStore(database, trusted_issuer_id="mode_issuer", clock=lambda: NOW)
    intent = _intent("cross_mode_retry")
    _seed(database, intent, "MOCK")
    mock_adapter = CountingAdapter()
    mock_gateway = PaymentGateway(
        PaymentPolicy(store, clock=lambda: NOW),
        store,
        store,
        mock_adapter,
        provider_mode=ProviderModeConfig.mock(),
    )
    first = mock_gateway.submit(intent, CONTEXT)
    assert first.status == "COMPLETED"
    assert mock_adapter.calls == 1

    sandbox_adapter = CountingSandboxAdapter()
    sandbox_gateway = PaymentGateway(
        PaymentPolicy(store, clock=lambda: NOW),
        store,
        store,
        sandbox_adapter,
        enable_sandbox_execution=True,
        provider_mode=ProviderModeConfig.sandbox(),
    )
    retry = sandbox_gateway.submit(intent, CONTEXT)

    assert retry.status == "REJECTED"
    assert retry.reason_codes == ("OPERATION_CONFLICT",)
    assert sandbox_adapter.calls == 0
