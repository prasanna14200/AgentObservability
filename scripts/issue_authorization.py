"""Demo-only local user approval. Never expose this module to an AI agent."""

from __future__ import annotations

import hashlib
import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentsentinel.payment_policy import AuthenticatedScope  # noqa: E402
from agentsentinel.payment_sqlite import SQLitePaymentStore  # noqa: E402
from agentsentinel.paypal_sandbox import SandboxConfig, amount_usd, read_local_env  # noqa: E402
from agentsentinel.schema.payment import Money, PaymentAuthorization, PaymentIntent  # noqa: E402

DEMO_PRINCIPAL = "demo_local_user"
DEMO_SCOPE = "demo_local_scope"
DEMO_ISSUER = "demo_local_application"
DEMO_DB = ROOT / "data" / "paypal_sandbox" / "gateway_demo.sqlite"


def demo_context() -> AuthenticatedScope:
    """A stubbed local session; production needs real authentication."""
    return AuthenticatedScope(principal_id=DEMO_PRINCIPAL, scope_id=DEMO_SCOPE)


def demo_intent(operation_id: str, merchant_id: str, amount: str, action: str,
                target_order_id: str | None = None) -> PaymentIntent:
    digest = hashlib.sha256(operation_id.encode("ascii")).hexdigest()[:24]
    return PaymentIntent(
        intent_id="intent_" + digest, operation_id=operation_id,
        trace_id="trace_" + digest, scope_id=DEMO_SCOPE,
        money=Money(amount=amount, currency="USD"), merchant_id=merchant_id,
        purpose="Manual Sandbox demonstration", action=action,
        # A stable demo timestamp keeps repeat submissions of one operation identical.
        target_order_id=target_order_id, requested_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


def issue_after_local_confirmation(db: Path, intent: PaymentIntent) -> PaymentAuthorization:
    """Trusted demo caller must obtain interactive confirmation before calling."""
    now = datetime.now(timezone.utc)
    digest = hashlib.sha256(intent.operation_id.encode("ascii")).hexdigest()[:24]
    record = PaymentAuthorization(
        authorization_id="authorization_" + digest, issuer_id=DEMO_ISSUER,
        scope_id=DEMO_SCOPE, intent_id=intent.intent_id,
        operation_id=intent.operation_id, merchant_id=intent.merchant_id,
        money=intent.money, action=intent.action, target_order_id=intent.target_order_id,
        issued_at=now, expires_at=now + timedelta(minutes=10),
        status="issued", record_origin="application_issued",
    )
    conn = sqlite3.connect(db, timeout=1, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""INSERT INTO authorizations
            (authorization_id,issuer_id,principal_id,scope_id,intent_id,operation_id,merchant_id,
             amount_text,currency,action,target_order_id,issued_at,expires_at,status,closed_at,record_origin)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record.authorization_id, record.issuer_id, DEMO_PRINCIPAL, record.scope_id,
             record.intent_id, record.operation_id, record.merchant_id, str(record.money.amount),
             record.money.currency, record.action, record.target_order_id,
             record.issued_at.isoformat(), record.expires_at.isoformat(), record.status,
             None, record.record_origin))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return record


def confirm_local_user(intent: PaymentIntent) -> bool:
    """The local human must approve exact fields; no noninteractive bypass."""
    if not sys.stdin.isatty():
        return False
    print(f"Approve Sandbox {intent.action}: USD {intent.money.amount:.2f}, "
          f"merchant {intent.merchant_id}, operation {intent.operation_id}, "
          f"target {intent.target_order_id or 'none'}?")
    return input("Type APPROVE to issue a 10-minute demo authorization: ").strip() == "APPROVE"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interactive demo-only Sandbox authorization issuance")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--amount", required=True)
    parser.add_argument("--action", choices=("create_order", "capture_order"), required=True)
    parser.add_argument("--target-order-id")
    args = parser.parse_args(argv)
    try:
        config = SandboxConfig.from_values(read_local_env(ROOT / ".env"))
        value = amount_usd(args.amount)
        intent = demo_intent(args.operation_id, config.merchant_id, value,
                             args.action, args.target_order_id)
        DEMO_DB.parent.mkdir(parents=True, exist_ok=True)
        SQLitePaymentStore(DEMO_DB, trusted_issuer_id=DEMO_ISSUER)
        if not confirm_local_user(intent):
            print("No authorization issued.")
            return 2
        issue_after_local_confirmation(DEMO_DB, intent)
        print("Bound, expiring demo authorization issued for the operation.")
        return 0
    except Exception:
        print("Demo authorization could not be issued.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
