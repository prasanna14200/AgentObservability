"""Reproducible, offline reliability latency samples for Day 13.

Run from the repository root with:
    .\\.venv\\Scripts\\python.exe scripts/measure_day13_reliability.py

Only temporary SQLite files and in-process test adapters are used.
"""

from __future__ import annotations

import platform
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentsentinel.approval_service import ApprovalStore  # noqa: E402
from agentsentinel.observability.collector import TraceCollector  # noqa: E402
from agentsentinel.observability.payment_audit import PaymentAuditChain  # noqa: E402
from agentsentinel.observability.payment_events import (  # noqa: E402
    PaymentTelemetryPublisher,
    build_payment_event,
)
from agentsentinel.payment_gateway import AdapterOutcome, PaymentGateway, TestOnlyAdapter  # noqa: E402
from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy  # noqa: E402
from agentsentinel.payment_sqlite import SQLitePaymentStore  # noqa: E402
from agentsentinel.schema.payment import Money, PaymentIntent  # noqa: E402

SAMPLES = 100
NOW = datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc)
CONTEXT = AuthenticatedScope(principal_id="day13_benchmark", scope_id="day13_benchmark")


def make_intent(operation_id: str) -> PaymentIntent:
    return PaymentIntent(
        intent_id=f"intent_{operation_id}",
        operation_id=operation_id,
        trace_id=f"trace_{operation_id}",
        scope_id=CONTEXT.scope_id,
        money=Money(amount="1.00", currency="USD"),
        merchant_id="DAY13MERCHANT",
        purpose="Fixed offline benchmark input",
        action="create_order",
        requested_at=NOW,
    )


def event(index: int):
    operation_id = f"operation_event_{index:04d}"
    return build_payment_event(
        event_type="PAYMENT_PROPOSED",
        trace_id=f"trace_event_{index:04d}",
        operation_id=operation_id,
        payment_intent_id=f"intent_{operation_id}",
        action="create_order",
        actor_role="application",
        timestamp=NOW,
    )


class BenchmarkAdapter(TestOnlyAdapter):
    def __init__(self, entered: Event | None = None, release: Event | None = None) -> None:
        self.calls = 0
        self.entered = entered
        self.release = release

    def execute(self, intent: PaymentIntent) -> AdapterOutcome:
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None and not self.release.wait(timeout=10):
            raise TimeoutError("offline benchmark adapter timed out")
        return AdapterOutcome(status="COMPLETED")


def report(name: str, values_ns: list[int]) -> None:
    ordered = sorted(values_ns)
    p95_index = max(0, (95 * len(ordered) + 99) // 100 - 1)
    print(
        f"{name}: n={len(ordered)} min_ms={ordered[0] / 1_000_000:.3f} "
        f"median_ms={statistics.median(ordered) / 1_000_000:.3f} "
        f"p95_ms={ordered[p95_index] / 1_000_000:.3f}"
    )


def main() -> int:
    print(f"python={platform.python_version()} os={platform.platform()} samples={SAMPLES}")
    print("environment=temporary SQLite; offline only; no PayPal transport constructed")
    with tempfile.TemporaryDirectory(prefix="agentsentinel_day13_measure_") as temporary:
        root = Path(temporary)
        db = root / "payment.sqlite"
        payment_store = SQLitePaymentStore(
            db,
            trusted_issuer_id="trusted_approval_service",
            clock=lambda: NOW,
        )
        gateway = PaymentGateway(
            PaymentPolicy(payment_store, clock=lambda: NOW),
            payment_store,
            payment_store,
        )
        decision_samples: list[int] = []
        for index in range(SAMPLES):
            started = time.perf_counter_ns()
            result = gateway.submit(make_intent(f"operation_decision_{index:04d}"), CONTEXT)
            decision_samples.append(time.perf_counter_ns() - started)
            if result.decision is None or result.decision.outcome != "BLOCK":
                raise RuntimeError("offline gateway benchmark did not fail closed")
        report("gateway_block_decision", decision_samples)

        approvals = ApprovalStore(db, clock=lambda: NOW)
        lookup_record = approvals.create_approval(
            intent=make_intent("operation_lookup_benchmark"),
            context=CONTEXT,
        )
        approval_samples: list[int] = []
        for _ in range(SAMPLES):
            started = time.perf_counter_ns()
            record = approvals.get_approval(lookup_record.approval_id)
            approval_samples.append(time.perf_counter_ns() - started)
            if record is None:
                raise RuntimeError("approval lookup benchmark record disappeared")
        report("approval_lookup", approval_samples)

        audit = PaymentAuditChain(root / "audit.sqlite")
        append_samples: list[int] = []
        for index in range(SAMPLES):
            started = time.perf_counter_ns()
            audit.append(event(index))
            append_samples.append(time.perf_counter_ns() - started)
        report("audit_append", append_samples)
        verify_samples: list[int] = []
        for _ in range(25):
            started = time.perf_counter_ns()
            result = audit.verify()
            verify_samples.append(time.perf_counter_ns() - started)
            if not result.valid or result.records_checked != SAMPLES:
                raise RuntimeError("audit verification benchmark failed")
        report("audit_verify_100_records", verify_samples)

        collector = TraceCollector(root / "collected")
        publisher = PaymentTelemetryPublisher(collector)
        publish_samples: list[int] = []
        for index in range(SAMPLES):
            started = time.perf_counter_ns()
            accepted = publisher.publish(event(index + SAMPLES))
            publish_samples.append(time.perf_counter_ns() - started)
            if not accepted:
                raise RuntimeError("telemetry publication benchmark rejected an event")
        report("telemetry_publish", publish_samples)

        concurrent_intent = make_intent("operation_concurrent_benchmark")
        request = approvals.create_approval(intent=concurrent_intent, context=CONTEXT)
        approvals.approve_approval(request.approval_id, request_fingerprint=request.request_fingerprint)
        concurrent_store = SQLitePaymentStore(
            db,
            trusted_issuer_id="trusted_approval_service",
            clock=lambda: NOW,
        )
        entered, release = Event(), Event()
        adapter = BenchmarkAdapter(entered, release)
        concurrent_gateway = PaymentGateway(
            PaymentPolicy(concurrent_store, clock=lambda: NOW),
            concurrent_store,
            concurrent_store,
            adapter,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(concurrent_gateway.submit, concurrent_intent, CONTEXT)
            if not entered.wait(timeout=10):
                raise RuntimeError("concurrent adapter did not start")
            started = time.perf_counter_ns()
            duplicate = concurrent_gateway.submit(concurrent_intent, CONTEXT)
            duplicate_ns = time.perf_counter_ns() - started
            release.set()
            first_result = first.result(timeout=10)
        if first_result.status != "COMPLETED" or duplicate.status != "UNCERTAIN" or adapter.calls != 1:
            raise RuntimeError("concurrent idempotency behavior did not match expected safety state")
        print(
            f"same_key_concurrency: first={first_result.status} concurrent_duplicate={duplicate.status} "
            f"duplicate_lookup_ms={duplicate_ns / 1_000_000:.3f} fake_executions={adapter.calls} "
            "paypal_transport_calls=0"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
