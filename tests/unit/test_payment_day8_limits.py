"""Day 8 deterministic limits; every provider is an in-process spy."""

from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3

from agentsentinel.payment_gateway import PaymentGateway, TrustedGatewayEntryPoint, _fingerprint
from agentsentinel.payment_policy import PaymentPolicy
from agentsentinel.payment_sandbox_adapter import SandboxPaymentAdapter
from agentsentinel.payment_sqlite import SQLitePaymentStore, SafetyLimits
from agentsentinel.paypal_sandbox import PayPalSandboxClient, SandboxConfig

from test_payment_sandbox_gateway import CONTEXT, MERCHANT, SpyTransport, make_intent, seed


def gateway_for(tmp_path, limits):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "limits.sqlite"
    store = SQLitePaymentStore(db, trusted_issuer_id="demo_issuer", limits=limits)
    spy = SpyTransport()
    client = PayPalSandboxClient(SandboxConfig("fake_client", "fake_secret", MERCHANT), spy)
    gateway = PaymentGateway(PaymentPolicy(store), store, store, SandboxPaymentAdapter(client),
                             enable_sandbox_execution=True)
    return db, store, spy, gateway


def capture(operation):
    return make_intent(action="capture_order", operation=operation, target="ORDER123")


def test_operation_attempt_count_is_durable_and_blocks_before_provider(tmp_path):
    limits = SafetyLimits(max_attempts_per_operation=2)
    db, store, spy, gateway = gateway_for(tmp_path, limits)
    intent = make_intent()
    seed(db, intent)
    assert gateway.submit(intent, CONTEXT).status == "COMPLETED"
    assert gateway.submit(intent, CONTEXT).status == "COMPLETED"
    restarted = SQLitePaymentStore(db, trusted_issuer_id="demo_issuer", limits=limits)
    restarted_gateway = PaymentGateway(PaymentPolicy(restarted), restarted, restarted)
    result = restarted_gateway.submit(intent, CONTEXT)
    assert result.status == "REJECTED"
    assert result.decision.reason_codes == ("LOOP_LIMIT_EXCEEDED", "OPERATION_LOOP_LIMIT")
    assert len([call for call in spy.calls if call[1] == "/v2/checkout/orders"]) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count FROM operation_attempts WHERE operation_id=?",
                            (intent.operation_id,)).fetchone()[0] == 3


def test_trace_limit_counts_separate_operations(tmp_path):
    db, _, spy, gateway = gateway_for(tmp_path, SafetyLimits(max_attempts_per_trace=1))
    first = make_intent(operation="attempt_one")
    second = make_intent(operation="attempt_two")
    seed(db, first)
    seed(db, second)
    assert gateway.submit(first, CONTEXT).status == "COMPLETED"
    result = gateway.submit(second, CONTEXT)
    assert result.decision.reason_codes == ("LOOP_LIMIT_EXCEEDED", "TRACE_LOOP_LIMIT")
    assert len([call for call in spy.calls if call[1] == "/v2/checkout/orders"]) == 1


def test_daily_budget_counts_uncertain_and_merchant_limit_names_guard(tmp_path):
    db, _, spy, gateway = gateway_for(tmp_path, SafetyLimits(
        daily_spend_usd=Decimal("1.50"), merchant_daily_spend_usd=Decimal("1.00")))
    first = capture("budget_first")
    seed(db, first)
    assert gateway.submit(first, CONTEXT).status == "COMPLETED"
    second = capture("budget_second")
    seed(db, second)
    blocked = gateway.submit(second, CONTEXT)
    assert blocked.decision.reason_codes == ("BUDGET_EXCEEDED", "DAILY_BUDGET_LIMIT")
    assert len([call for call in spy.calls if call[1].endswith("/capture")]) == 1

    db2, _, spy2, gateway2 = gateway_for(tmp_path / "other", SafetyLimits(
        daily_spend_usd=Decimal("3.00"), merchant_daily_spend_usd=Decimal("1.50")))
    seed(db2, first)
    assert gateway2.submit(first, CONTEXT).status == "COMPLETED"
    seed(db2, second)
    blocked = gateway2.submit(second, CONTEXT)
    assert blocked.decision.reason_codes == ("BUDGET_EXCEEDED", "MERCHANT_BUDGET_LIMIT")
    assert len([call for call in spy2.calls if call[1].endswith("/capture")]) == 1


