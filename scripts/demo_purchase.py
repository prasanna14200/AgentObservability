"""Manual walking skeleton. Run commands separately; never import into an agent."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser

from issue_authorization import (  # noqa: E402
    DEMO_DB, DEMO_ISSUER, ROOT, confirm_local_user, demo_context,
    demo_intent, issue_after_local_confirmation,
)

from agentsentinel.payment_gateway import PaymentGateway, TrustedGatewayEntryPoint  # noqa: E402
from agentsentinel.payment_policy import PaymentPolicy  # noqa: E402
from agentsentinel.payment_sandbox_adapter import SandboxPaymentAdapter, SandboxStatusVerifier  # noqa: E402
from agentsentinel.payment_sqlite import SQLitePaymentStore, SQLiteReconciler  # noqa: E402
from agentsentinel.paypal_sandbox import (  # noqa: E402
    PayPalSandboxClient, SandboxConfig, SandboxError, amount_usd, read_local_env,
)


class DemoSession:
    def current_context(self):
        return demo_context()


def _safe_result(result, audit):
    return {
        "operation_id": result.operation_id, "decision": result.decision.outcome if result.decision else None,
        "status": result.status, "reasons": result.reason_codes,
        "order_id": audit.provider_order_id if audit else None,
        "capture_id": audit.provider_capture_id if audit else None,
        "audit_state": audit.state if audit else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual AgentSentinel Sandbox gateway demo")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "capture", "reconcile", "open-approval"):
        part = sub.add_parser(name)
        part.add_argument("--operation-id", required=True)
        if name in {"create", "capture"}:
            part.add_argument("--amount", required=True, help="USD 0.01-10.00; use 1.00 for demo")
        if name == "capture":
            part.add_argument("--order-id", required=True)
    args = parser.parse_args(argv)
    try:
        # Deliberately executed only by a human invoking this CLI. No import reads .env.
        config = SandboxConfig.from_values(read_local_env(ROOT / ".env"))
        client = PayPalSandboxClient(config)
        DEMO_DB.parent.mkdir(parents=True, exist_ok=True)
        store = SQLitePaymentStore(DEMO_DB, trusted_issuer_id=DEMO_ISSUER)

        if args.command == "open-approval":
            audit = store.audit_operation(args.operation_id)
            if audit is None or audit.state != "COMPLETED" or audit.provider_order_id is None:
                raise SandboxError("A completed create operation is required")
            webbrowser.open(client.approval_link(audit.provider_order_id))
            print("Opened the validated Sandbox approval page; buyer approval is manual.")
            return 0

        if args.command == "reconcile":
            audit = store.audit_operation(args.operation_id)
            if audit is None:
                raise SandboxError("Unknown operation")
            if audit.state in {"RESERVED", "CLAIMED", "UNCERTAIN"}:
                result = SQLiteReconciler(store, SandboxStatusVerifier(client)).reconcile(args.operation_id)
                audit = store.audit_operation(args.operation_id)
                print(json.dumps(_safe_result(result, audit), sort_keys=True))
            else:
                print(json.dumps({"operation_id": audit.operation_id, "audit_state": audit.state,
                                  "order_id": audit.provider_order_id,
                                  "capture_id": audit.provider_capture_id}, sort_keys=True))
            if audit and audit.provider_order_id:
                order = client.get_order(audit.provider_order_id)
                output = {"order_status": order["status"]}
                if audit.provider_capture_id:
                    capture = client.get_capture(audit.provider_capture_id)
                    output["capture_status"] = capture["status"]
                print(json.dumps(output, sort_keys=True))
            return 0

        value = amount_usd(args.amount)
        target = args.order_id if args.command == "capture" else None
        intent = demo_intent(args.operation_id, config.merchant_id, value,
                             "capture_order" if args.command == "capture" else "create_order", target)
        existing = store.audit_operation(intent.operation_id)
        if existing is None and store.find(demo_context(), intent.intent_id, intent.operation_id) is None:
            if target:
                order = client.get_order(target)  # Read-only approval and binding check.
                if (order["status"] != "APPROVED" or order["amount"] != value
                        or order["currency"] != "USD" or order["merchant_id"] != config.merchant_id):
                    raise SandboxError("Buyer approval or order binding is missing")
            if not confirm_local_user(intent):
                print("No authorization issued.")
                return 2
            issue_after_local_confirmation(DEMO_DB, intent)
        gateway = PaymentGateway(PaymentPolicy(store), store, store, SandboxPaymentAdapter(client),
                                 enable_sandbox_execution=True)
        result = TrustedGatewayEntryPoint(gateway, DemoSession()).submit(intent)
        print(json.dumps(_safe_result(result, store.audit_operation(intent.operation_id)), sort_keys=True))
        return 0 if result.status == "COMPLETED" else 2
    except Exception:
        # Provider bodies, URLs, credentials, and local SQL errors never reach stdout.
        print("Sandbox demo could not complete; inspect sanitized operation state with reconcile.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
