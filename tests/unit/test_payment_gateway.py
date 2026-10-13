"""Day 6 gateway behavior with one-process stores and fake payment adapters only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, Lock

import pytest

from agentsentinel.payment_gateway import (
    AdapterOutcome, InMemoryOperationStore, PaymentGateway, TestOnlyAdapter,
)
from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy
from agentsentinel.schema.payment import Money, PaymentAuthorization, PaymentIntent

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
CONTEXT = AuthenticatedScope(principal_id="principal_001", scope_id="scope_001")


def intent(*, amount="3.00", currency="USD", action="create_order", target=None, **changes):
    fields = dict(intent_id="intent_001", operation_id="operation_001", trace_id="trace_001",
                  scope_id="scope_001", money=Money(amount=amount, currency=currency),
                  merchant_id="merchant_001", purpose="Notebook", action=action,
                  target_order_id=target, requested_at=NOW)
    fields.update(changes)
    return PaymentIntent(**fields)


def authorization(request=None, **changes):
    request = request or intent()
    fields = dict(authorization_id="authorization_001", issuer_id="issuer_001",
                  scope_id=request.scope_id, intent_id=request.intent_id,
                  operation_id=request.operation_id, merchant_id=request.merchant_id,
                  money=request.money, action=request.action, target_order_id=request.target_order_id,
                  issued_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=5),
                  status="issued", record_origin="application_issued")
    fields.update(changes)
    return PaymentAuthorization(**fields)


class TestAuthorizationStore:
    """Test-local repository and atomic consumer; not a production approval store."""

    __test__ = False

    def __init__(self, record=None, *, lookup_error=False, claim_error=False, claim_allowed=True):
        self._lock = Lock()
        self.record = record
        self.lookup_error = lookup_error
        self.claim_error = claim_error
        self.claim_allowed = claim_allowed
        self.claim_count = 0
        self.lookup_count = 0
        self._consumed = False

    def find(self, context, intent_id, operation_id):
        with self._lock:
            self.lookup_count += 1
            if self.lookup_error:
                raise TimeoutError("hidden repository detail")
            if context != CONTEXT or self.record is None or self._consumed:
                return None
            return self.record

    def claim(self, context, request, decision):
        with self._lock:
            self.claim_count += 1
            if self.claim_error:
                raise TimeoutError("hidden consumption detail")
            record = self.record
            if (not self.claim_allowed or self._consumed or context != CONTEXT or record is None
                    or decision.outcome != "ALLOW" or not record.binds_to(request)
                    or record.status != "issued" or not record.issued_at <= NOW < record.expires_at):
                return False
            self._consumed = True
            return True


class FakeAdapter(TestOnlyAdapter):
    def __init__(self, *, error=None, outcome=None, entered=None, release=None):
        self.calls = 0
        self._lock = Lock()
        self.error = error
        self.outcome = outcome
        self.entered = entered
        self.release = release

    def execute(self, request):
        with self._lock:
            self.calls += 1
        if self.entered:
            self.entered.set()
        if self.release and not self.release.wait(5):
            raise TimeoutError("fake adapter hold timed out")
        if self.error:
            raise self.error
        return self.outcome if self.outcome is not None else AdapterOutcome(status="COMPLETED")


def build(record=None, *, adapter=None, operations=None, **store_options):
    store = TestAuthorizationStore(record, **store_options)
    fake = adapter if adapter is not None else FakeAdapter()
    gateway = PaymentGateway(PaymentPolicy(store, clock=lambda: NOW),
                             operations or InMemoryOperationStore(), store, fake)
    return gateway, store, fake


def test_trusted_authorization_executes_fake_once_and_caches_result():
    request = intent()
    gateway, store, adapter = build(authorization(request))
    first = gateway.submit(request, CONTEXT)
    second = gateway.submit(request, CONTEXT)
    assert first.status == "COMPLETED" and first.decision.outcome == "ALLOW"
    assert first.reason_codes == ("EXECUTED_FAKE",)
    assert second == first
    assert adapter.calls == store.claim_count == store.lookup_count == 1
    assert type(first).model_validate_json(first.model_dump_json()) == first


def test_block_review_and_missing_authorization_never_execute():
    for request, record, expected in [
        (intent(), None, "REJECTED"),
        (intent(amount="6.00"), authorization(intent(amount="6.00")), "REVIEW_REQUIRED"),
        (intent(), authorization(merchant_id="merchant_002"), "REJECTED"),
    ]:
        gateway, store, adapter = build(record)
        result = gateway.submit(request, CONTEXT)
        assert result.status == expected
        assert result.decision.outcome == ("REVIEW" if expected == "REVIEW_REQUIRED" else "BLOCK")
        assert adapter.calls == store.claim_count == 0


def test_forged_agent_authorization_is_not_an_input():
    gateway, _, adapter = build(None)
    fake_record = authorization()
    with pytest.raises(TypeError):
        gateway.submit(intent(), CONTEXT, fake_record)
    with pytest.raises(TypeError):
        gateway.submit(intent(), CONTEXT, decision="ALLOW")
    assert gateway.submit(intent(), CONTEXT).status == "REJECTED"
    assert adapter.calls == 0


def test_review_state_is_cached_without_consumption():
    request = intent(amount="6.00")
    gateway, store, adapter = build(authorization(request))
    first = gateway.submit(request, CONTEXT)
    assert first.status == "REVIEW_REQUIRED"
    assert gateway.submit(request, CONTEXT) == first
    assert store.lookup_count == 1
    assert store.claim_count == adapter.calls == 0


@pytest.mark.parametrize("change,reason", [
    ({"merchant_id": "merchant_002"}, "AUTHORIZATION_MERCHANT_MISMATCH"),
    ({"money": Money(amount="3.01", currency="USD")}, "AUTHORIZATION_AMOUNT_MISMATCH"),
    ({"money": Money(amount="3.00", currency="EUR")}, "AUTHORIZATION_CURRENCY_MISMATCH"),
    ({"scope_id": "scope_002"}, "AUTHORIZATION_SCOPE_MISMATCH"),
])
def test_wrong_authorization_fields_never_execute(change, reason):
    gateway, store, adapter = build(authorization(**change))
    result = gateway.submit(intent(), CONTEXT)
    assert result.status == "REJECTED"
    assert reason in result.decision.reason_codes
    assert store.claim_count == adapter.calls == 0


def test_wrong_action_and_target_order_never_execute():
    request = intent(action="capture_order", target="order_001")
    for record, reason in [
        (authorization(request, action="create_order", target_order_id=None), "AUTHORIZATION_ACTION_MISMATCH"),
        (authorization(request, target_order_id="order_002"), "AUTHORIZATION_TARGET_MISMATCH"),
    ]:
        gateway, _, adapter = build(record)
        result = gateway.submit(request, CONTEXT)
        assert reason in result.decision.reason_codes
        assert adapter.calls == 0


@pytest.mark.parametrize("status", ["revoked", "consumed", "expired"])
def test_inactive_authorization_never_executes(status):
    gateway, _, adapter = build(authorization(status=status, closed_at=NOW))
    result = gateway.submit(intent(), CONTEXT)
    assert "AUTHORIZATION_INACTIVE" in result.decision.reason_codes
    assert adapter.calls == 0


def test_expired_and_wrong_authenticated_scope_never_execute():
    expired = authorization(issued_at=NOW - timedelta(minutes=5), expires_at=NOW)
    gateway, _, adapter = build(expired)
    assert "AUTHORIZATION_EXPIRED" in gateway.submit(intent(), CONTEXT).decision.reason_codes
    assert adapter.calls == 0
    gateway, _, adapter = build(authorization())
    wrong = AuthenticatedScope(principal_id="principal_001", scope_id="scope_002")
    assert gateway.submit(intent(), wrong).status == "REJECTED"
    assert adapter.calls == 0


def test_unsupported_currency_and_operation_conflict_never_execute_twice():
    request = intent(currency="EUR", amount="3")
    gateway, _, adapter = build(authorization(request))
    assert "UNSUPPORTED_CURRENCY" in gateway.submit(request, CONTEXT).decision.reason_codes
    assert adapter.calls == 0
    first = intent()
    gateway, _, adapter = build(authorization(first))
    assert gateway.submit(first, CONTEXT).status == "COMPLETED"
    changed = intent(merchant_id="merchant_002")
    result = gateway.submit(changed, CONTEXT)
    assert result.status == "REJECTED" and result.reason_codes == ("OPERATION_CONFLICT",)
    assert adapter.calls == 1


def test_concurrent_duplicate_attempts_call_fake_once():
    entered, release = Event(), Event()
    adapter = FakeAdapter(entered=entered, release=release)
    request = intent()
    gateway, store, _ = build(authorization(request), adapter=adapter)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(gateway.submit, request, CONTEXT)
        assert entered.wait(5)
        concurrent = pool.submit(gateway.submit, request, CONTEXT).result(timeout=5)
        assert concurrent.status == "IN_PROGRESS"
        release.set()
        completed = first.result(timeout=5)
    assert completed.status == "COMPLETED"
    assert adapter.calls == store.claim_count == 1


def test_repository_reservation_and_consumption_fail_closed():
    gateway, _, adapter = build(authorization(), lookup_error=True)
    result = gateway.submit(intent(), CONTEXT)
    assert "AUTHORIZATION_LOOKUP_FAILED" in result.decision.reason_codes
    assert adapter.calls == 0

    class FailingOperations:
        def reserve(self, operation_id, fingerprint):
            raise RuntimeError("hidden store detail")

        def finish(self, operation_id, fingerprint, result):
            raise AssertionError("finish must not run")

    gateway, _, adapter = build(authorization(), operations=FailingOperations())
    assert gateway.submit(intent(), CONTEXT).reason_codes == ("OPERATION_RESERVATION_FAILED",)
    assert adapter.calls == 0
    for options in ({"claim_allowed": False}, {"claim_error": True}):
        gateway, _, adapter = build(authorization(), **options)
        assert gateway.submit(intent(), CONTEXT).reason_codes == ("AUTHORIZATION_CONSUMPTION_FAILED",)
        assert adapter.calls == 0


def test_adapter_timeout_is_uncertain_and_not_retried():
    adapter = FakeAdapter(error=TimeoutError("RAW-SENSITIVE-PROVIDER-BODY"))
    gateway, store, _ = build(authorization(), adapter=adapter)
    first = gateway.submit(intent(), CONTEXT)
    second = gateway.submit(intent(), CONTEXT)
    assert first.status == "UNCERTAIN" and second == first
    assert first.reason_codes == ("PROVIDER_OUTCOME_UNCERTAIN",)
    assert "RAW-SENSITIVE" not in first.model_dump_json()
    assert adapter.calls == store.claim_count == 1


def test_raw_adapter_payload_is_never_returned():
    adapter = FakeAdapter(outcome={"status": "COMPLETED", "raw_response": "RAW-SENSITIVE-PROVIDER-BODY"})
    gateway, _, _ = build(authorization(), adapter=adapter)
    result = gateway.submit(intent(), CONTEXT)
    assert result.status == "UNCERTAIN"
    assert "RAW-SENSITIVE" not in result.model_dump_json()


def test_finalization_failure_after_fake_call_is_uncertain_and_nonretryable():
    class FailingFinish(InMemoryOperationStore):
        def finish(self, operation_id, fingerprint, result):
            raise RuntimeError("hidden store detail")

    operations = FailingFinish()
    gateway, store, adapter = build(authorization(), operations=operations)
    first = gateway.submit(intent(), CONTEXT)
    second = gateway.submit(intent(), CONTEXT)
    assert first.status == "UNCERTAIN"
    assert first.reason_codes == ("OPERATION_FINALIZATION_FAILED",)
    assert second.status == "IN_PROGRESS"
    assert adapter.calls == store.claim_count == 1


def test_real_adapter_shape_is_disabled_without_fake_marker():
    class RealLookingAdapter:
        calls = 0

        def execute(self, request):
            self.calls += 1
            return AdapterOutcome(status="COMPLETED")

    adapter = RealLookingAdapter()
    gateway, store, _ = build(authorization(), adapter=adapter)
    result = gateway.submit(intent(), CONTEXT)
    assert result.reason_codes == ("EXECUTION_DISABLED",)
    assert adapter.calls == store.claim_count == 0


def test_invalid_intent_copy_is_rejected_before_reservation():
    gateway, store, adapter = build(authorization())
    unvalidated = intent().model_copy(update={"action": "refund"})
    with pytest.raises(ValueError):
        gateway.submit(unvalidated, CONTEXT)
    assert store.lookup_count == adapter.calls == 0
