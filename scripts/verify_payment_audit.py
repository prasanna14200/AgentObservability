"""Verify the local payment telemetry hash chain without printing event payloads."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentsentinel.observability.payment_audit import PaymentAuditChain, PaymentAuditUnavailable  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=ROOT / "data" / "collected" / "payment_telemetry.sqlite",
        help="local payment telemetry SQLite path",
    )
    args = parser.parse_args()
    try:
        result = PaymentAuditChain(args.db).verify()
    except PaymentAuditUnavailable:
        print("Payment audit verification unavailable.")
        return 2
    if result.valid:
        print(f"VALID: {result.records_checked} records checked.")
        return 0
    print(
        "INVALID: "
        f"{result.records_checked} records checked; "
        f"first broken sequence={result.first_broken_sequence}; "
        f"reason={result.reason_code}."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
