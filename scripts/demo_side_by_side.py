"""Controlled offline simulation: one poisoned proposal, two execution paths.

This module is the ONLY direct-to-mock payment path. It never loads .env,
constructs a Sandbox client, or calls PayPal.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentsentinel.payment_gateway import (  # noqa: E402
    AdapterOutcome, PaymentGateway, TestOnlyAdapter, TrustedGatewayEntryPoint,
)
from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy  # noqa: E402
from agentsentinel.payment_sqlite import SQLitePaymentStore  # noqa: E402
from agentsentinel.purchase_agent.contracts import (  # noqa: E402
    Proposal, PurchaseRequest, ToolResult, load_catalog,
)
from agentsentinel.purchase_agent.graph import PurchaseAgent  # noqa: E402
from agentsentinel.purchase_agent.scripted import ScriptedLLM  # noqa: E402
from agentsentinel.purchase_gateway_bridge import GatewayPurchaseClient, operation_id_for  # noqa: E402
from agentsentinel.schema.payment import Money, PaymentAuthorization  # noqa: E402

CATALOG = ROOT / "data" / "catalog" / "day9_products.json"
DEMO_ORDER = "ORDER_DEMO_001"
DEMO_CONTEXT = AuthenticatedScope(principal_id="demo_user", scope_id="demo_scope")
DEMO_ISSUER = "demo_application"


class MockPayPalAdapter(TestOnlyAdapter):
    """Counts fake money only. Real credentials or transports are rejected."""

    def __init__(self, *, client_id=None, client_secret=None, transport=None) -> None:
        if any(value is not None for value in (client_id, client_secret, transport)):
            raise ValueError("mock adapter refuses credentials and transports")
        self.calls = 0
        self.money_moved = Decimal("0.00")

    def execute(self, intent) -> AdapterOutcome:
        self.calls += 1
        if intent.action == "capture_order":
            self.money_moved += intent.money.amount
        return AdapterOutcome(status="COMPLETED", provider_order_id="MOCK_ORDER_001",
                              provider_capture_id="MOCK_CAPTURE_001" if intent.action == "capture_order" else None)

    def execute_unchecked(self, proposal: Proposal) -> ToolResult:
        """Intentionally unsafe route, used only for the comparison simulation."""
        self.calls += 1
        if proposal.action == "capture_order":
            self.money_moved += proposal.amount_usd
        return ToolResult(decision="ALLOW", status="COMPLETED",
                          reason_code="UNPROTECTED_MOCK_ONLY")


class UnprotectedDemoToolClient:
    """Unprotected proposal client; keep this class inside this demo file only."""

    def __init__(self, adapter: MockPayPalAdapter) -> None:
        if not isinstance(adapter, MockPayPalAdapter):
            raise TypeError("only the offline mock adapter is accepted")
        self._adapter = adapter

    def propose_create_order(self, proposal: Proposal) -> ToolResult:
        return self._adapter.execute_unchecked(proposal)

    def propose_capture_order(self, proposal: Proposal) -> ToolResult:
        return self._adapter.execute_unchecked(proposal)


class DemoContextProvider:
    def current_context(self) -> AuthenticatedScope:
        return DEMO_CONTEXT


def _seed_trusted_demo_authorization(db: Path, run_id: str) -> None:
    """Script-only trusted setup for the original $1 catalog offer, not agent code."""
    now = datetime.now(timezone.utc)
    operation_id = operation_id_for(run_id, "capture_order")
    import hashlib
    record = PaymentAuthorization(
        authorization_id="auth_" + operation_id, issuer_id=DEMO_ISSUER,
        scope_id=DEMO_CONTEXT.scope_id,
        intent_id="intent_" + hashlib.sha256(operation_id.encode("ascii")).hexdigest()[:24],
        operation_id=operation_id, merchant_id="ABCDEFGH", money=Money(amount="1.00", currency="USD"),
        action="capture_order", target_order_id=DEMO_ORDER,
        issued_at=now - timedelta(minutes=1), expires_at=now + timedelta(minutes=10),
        status="issued", record_origin="application_issued",
    )
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("""INSERT INTO authorizations
            (authorization_id,issuer_id,principal_id,scope_id,intent_id,operation_id,merchant_id,
             amount_text,currency,action,target_order_id,issued_at,expires_at,status,closed_at,record_origin)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record.authorization_id, record.issuer_id, DEMO_CONTEXT.principal_id, record.scope_id,
             record.intent_id, record.operation_id, record.merchant_id, str(record.money.amount),
             record.money.currency, record.action, record.target_order_id,
             record.issued_at.isoformat(), record.expires_at.isoformat(), record.status,
             None, record.record_origin))


def protected_client(db: Path, *, run_id: str, adapter: MockPayPalAdapter) -> GatewayPurchaseClient:
    store = SQLitePaymentStore(db, trusted_issuer_id=DEMO_ISSUER)
    gateway = PaymentGateway(PaymentPolicy(store), store, store, adapter)
    client = GatewayPurchaseClient(TrustedGatewayEntryPoint(gateway, DemoContextProvider()),
                                   scope_id=DEMO_CONTEXT.scope_id, run_id=run_id)
    _seed_trusted_demo_authorization(db, run_id)
    return client


def demo_request() -> PurchaseRequest:
    return PurchaseRequest(
        user_request="Capture my approved notebook order for no more than USD 2.00.",
        product_id="notebook", budget_usd=Decimal("2.00"),
        action="capture_order", target_order_id=DEMO_ORDER,
    )


def run_comparison() -> list[tuple[str, object, Decimal]]:
    catalog = load_catalog(CATALOG)
    request = demo_request()
    llm = ScriptedLLM(naive_obedient=True)
    unprotected_adapter = MockPayPalAdapter()
    unprotected = PurchaseAgent(llm, UnprotectedDemoToolClient(unprotected_adapter), catalog).run(request)
    with TemporaryDirectory(prefix="agentsentinel_day9_") as directory:
        protected_adapter = MockPayPalAdapter()
        client = protected_client(Path(directory) / "payment.sqlite", run_id="day9_demo", adapter=protected_adapter)
        protected = PurchaseAgent(llm, client, catalog).run(request)
        return [("Unprotected mock", unprotected, unprotected_adapter.money_moved),
                ("AgentSentinel", protected, protected_adapter.money_moved)]


def main() -> int:
    print("CONTROLLED SIMULATION - no PayPal transaction or real money")
    print("| Path | Proposed action | Merchant | Amount USD | Decision | Status | Reason | Mock money moved USD |")
    print("|---|---|---|---:|---|---|---|---:|")
    for name, run, moved in run_comparison():
        proposal, result = run.proposal, run.result
        print(f"| {name} | {proposal.action} | {proposal.merchant_id} | {proposal.amount_usd:.2f} "
              f"| {result.decision} | {result.status} | {result.reason_code} | {moved:.2f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
