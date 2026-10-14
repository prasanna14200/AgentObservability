"""Manual walking skeleton. Run commands separately; never import into an agent."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import webbrowser
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from issue_authorization import (  # noqa: E402
    DEMO_DB, DEMO_ISSUER, ROOT, confirm_local_user, demo_context,
    demo_intent, issue_after_local_confirmation,
)

from agentsentinel.payment_gateway import PaymentGateway, TrustedGatewayEntryPoint  # noqa: E402
from agentsentinel.payment_policy import PaymentPolicy  # noqa: E402
from agentsentinel.payment_sandbox_adapter import SandboxPaymentAdapter, SandboxStatusVerifier  # noqa: E402
from agentsentinel.payment_sqlite import SQLitePaymentStore, SQLiteReconciler  # noqa: E402
from agentsentinel.paypal_sandbox import (  # noqa: E402
    ID_RE, PayPalSandboxClient, SandboxConfig, SandboxError, UncertainOutcome, amount_usd, read_local_env,
)

OPERATION_STATES = {"RESERVED", "CLAIMED", "COMPLETED", "REJECTED", "REVIEW_REQUIRED", "UNCERTAIN"}
ORDER_STATES = {"CREATED", "SAVED", "APPROVED", "VOIDED", "COMPLETED", "PAYER_ACTION_REQUIRED"}
CAPTURE_STATES = {"COMPLETED", "PENDING", "DECLINED", "FAILED", "REFUNDED", "PARTIALLY_REFUNDED"}


class DemoSession:
    def current_context(self):
        return demo_context()


def _local_snapshot(db: Path, operation_id: str) -> dict:
    """Read only the bounded metadata needed for a phase view; never mutate SQLite."""
    if not ID_RE.fullmatch(operation_id) or not db.is_file():
        raise ValueError("Invalid or unknown operation")
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""SELECT operation_id,action,state,merchant_id,amount_text,currency,target_order_id,
            provider_order_id,provider_capture_id,updated_at,
            attempt_id IS NOT NULL AS claim_recorded,
            idempotency_key IS NOT NULL AS request_id_recorded
            FROM operations WHERE operation_id=?""", (operation_id,)).fetchone()
        if row is None or row["action"] not in {"create_order", "capture_order"} or row["state"] not in OPERATION_STATES:
            raise ValueError("Unknown or invalid operation")
        order_id = row["provider_order_id"] or row["target_order_id"]
        if order_id is not None and not ID_RE.fullmatch(order_id):
            raise ValueError("Invalid stored order identifier")
        if row["provider_capture_id"] is not None and not ID_RE.fullmatch(row["provider_capture_id"]):
            raise ValueError("Invalid stored capture identifier")
        related = conn.execute("""SELECT state,provider_capture_id FROM operations
            WHERE action='capture_order' AND target_order_id=?""", (order_id,)).fetchall() if order_id else []
        for item in related:
            if item["state"] not in OPERATION_STATES:
                raise ValueError("Invalid related capture state")
        updated = datetime.fromisoformat(row["updated_at"]).astimezone(timezone.utc)
        return {**dict(row), "related_captures": [dict(item) for item in related],
                "updated_at": updated.isoformat()}
    finally:
        conn.close()


def _phase_view(local: dict, *, order_status: str | None = None,
                capture_status: str | None = None) -> dict:
    """A completed create action says nothing about buyer approval or capture."""
    if local["action"] == "create_order":
        creation = ("CREATE_ACKNOWLEDGED" if local["state"] == "COMPLETED" and local["provider_order_id"]
                    else "CREATE_OUTCOME_UNCERTAIN" if local["state"] in {"CLAIMED", "UNCERTAIN"}
                    else "NOT_CREATED" if local["state"] in {"REJECTED", "REVIEW_REQUIRED"}
                    else "IN_PROGRESS")
    else:
        creation = "EXISTING_ORDER_NOT_REVERIFIED"
    captures = local["related_captures"]
    if not captures:
        capture = "NOT_ATTEMPTED_LOCALLY"
    elif any(item["state"] == "COMPLETED" and item["provider_capture_id"] for item in captures):
        capture = "CAPTURE_RESPONSE_COMPLETED"
    elif any(item["state"] in {"CLAIMED", "UNCERTAIN"} for item in captures):
        capture = "CAPTURE_OUTCOME_UNCERTAIN"
    else:
        capture = "NO_COMPLETED_LOCAL_CAPTURE"
    if capture_status == "COMPLETED":
        capture = ("PROVIDER_VERIFIED_COMPLETED" if captures
                   else "PROVIDER_CAPTURE_WITHOUT_LOCAL_OPERATION")
    buyer = ("PROVIDER_APPROVED" if order_status == "APPROVED"
             else "AWAITING_BUYER" if order_status in {"CREATED", "PAYER_ACTION_REQUIRED"}
             else "NOT_SEPARATELY_OBSERVED" if order_status == "COMPLETED"
             else "UNVERIFIED")
    return {"order_creation_state": creation, "buyer_approval_state": buyer, "capture_state": capture}