def test_agent_facing_entrypoint_cannot_supply_advisory(tmp_path):
    db, _, spy, gateway = gateway_for(tmp_path, SafetyLimits())
    intent = make_intent()
    seed(db, intent)

    class Context:
        def current_context(self):
            return CONTEXT

    class Advisory:
        def signals_for(self, intent, context):
            return ("request_review",)

    entry = TrustedGatewayEntryPoint(gateway, Context(), Advisory())
    try:
        entry.submit(intent, advisory_signals=())
    except TypeError:
        pass
    else:
        raise AssertionError("agent supplied signals were accepted")
    result = entry.submit(intent)
    assert result.status == "REVIEW_REQUIRED"
    assert result.decision.reason_codes == ("REVIEW_ADVISORY_SIGNAL",)
    assert spy.calls == []
    blocked = make_intent(operation="advisory_block", amount="2.00")
    seed(db, blocked, amount="1.00")
    result = entry.submit(blocked)
    assert result.status == "REJECTED"
    assert "AUTHORIZATION_AMOUNT_MISMATCH" in result.decision.reason_codes
    assert "REVIEW_ADVISORY_SIGNAL" not in result.decision.reason_codes
    assert spy.calls == []


def test_concurrent_reservations_cannot_both_spend_same_budget(tmp_path):
    _, store, spy, _ = gateway_for(tmp_path, SafetyLimits(
        daily_spend_usd=Decimal("1.00"), merchant_daily_spend_usd=Decimal("1.00")))
    intents = [capture("parallel_one"), capture("parallel_two")]

    def reserve(intent):
        return store.reserve_intent(intent, CONTEXT, _fingerprint(intent, CONTEXT))

    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(reserve, intents))
    assert sorted(item.state for item in states) == ["NEW", "SAFETY_BLOCK"]
    assert next(item for item in states if item.state == "SAFETY_BLOCK").guard_reason == "DAILY_BUDGET_LIMIT"
    assert spy.calls == []


def test_uncertain_capture_stays_in_budget(tmp_path):
    db, store, spy, gateway = gateway_for(tmp_path, SafetyLimits(
        daily_spend_usd=Decimal("1.00"), merchant_daily_spend_usd=Decimal("1.00")))
    original_request = spy.request

    def timeout_capture(method, path, headers, body):
        if method == "POST" and path.endswith("/capture"):
            spy.calls.append((method, path, headers.get("PayPal-Request-Id")))
            raise TimeoutError("test-only timeout")
        return original_request(method, path, headers, body)

    spy.request = timeout_capture
    first = capture("uncertain_first")
    seed(db, first)
    assert gateway.submit(first, CONTEXT).status == "UNCERTAIN"
    second = capture("uncertain_second")
    seed(db, second)
    result = gateway.submit(second, CONTEXT)
    assert result.decision.reason_codes == ("BUDGET_EXCEEDED", "DAILY_BUDGET_LIMIT")
    assert len([call for call in spy.calls if call[1].endswith("/capture")]) == 1


def test_prior_day_unresolved_reservation_still_holds_budget(tmp_path):
    _, store, spy, _ = gateway_for(tmp_path, SafetyLimits(
        daily_spend_usd=Decimal("1.00"), merchant_daily_spend_usd=Decimal("1.00")))
    first = capture("pending_yesterday")
    now = datetime(2026, 10, 13, 23, 59, tzinfo=timezone.utc)
    store.clock = lambda: now
    assert store.reserve_intent(first, CONTEXT, _fingerprint(first, CONTEXT)).state == "NEW"
    store.clock = lambda: now + timedelta(minutes=2)
    second = capture("new_day")
    result = store.reserve_intent(second, CONTEXT, _fingerprint(second, CONTEXT))
    assert result.state == "SAFETY_BLOCK" and result.guard_reason == "DAILY_BUDGET_LIMIT"
    assert spy.calls == []
