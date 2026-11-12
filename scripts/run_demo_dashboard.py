"""Seed and serve the local-only AgentSentinel mock dashboard demonstration."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT, ROOT / "src", ROOT / "scripts", ROOT / "tests" / "unit"):
    sys.path.insert(0, str(import_path))


_DEMO_DEPENDENCIES_LOADED = False


def _demo_seeding_enabled() -> bool:
    return False


def _load_demo_dependencies() -> None:
    global _DEMO_DEPENDENCIES_LOADED, DEMO_CONTEXT
    if _DEMO_DEPENDENCIES_LOADED:
        return

    previous_approval_db = os.environ.get("APPROVAL_DB_PATH")
    os.environ["APPROVAL_DB_PATH"] = ":memory:"
    try:
        from demo_side_by_side import MockPayPalAdapter  # noqa: E402
        from scripts.demo_support import MERCHANT, SpyTransport  # noqa: E402

        from agentsentinel.approval_service import ApprovalRecord, ApprovalStore, create_app  # noqa: E402
        from agentsentinel.data.payment_synth_traces import generate_payment_traces  # noqa: E402
        from agentsentinel.detectors.payment import PaymentDetectorAdapter  # noqa: E402
        from agentsentinel.observability.collector import TraceCollector  # noqa: E402
        from agentsentinel.observability.payment_audit import PaymentAuditChain  # noqa: E402
        from agentsentinel.observability.payment_events import PaymentTelemetryPublisher  # noqa: E402
        from agentsentinel.payment_gateway import (  # noqa: E402
            ApprovalRequired,
            PaymentGateway,
            TrustedGatewayEntryPoint,
        )
        from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy  # noqa: E402
        from agentsentinel.payment_provider import ProviderModeConfig  # noqa: E402
        from agentsentinel.payment_sandbox_adapter import SandboxPaymentAdapter  # noqa: E402
        from agentsentinel.payment_sqlite import SQLitePaymentStore  # noqa: E402
        from agentsentinel.paypal_sandbox import PayPalSandboxClient, SandboxConfig  # noqa: E402
        from agentsentinel.purchase_gateway_bridge import ApprovedPaymentExecutor  # noqa: E402
        from agentsentinel.schema.payment import Money, PaymentIntent  # noqa: E402
    finally:
        if previous_approval_db is None:
            os.environ.pop("APPROVAL_DB_PATH", None)
        else:
            os.environ["APPROVAL_DB_PATH"] = previous_approval_db

    globals().update(
        {
            "ApprovalRecord": ApprovalRecord,
            "ApprovalRequired": ApprovalRequired,
            "ApprovalStore": ApprovalStore,
            "ApprovedPaymentExecutor": ApprovedPaymentExecutor,
            "AuthenticatedScope": AuthenticatedScope,
            "MERCHANT": MERCHANT,
            "MockPayPalAdapter": MockPayPalAdapter,
            "Money": Money,
            "PaymentAuditChain": PaymentAuditChain,
            "PaymentDetectorAdapter": PaymentDetectorAdapter,
            "PaymentGateway": PaymentGateway,
            "PaymentIntent": PaymentIntent,
            "PaymentPolicy": PaymentPolicy,
            "PaymentTelemetryPublisher": PaymentTelemetryPublisher,
            "PayPalSandboxClient": PayPalSandboxClient,
            "ProviderModeConfig": ProviderModeConfig,
            "SandboxConfig": SandboxConfig,
            "SandboxPaymentAdapter": SandboxPaymentAdapter,
            "SQLitePaymentStore": SQLitePaymentStore,
            "SpyTransport": SpyTransport,
            "TraceCollector": TraceCollector,
            "TrustedGatewayEntryPoint": TrustedGatewayEntryPoint,
            "create_app": create_app,
            "generate_payment_traces": generate_payment_traces,
        }
    )
    DEMO_CONTEXT = AuthenticatedScope(
        principal_id="dashboard_demo_operator",
        scope_id="dashboard_demo_scope",
    )
    _DEMO_DEPENDENCIES_LOADED = True


_configured_demo_parent = os.environ.get("AGENTSENTINEL_DEMO_DATA_PARENT")
DEMO_DATA_PARENT = (
    Path(_configured_demo_parent).expanduser()
    if _configured_demo_parent
    else None
)
DEMO_MARKER = ".agentsentinel-dashboard-demo"
DEMO_MARKER_CONTENT = "AgentSentinel isolated local mock dashboard demo\n"
DEMO_ISSUER = "dashboard_demo_issuer"


class DemoClock:
    def __init__(self, start: datetime) -> None:
        self._next = start

    def __call__(self) -> datetime:
        current = self._next
        self._next += timedelta(milliseconds=1)
        return current


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    trace_id: str
    decision: str
    status: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class DemoSeed:
    root: Path
    database_path: Path
    telemetry_path: Path
    failures_path: Path
    pending_approval_id: str
    scenarios: tuple[ScenarioResult, ...]
    mock_adapter: MockPayPalAdapter
    spy_transport: SpyTransport


class DemoContextProvider:
    def current_context(self) -> AuthenticatedScope:
        return DEMO_CONTEXT


def _intent(
    key: str, *, amount: str = "4.00", merchant: str | None = None
) -> PaymentIntent:
    if merchant is None:
        merchant = MERCHANT
    return PaymentIntent(
        intent_id=f"demo_intent_{key}",
        operation_id=f"demo_op_{key}",
        trace_id=f"demo_trace_{key}",
        scope_id=DEMO_CONTEXT.scope_id,
        money=Money(amount=amount, currency="USD"),
        merchant_id=merchant,
        purpose="Isolated local mock dashboard demonstration",
        action="create_order",
        requested_at=datetime.now(timezone.utc),
    )


def _approve_for_demo(
    approvals: ApprovalStore,
    intent: PaymentIntent,
    provider_mode: str = "MOCK",
) -> ApprovalRecord:
    record = approvals.create_approval(
        intent=intent,
        context=DEMO_CONTEXT,
        reason_codes=("DEMO_SEEDED_AUTHORIZATION",),
        provider_mode=provider_mode,
    )
    return approvals.approve_approval(
        record.approval_id,
        request_fingerprint=record.request_fingerprint,
    )


def _result(
    name: str,
    trace_id: str,
    decision: str,
    status: str,
    reasons: tuple[str, ...],
) -> ScenarioResult:
    return ScenarioResult(name, trace_id, decision, status, reasons)


def _write_advisory_trace(
    collector: TraceCollector, now: datetime
) -> tuple[str, tuple[str, ...]]:
    traces = generate_payment_traces(normal_count=70, per_scenario=1)
    adapter = PaymentDetectorAdapter()
    normal_traces = [trace for trace in traces if trace.label == "normal"]
    retry_trace = next(trace for trace in traces if trace.scenario == "retry storm")
    rules_detection = adapter.rules(retry_trace.events)
    forest_detection = adapter.isolation_forest(
        retry_trace.events,
        adapter.fit_isolation_forest(normal_traces),
    )
    if not rules_detection.anomaly or not forest_detection.anomaly:
        raise RuntimeError("seeded advisory fixture did not trigger the existing detectors")

    first_timestamp = min(event.timestamp for event in retry_trace.events)
    detector_event_id = next(
        event.event_id
        for event in retry_trace.events
        if event.event_type == "PAYMENT_DISPATCHED"
    )
    for event in retry_trace.events:
        shifted = event.model_copy(
            update={"timestamp": now + (event.timestamp - first_timestamp)}
        )
        if event.event_id == detector_event_id:
            shifted = shifted.model_copy(
                update={
                    "detector_version": forest_detection.detector_version,
                    "anomaly_score": forest_detection.anomaly_score,
                }
            )
        captured = collector.capture_payment_event(shifted)
        if captured.status not in {"accepted", "duplicate"}:
            raise RuntimeError("seeded advisory telemetry could not be persisted")
    reasons = tuple(
        dict.fromkeys((*rules_detection.reason_codes, *forest_detection.reason_codes))
    )
    return retry_trace.trace_id, reasons


def _is_reparse_point(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(path_stat.st_mode) or bool(attributes & reparse_flag)


def _assert_no_reparse_points(path: Path) -> None:
    absolute_path = Path(os.path.abspath(path))
    for candidate in reversed((absolute_path, *absolute_path.parents)):
        if _is_reparse_point(candidate):
            raise RuntimeError("refusing to access a demo path containing a reparse point")


def _ensure_demo_parent(parent: Path | None) -> Path:
    if parent is None:
        raise RuntimeError(
            "AGENTSENTINEL_DEMO_DATA_PARENT must name an operator-provisioned, "
            "trusted directory"
        )
    if not parent.is_absolute():
        raise RuntimeError("demo data parent must be an absolute path")
    canonical_parent = Path(os.path.abspath(parent))
    _assert_no_reparse_points(canonical_parent)
    if not canonical_parent.exists():
        raise RuntimeError(
            "demo data parent must already exist and be operator-provisioned"
        )
    if not canonical_parent.is_dir():
        raise RuntimeError("demo data parent is not a directory")
    resolved_parent = canonical_parent.resolve(strict=True)
    if resolved_parent != canonical_parent:
        raise RuntimeError("demo data parent is not a canonical directory")
    return resolved_parent


def _prepare_demo_directory(parent: Path, *, reset: bool) -> Path:
    if reset:
        raise RuntimeError(
            "--reset is unsupported; demo runs always use a fresh directory and never delete data"
        )
    canonical_parent = _ensure_demo_parent(parent)
    for _ in range(10):
        run_directory = canonical_parent / f"run-{uuid4().hex}"
        try:
            run_directory.mkdir()
        except FileExistsError:
            continue
        _assert_no_reparse_points(run_directory)
        if run_directory.resolve(strict=True).parent != canonical_parent:
            raise RuntimeError("new demo directory resolved outside its configured parent")
        marker = run_directory / DEMO_MARKER
        marker.write_text(DEMO_MARKER_CONTENT, encoding="utf-8")
        return run_directory
    raise RuntimeError("could not create a unique demo directory")


def seed_demo_data(*, reset: bool = False) -> DemoSeed:
    """Create a fresh isolated database and seed real gateway/approval flows."""
    if not _demo_seeding_enabled():
        raise RuntimeError(
            "demo seeding is disabled until a safe Windows demo-data boundary "
            "is independently verified"
        )
    _load_demo_dependencies()
    root = _prepare_demo_directory(DEMO_DATA_PARENT, reset=reset)
    database_path = root / "payments.sqlite"
    telemetry_root = root / "telemetry"
    telemetry_path = telemetry_root / "payment_telemetry.sqlite"
    failures_path = telemetry_root / "rejected" / "payment_telemetry.jsonl"
    normal_database = ROOT / "data" / "paypal_sandbox" / "gateway_demo.sqlite"
    if database_path.resolve() == normal_database.resolve():
        raise RuntimeError("demo database must be separate from the application database")

    seeded_at = datetime.now(timezone.utc).replace(microsecond=0)
    clock = DemoClock(seeded_at)
    collector = TraceCollector(telemetry_root)
    publisher = PaymentTelemetryPublisher(collector)
    approvals = ApprovalStore(
        database_path,
        clock=clock,
        ttl_seconds=24 * 60 * 60,
        trusted_issuer_id=DEMO_ISSUER,
        telemetry_publisher=publisher,
    )
    payments = SQLitePaymentStore(
        database_path,
        trusted_issuer_id=DEMO_ISSUER,
        clock=clock,
    )
    policy = PaymentPolicy(payments, clock=clock)
    mock_adapter = MockPayPalAdapter()
    gateway = PaymentGateway(
        policy,
        payments,
        payments,
        mock_adapter,
        kill_switch=approvals,
        telemetry_publisher=publisher,
        provider_mode=ProviderModeConfig.mock(),
    )
    entrypoint = TrustedGatewayEntryPoint(gateway, DemoContextProvider())
    executor = ApprovedPaymentExecutor(entrypoint, approvals)
    scenarios: list[ScenarioResult] = []

    allow_intent = _intent("allow", amount="4.00")
    allow_approval = _approve_for_demo(approvals, allow_intent)
    allow_result = executor.execute(allow_approval.approval_id)
    if allow_result.decision is None:
        raise RuntimeError("ALLOW demonstration did not produce a policy decision")
    scenarios.append(
        _result(
            "ALLOW - completed operation",
            allow_intent.trace_id,
            allow_result.decision.outcome,
            allow_result.status,
            tuple(allow_result.decision.reason_codes),
        )
    )

    review_intent = _intent("review", amount="6.00")
    review_result = entrypoint.submit_or_request_approval(review_intent, approvals)
    if not isinstance(review_result, ApprovalRequired):
        raise RuntimeError("REVIEW demonstration did not create a pending approval")
    scenarios.append(
        _result(
            "REVIEW - pending human approval",
            review_intent.trace_id,
            review_result.decision.outcome,
            "PENDING",
            tuple(review_result.decision.reason_codes),
        )
    )

    amount_authorization_intent = _intent("amount_mismatch", amount="2.00")
    _approve_for_demo(approvals, amount_authorization_intent)
    amount_mismatch_intent = amount_authorization_intent.model_copy(
        update={"money": Money(amount="3.00", currency="USD")}
    )
    amount_result = entrypoint.submit(amount_mismatch_intent)
    if amount_result.decision is None:
        raise RuntimeError("amount mismatch demonstration did not produce a decision")
    scenarios.append(
        _result(
            "BLOCK - amount mismatch",
            amount_mismatch_intent.trace_id,
            amount_result.decision.outcome,
            amount_result.status,
            tuple(amount_result.decision.reason_codes),
        )
    )

    merchant_authorization_intent = _intent("merchant_mismatch", amount="2.00")
    _approve_for_demo(approvals, merchant_authorization_intent)
    merchant_mismatch_intent = merchant_authorization_intent.model_copy(
        update={"merchant_id": "OTHER_MERCHANT"}
    )
    merchant_result = entrypoint.submit(merchant_mismatch_intent)
    if merchant_result.decision is None:
        raise RuntimeError("merchant mismatch demonstration did not produce a decision")
    scenarios.append(
        _result(
            "BLOCK - merchant mismatch",
            merchant_mismatch_intent.trace_id,
            merchant_result.decision.outcome,
            merchant_result.status,
            tuple(merchant_result.decision.reason_codes),
        )
    )

    uncertain_intent = _intent("uncertain", amount="2.00")
    uncertain_approval = _approve_for_demo(
        approvals, uncertain_intent, provider_mode="SANDBOX"
    )
    spy_transport = SpyTransport(timeout_post=True)
    mock_client = PayPalSandboxClient(
        SandboxConfig("mock_demo_client", "mock_demo_secret", MERCHANT),
        spy_transport,
    )
    if mock_client.transport is not spy_transport:
        raise RuntimeError("uncertain scenario transport was not retained")
    uncertain_gateway = PaymentGateway(
        policy,
        payments,
        payments,
        SandboxPaymentAdapter(mock_client),
        kill_switch=approvals,
        telemetry_publisher=publisher,
        enable_sandbox_execution=True,
        provider_mode=ProviderModeConfig.sandbox(),
    )
    uncertain_executor = ApprovedPaymentExecutor(
        TrustedGatewayEntryPoint(uncertain_gateway, DemoContextProvider()),
        approvals,
    )
    uncertain_result = uncertain_executor.execute(uncertain_approval.approval_id)
    if uncertain_result.decision is None:
        raise RuntimeError("uncertain demonstration did not produce a policy decision")
    scenarios.append(
        _result(
            "UNCERTAIN - simulated ambiguous outcome",
            uncertain_intent.trace_id,
            uncertain_result.decision.outcome,
            uncertain_result.status,
            tuple(uncertain_result.reason_codes),
        )
    )

    kill_intent = _intent("kill_switch", amount="2.00")
    _approve_for_demo(approvals, kill_intent)
    submission_error: BaseException | None = None
    try:
        approvals.set_kill_switch(enabled=True, actor_role="approver")
        kill_result = entrypoint.submit(kill_intent)
    except BaseException as error:
        submission_error = error
        raise
    finally:
        try:
            approvals.set_kill_switch(enabled=False, actor_role="approver")
        except BaseException:
            if submission_error is None:
                raise
    if kill_result.decision is None:
        raise RuntimeError("kill-switch demonstration did not produce a policy decision")
    if approvals.read_kill_switch().enabled:
        raise RuntimeError("demo kill switch did not finish in the OFF state")
    scenarios.append(
        _result(
            "Kill switch - blocked while ON; final state OFF",
            kill_intent.trace_id,
            kill_result.decision.outcome,
            kill_result.status,
            tuple(kill_result.decision.reason_codes),
        )
    )

    advisory_trace_id, advisory_reasons = _write_advisory_trace(collector, seeded_at)
    expected_policy_events = {
        allow_intent.operation_id: allow_intent.trace_id,
        review_intent.operation_id: review_intent.trace_id,
        amount_mismatch_intent.operation_id: amount_mismatch_intent.trace_id,
        merchant_mismatch_intent.operation_id: merchant_mismatch_intent.trace_id,
        uncertain_intent.operation_id: uncertain_intent.trace_id,
        kill_intent.operation_id: kill_intent.trace_id,
    }
    if publisher.failures:
        raise RuntimeError("seeded gateway telemetry publication failed")
    with PaymentAuditChain.open_readonly(telemetry_path).verified_snapshot() as (
        audit_connection,
        audit,
    ):
        if not audit.valid:
            raise RuntimeError("seeded payment audit chain failed integrity verification")
        policy_events = audit_connection.execute(
            "SELECT event_type, canonical_event FROM payment_audit_chain "
            "WHERE event_type='POLICY_EVALUATED'"
        ).fetchall()
    observed_policy_events = {
        (event["operation_id"], event["trace_id"])
        for row in policy_events
        if row["event_type"] == "POLICY_EVALUATED"
        for event in (json.loads(row["canonical_event"]),)
        if event.get("event_type") == "POLICY_EVALUATED"
    }
    missing_policy_events = {
        (operation_id, trace_id)
        for operation_id, trace_id in expected_policy_events.items()
        if (operation_id, trace_id) not in observed_policy_events
    }
    if missing_policy_events:
        raise RuntimeError("seeded gateway policy telemetry is incomplete")
    if not spy_transport.calls or any(
        path not in {"/v1/oauth2/token", "/v2/checkout/orders"}
        for _, path, _ in spy_transport.calls
    ):
        raise RuntimeError("unexpected mock transport activity in uncertain scenario")

    scenarios.append(
        _result(
            "Advisory alert - existing rules and Isolation Forest",
            advisory_trace_id,
            "ADVISORY",
            "NOT_AUTHORIZATION",
            advisory_reasons,
        )
    )
    return DemoSeed(
        root=root,
        database_path=database_path,
        telemetry_path=telemetry_path,
        failures_path=failures_path,
        pending_approval_id=review_result.approval_id,
        scenarios=tuple(scenarios),
        mock_adapter=mock_adapter,
        spy_transport=spy_transport,
    )


def _ensure_local_port_available() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", 8000))
        except OSError:
            raise RuntimeError(
                "127.0.0.1:8000 is unavailable; stop the conflicting local service first"
            ) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Demo seeding is disabled until a safe Windows demo-data boundary "
            "is independently verified."
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="unsupported; demo runs always use a new directory and delete nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.reset:
        print(
            "--reset is unsupported; demo runs always use a fresh directory and never delete data.",
            file=sys.stderr,
        )
        return 2
    if not _demo_seeding_enabled():
        print(
            "Demo seeding is disabled until a safe Windows demo-data boundary "
            "is independently verified.",
            file=sys.stderr,
        )
        return 2
    if not sys.stdout.isatty():
        print(
            "Demo startup requires a local interactive terminal for the temporary credential.",
            file=sys.stderr,
        )
        return 2
    try:
        _ensure_local_port_available()
        seeded = seed_demo_data(reset=args.reset)
    except (FileExistsError, OSError, RuntimeError) as error:
        print(f"Demo startup failed: {error}", file=sys.stderr)
        return 2

    operator_secret = secrets.token_urlsafe(32)
    app = create_app(
        db_path=seeded.database_path,
        approver_secret=operator_secret,
        viewer_secret="demo-viewer-disabled",
        telemetry_db_path=seeded.telemetry_path,
        failures_path=seeded.failures_path,
        demo_mode=True,
        ttl_seconds=24 * 60 * 60,
    )
    print("LOCAL MOCK DEMONSTRATION - no PayPal requests are made.")
    print("Dashboard: http://127.0.0.1:8000/dashboard/login")
    print(
        "Temporary local operator credential (shown only in this terminal): "
        f"{operator_secret}"
    )
    print("Seeded scenarios:")
    for scenario in seeded.scenarios:
        print(
            f"- {scenario.name}: {scenario.decision}/{scenario.status} "
            f"({', '.join(scenario.reason_codes)})"
        )
    print("Demo database is isolated in a fresh directory; existing runs are preserved.")

    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        access_log=False,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
