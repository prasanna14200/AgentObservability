"""Verify the documented public guard API from an isolated source copy."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_guard_public_api_runs_from_fresh_source_copy(tmp_path):
    repository = Path(__file__).resolve().parents[2]
    source_copy = tmp_path / "fresh_clone" / "src"
    source_copy.mkdir(parents=True)
    shutil.copytree(
        repository / "src" / "agentsentinel",
        source_copy / "agentsentinel",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    script = """
from datetime import datetime, timedelta, timezone
from agentsentinel import TrustedGatewayEntryPoint, guard
from agentsentinel.payment_gateway import AdapterOutcome, InMemoryOperationStore, PaymentGateway, TestOnlyAdapter
from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy
from agentsentinel.schema.payment import Money, PaymentAuthorization, PaymentIntent

now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
context = AuthenticatedScope(principal_id="fresh_user", scope_id="fresh_scope")
intent = PaymentIntent(
    intent_id="fresh_intent", operation_id="fresh_operation", trace_id="fresh_trace",
    scope_id=context.scope_id, money=Money(amount="1.00", currency="USD"),
    merchant_id="FRESHMERCHANT", purpose="Fresh source smoke test",
    action="create_order", requested_at=now,
)
record = PaymentAuthorization(
    authorization_id="fresh_authorization", issuer_id="fresh_issuer",
    scope_id=context.scope_id, intent_id=intent.intent_id,
    operation_id=intent.operation_id, merchant_id=intent.merchant_id,
    money=intent.money, action=intent.action, issued_at=now - timedelta(minutes=1),
    expires_at=now + timedelta(minutes=5), status="issued",
    record_origin="application_issued",
)

class Repository:
    def find(self, caller, intent_id, operation_id):
        return record

class Consumer:
    def claim(self, caller, request, decision):
        return record.binds_to(request) and decision.outcome == "ALLOW"

class Adapter(TestOnlyAdapter):
    def execute(self, request):
        return AdapterOutcome(status="COMPLETED")

class ContextProvider:
    def current_context(self):
        return context

policy = PaymentPolicy(Repository(), clock=lambda: now)
gateway = PaymentGateway(policy, InMemoryOperationStore(), Consumer(), Adapter())
entry_point = TrustedGatewayEntryPoint(gateway, ContextProvider())

@guard(entry_point)
def purchase(proposal):
    return proposal

result = purchase(intent)
assert result.status == "COMPLETED", result.model_dump_json()
assert result.decision.outcome == "ALLOW"
print(result.status)
"""
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")
        if key in os.environ
    }
    environment["PYTHONPATH"] = str(source_copy)
    environment["PYTHONNOUSERSITE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "COMPLETED"
