"""Explicit manual Sandbox commands. No command runs during pytest."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentsentinel.paypal_sandbox import (  # noqa: E402
    PayPalSandboxClient, SandboxConfig, SandboxError, UncertainOutcome,
    amount_usd, read_local_env, request_id,
)

JOURNAL = ROOT / "data" / "paypal_sandbox" / "operations.json"


def load_journal() -> dict:
    return json.loads(JOURNAL.read_text(encoding="utf-8")) if JOURNAL.exists() else {"create": {}, "capture": {}}


def save_journal(journal: dict) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    temp = JOURNAL.with_suffix(".tmp")
    temp.write_text(json.dumps(journal, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(JOURNAL)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual PayPal Sandbox REST spike")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--operation-id", required=True)
    create.add_argument("--amount", required=True, help="USD value, e.g. 1.00 (max 10.00)")
    for name in ("show", "open-approval", "capture", "reconcile"):
        command = sub.add_parser(name)
        command.add_argument("--operation-id", required=True, help="Original create operation ID")
        if name == "capture":
            command.add_argument("--capture-operation-id", required=True)
    args = parser.parse_args(argv)
    try:
        config = SandboxConfig.from_values(read_local_env(ROOT / ".env"))
        client = PayPalSandboxClient(config)
        journal = load_journal()
        if args.command == "create":
            value = amount_usd(args.amount)
            request_id(config.merchant_id, "create", args.operation_id)
            existing = journal["create"].get(args.operation_id)
            if existing:
                if existing["amount"] != value:
                    raise SandboxError("Operation ID already used for a different amount")
                if not existing.get("order_id"):
                    raise UncertainOutcome("Create outcome uncertain; inspect Sandbox before further action")
                print(json.dumps({"order_id": existing["order_id"], "status": "ALREADY_RECORDED"}))
                return 0
            journal["create"][args.operation_id] = {"amount": value, "order_id": None}
            save_journal(journal)
            result = client.create_order(args.operation_id, value)
            journal["create"][args.operation_id]["order_id"] = result["order_id"]
            save_journal(journal)
            print(json.dumps({"order_id": result["order_id"], "status": result["status"]}))
            return 0

        original = journal["create"].get(args.operation_id)
        if not original or not original.get("order_id"):
            raise SandboxError("Unknown or uncertain create operation; reconcile manually")
        order_id = original["order_id"]
        if args.command == "show":
            order = client.get_order(order_id)
            print(json.dumps({"order_id": order_id, "status": order["status"],
                              "capture_id": order["capture_id"], "capture_status": order["capture_status"]}))
        elif args.command == "open-approval":
            webbrowser.open(client.approval_link(order_id))
            print("Opened the validated Sandbox buyer approval page. Buyer must approve manually.")
        elif args.command == "capture":
            key = request_id(config.merchant_id, "capture", args.capture_operation_id)
            existing = journal["capture"].get(args.capture_operation_id)
            if existing:
                raise UncertainOutcome("Capture operation already recorded; use reconcile, never repeat POST automatically")
            order = client.get_order(order_id)
            if order["status"] != "APPROVED":
                raise SandboxError("Buyer approval pending or order not capturable")
            if (order["amount"], order["currency"], order["merchant_id"]) != (
                original["amount"], "USD", config.merchant_id
            ):
                raise SandboxError("Order amount, currency, or merchant mismatch")
            # Persist the operation before POST. A crash or timeout cannot lead to a blind rerun.
            journal["capture"][args.capture_operation_id] = {"order_id": order_id, "request_id": key,
                                                                 "state": "PENDING"}
            save_journal(journal)
            result = client.capture_order(order_id, args.capture_operation_id, original["amount"])
            journal["capture"][args.capture_operation_id].update(
                {"state": "RESPONSE_RECEIVED", "capture_id": result["capture_id"]})
            save_journal(journal)
            print(json.dumps({"order_id": order_id, "capture_id": result["capture_id"],
                              "capture_status": result["capture_status"]}))
        else:  # reconcile
            order = client.get_order(order_id)
            capture = client.get_capture(order["capture_id"]) if order["capture_id"] else None
            print(json.dumps({"order_id": order_id, "order_status": order["status"],
                              "capture_id": order["capture_id"],
                              "capture_status": capture["status"] if capture else None}))
        return 0
    except (SandboxError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        # Only locally defined, sanitized errors should reach this output.
        if isinstance(exc, (OSError, KeyError, json.JSONDecodeError)):
            print("Local operation journal error; inspect its path without sharing contents", file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