def _safe_result(result, local: dict) -> dict:
    return {"operation_id": result.operation_id, "operation_action": local["action"],
            "local_operation_state": local["state"],
            "decision": result.decision.outcome if result.decision else None,
            "reason_codes": result.reason_codes,
            "order_id": local["provider_order_id"] or local["target_order_id"],
            "capture_id": local["provider_capture_id"], **_phase_view(local)}


def _safe_failure_code(exc: Exception) -> str:
    """Report only locally defined categories; provider bodies and exception text stay private."""
    if isinstance(exc, UncertainOutcome):
        return "NETWORK_OUTCOME_UNKNOWN"
    if isinstance(exc, SandboxError):
        match = re.fullmatch(r"Sandbox (OAuth failed|GET request failed|POST request failed) \(HTTP ([1-5][0-9]{2})\)", str(exc))
        if match:
            operation = {"OAuth failed": "OAUTH", "GET request failed": "GET",
                         "POST request failed": "POST"}[match.group(1)]
            return f"{operation}_HTTP_{match.group(2)}"
        if str(exc) in {"PayPal returned invalid JSON", "PayPal returned an invalid response shape",
                        "PayPal response exceeded size limit"}:
            return "INVALID_PROVIDER_RESPONSE"
        if str(exc) in {"PAYPAL_ENVIRONMENT must be sandbox",
                        "Sandbox client ID, secret, and merchant ID are required in ignored .env",
                        "Invalid Sandbox merchant ID"}:
            return "SANDBOX_CONFIGURATION_INVALID"
        return "SANDBOX_OPERATION_UNAVAILABLE"
    if isinstance(exc, ValueError):
        return "INVALID_INPUT_OR_RESPONSE"
    return "LOCAL_OR_UNEXPECTED_ERROR"


def _http_status(code: str, method: str) -> int | None:
    match = re.fullmatch(rf"{method}_HTTP_([1-5][0-9]{{2}})", code)
    return int(match.group(1)) if match else None


def _read_only_diagnostic(local: dict, client: PayPalSandboxClient, merchant_id: str) -> dict:
    """Only PayPal GETs (plus OAuth) and SQLite reads; never issue payment POSTs."""
    output = {
        "operation_id": local["operation_id"], "operation_action": local["action"],
        "local_operation_state": local["state"], "local_updated_at_utc": local["updated_at"],
        "local_claim_recorded": bool(local["claim_recorded"]),
        "local_request_id_recorded": bool(local["request_id_recorded"]),
        "local_order_id_present": bool(local["provider_order_id"] or local["target_order_id"]),
        "configured_merchant_matches_local": merchant_id == local["merchant_id"],
        "provider_http_status": None, "provider_order_status": None,
        "provider_capture_http_status": None, "provider_capture_status": None,
        "provider_capture_present": False, "provider_binding": "NOT_CHECKED",
        "provider_error_code": None, **_phase_view(local),
    }
    order_id = local["provider_order_id"] or local["target_order_id"]
    if not order_id:
        output["provider_error_code"] = "NO_LOCAL_ORDER_ID"
        return output
    try:
        order = client.get_order(order_id)
    except Exception as exc:
        code = _safe_failure_code(exc)
        output.update(provider_http_status=_http_status(code, "GET"), provider_error_code=code)
        return output

    output["provider_http_status"] = 200
    status = order.get("status")
    output["provider_order_status"] = status if status in ORDER_STATES else "UNKNOWN"
    try:
        amount_matches = Decimal(order.get("amount")) == Decimal(local["amount_text"])
    except (InvalidOperation, TypeError, ValueError):
        amount_matches = False
    binding = (order.get("order_id") == order_id and order.get("merchant_id") == local["merchant_id"]
               and order.get("currency") == local["currency"] and amount_matches)
    output["provider_binding"] = "MATCH" if binding else "MISMATCH_OR_INCOMPLETE"
    if not binding:
        output["provider_error_code"] = "ORDER_BINDING_UNVERIFIED"
        return output

    capture_id = order.get("capture_id")
    output["provider_capture_present"] = bool(capture_id)
    if capture_id:
        if not isinstance(capture_id, str) or not ID_RE.fullmatch(capture_id):
            output["provider_error_code"] = "INVALID_PROVIDER_CAPTURE_ID"
            return output
        try:
            capture = client.get_capture(capture_id)
        except Exception as exc:
            code = _safe_failure_code(exc)
            output.update(provider_capture_http_status=_http_status(code, "GET"), provider_error_code=code)
            return output
        output["provider_capture_http_status"] = 200
        if capture.get("capture_id") != capture_id:
            output["provider_error_code"] = "CAPTURE_BINDING_UNVERIFIED"
            return output
        capture_status = capture.get("status")
        output["provider_capture_status"] = capture_status if capture_status in CAPTURE_STATES else "UNKNOWN"
    output.update(_phase_view(local, order_status=output["provider_order_status"],
                              capture_status=output["provider_capture_status"]))
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual AgentSentinel Sandbox gateway demo")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "capture", "reconcile", "diagnose", "open-approval"):
        part = sub.add_parser(name)
        part.add_argument("--operation-id", required=True)
        if name in {"create", "capture"}:
            part.add_argument("--amount", required=True, help="USD 0.01-10.00; use 1.00 for demo")
        if name == "capture":
            part.add_argument("--order-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "diagnose":
            # This branch never constructs SQLitePaymentStore or writes the DB.
            local = _local_snapshot(DEMO_DB, args.operation_id)
            config = SandboxConfig.from_values(read_local_env(ROOT / ".env"))
            diagnostic = _read_only_diagnostic(local, PayPalSandboxClient(config), config.merchant_id)
            print(json.dumps(diagnostic, sort_keys=True))
            return 0 if diagnostic["provider_http_status"] == 200 and not diagnostic["provider_error_code"] else 2
        # Deliberately executed only by a human invoking this CLI. No import reads .env.
        config = SandboxConfig.from_values(read_local_env(ROOT / ".env"))
        client = PayPalSandboxClient(config)
        DEMO_DB.parent.mkdir(parents=True, exist_ok=True)
        store = SQLitePaymentStore(DEMO_DB, trusted_issuer_id=DEMO_ISSUER)

        if args.command == "open-approval":
            audit = store.audit_operation(args.operation_id)
            if audit is None or audit.state != "COMPLETED" or audit.provider_order_id is None:
                raise SandboxError("A completed create operation is required")
            try:
                link = client.approval_link(audit.provider_order_id)
            except SandboxError as exc:
                # Only these exact, locally defined categories may reach stdout.
                if str(exc) == "Order is not awaiting buyer approval":
                    print("Approval unavailable: order is not awaiting buyer action. Run reconcile.")
                    return 2
                if str(exc) == "No validated Sandbox buyer approval link found":
                    print("Approval unavailable: no validated Sandbox link was returned. Run reconcile.")
                    return 2
                raise
            if not webbrowser.open(link):
                print("Browser launch was not confirmed; no approval action was performed by this script.")
                return 2
            print("Opened the validated Sandbox approval page; buyer approval is manual.")
            return 0

        if args.command == "reconcile":
            audit = store.audit_operation(args.operation_id)
            if audit is None:
                raise SandboxError("Unknown operation")
            if audit.state in {"RESERVED", "CLAIMED", "UNCERTAIN"}:
                SQLiteReconciler(store, SandboxStatusVerifier(client)).reconcile(args.operation_id)
            local = _local_snapshot(DEMO_DB, args.operation_id)
            diagnostic = _read_only_diagnostic(local, client, config.merchant_id)
            print(json.dumps(diagnostic, sort_keys=True))
            return 0 if diagnostic["provider_http_status"] == 200 and not diagnostic["provider_error_code"] else 2

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
        print(json.dumps(_safe_result(result, _local_snapshot(DEMO_DB, intent.operation_id)), sort_keys=True))
        return 0 if result.status == "COMPLETED" else 2
    except Exception as exc:
        # Never echo the raw exception; custom transports could contain secrets.
        print(f"Sandbox demo stopped [{_safe_failure_code(exc)}]. No automatic retry.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
