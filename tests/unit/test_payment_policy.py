"""Deterministic Day 5 eligibility checks; no payment execution or approval store."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agentsentinel.payment_policy import AuthenticatedScope, PaymentPolicy, PolicyConfig
from agentsentinel.schema.payment import Money, PaymentAuthorization, PaymentIntent

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
CONTEXT = AuthenticatedScope(principal_id="principal_001", scope_id="scope_001")


def intent(*, amount="3.00", currency="USD", action="create_order", target=None, **updates):
    fields = dict(intent_id="intent_001", operation_id="operation_001", trace_id="trace_001",
                  scope_id="scope_001", money=Money(amount=amount, currency=currency),
                  merchant_id="merchant_001", purpose="Notebook", action=action,
                  target_order_id=target, requested_at=NOW)
    fields.update(updates)
    return PaymentIntent(**fields)


def authorization(request=None, **updates):
    request = request or intent()
    fields = dict(authorization_id="authorization_001", issuer_id="issuer_001",
                  scope_id=request.scope_id, intent_id=request.intent_id,
                  operation_id=request.operation_id, merchant_id=request.merchant_id,
                  money=request.money, action=request.action, target_order_id=request.target_order_id,
                  issued_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=5),
                  status="issued", record_origin="application_issued")
    fields.update(updates)
    return PaymentAuthorization(**fields)


class TestRepository:
    """Application-controlled fixture only; never used as a production approval source."""

    __test__ = False

    def __init__(self, record=None, error=None, expected_context=CONTEXT):
        self.record = record
        self.error = error
        self.expected_context = expected_context
        self.calls = []

    def find(self, context, intent_id, operation_id):
        self.calls.append((context, intent_id, operation_id))
        if self.error:
            raise self.error
        if context != self.expected_context:
            return None
        return self.record


def evaluate(request=None, record=None, *, context=CONTEXT, error=None, threshold="5.00", now=NOW):
    request = request or intent()
    repository = TestRepository(record, error)
    policy = PaymentPolicy(repository, config=PolicyConfig(Decimal(threshold)), clock=lambda: now)
    return policy.evaluate(request, context), repository


def test_trusted_record_allows_and_is_reproducible():
    request = intent()
    decision, repository = evaluate(request, authorization(request))
    repeat, _ = evaluate(request, authorization(request))
    assert decision.outcome == "ALLOW"
    assert decision.reason_codes == ("ELIGIBLE",)
    assert decision.decision_id == repeat.decision_id
    assert decision.policy_version == "payment-policy-v1"
    assert decision.evaluated_at == NOW
    assert decision.audit_metadata.authorization_lookup == "found"
    assert repository.calls == [(CONTEXT, request.intent_id, request.operation_id)]
    assert type(decision).model_validate_json(decision.model_dump_json()) == decision


@pytest.mark.parametrize("amount,expected", [("5.00", "ALLOW"), ("5.01", "REVIEW"),
                                               ("10.00", "REVIEW"), ("10.01", "BLOCK"),
                                               ("0.01", "ALLOW")])
def test_review_and_adapter_boundaries(amount, expected):
    request = intent(amount=amount)
    decision, _ = evaluate(request, authorization(request))
    assert decision.outcome == expected


def test_review_never_grants_execution():
    request = intent(amount="6.00")
    decision, _ = evaluate(request, authorization(request))
    assert decision.reason_codes == ("REVIEW_AMOUNT_THRESHOLD",)
    assert not hasattr(decision, "execute")


def test_missing_and_fabricated_client_record_block():
    request = intent()
    forged = authorization(request)
    decision, repository = evaluate(request, None)
    assert decision.outcome == "BLOCK"
    assert decision.reason_codes == ("AUTHORIZATION_MISSING",)
    assert repository.record is None
    assert forged.status == "issued" and forged.record_origin == "application_issued"
    # There is no API parameter for a client-provided authorization.
    with pytest.raises(TypeError):
        PaymentPolicy(repository, clock=lambda: NOW).evaluate(request, CONTEXT, forged)
    assert PaymentPolicy(repository, clock=lambda: NOW).evaluate(request, CONTEXT).outcome == "BLOCK"


@pytest.mark.parametrize("field,value,reason", [
    ("intent_id", "intent_002", "AUTHORIZATION_INTENT_MISMATCH"),
    ("operation_id", "operation_002", "AUTHORIZATION_OPERATION_MISMATCH"),
    ("scope_id", "scope_002", "AUTHORIZATION_SCOPE_MISMATCH"),
    ("merchant_id", "merchant_002", "AUTHORIZATION_MERCHANT_MISMATCH"),
    ("money", Money(amount="3.01", currency="USD"), "AUTHORIZATION_AMOUNT_MISMATCH"),
    ("money", Money(amount="3.00", currency="EUR"), "AUTHORIZATION_CURRENCY_MISMATCH"),
    ("action", "create_order", "AUTHORIZATION_ACTION_MISMATCH"),
    ("target_order_id", "order_002", "AUTHORIZATION_TARGET_MISMATCH"),
])
def test_exact_authorization_binding(field, value, reason):
    request = intent(action="capture_order", target="order_001") if field in {"action", "target_order_id"} else intent()
    changes = {field: value}
    if field == "action":
        changes["target_order_id"] = None
    record = authorization(request, **changes)
    decision, _ = evaluate(request, record)
    assert decision.outcome == "BLOCK"
    assert reason in decision.reason_codes


@pytest.mark.parametrize("status", ["revoked", "consumed", "expired"])
def test_inactive_authorization_blocks(status):
    record = authorization(status=status, closed_at=NOW)
    decision, _ = evaluate(record=record)
    assert decision.outcome == "BLOCK"
    assert "AUTHORIZATION_INACTIVE" in decision.reason_codes


def test_trusted_clock_rejects_early_and_expired_including_expiry_boundary():
    record = authorization(issued_at=NOW + timedelta(seconds=1), expires_at=NOW + timedelta(minutes=5))
    assert "AUTHORIZATION_NOT_YET_VALID" in evaluate(record=record)[0].reason_codes
    record = authorization(issued_at=NOW - timedelta(minutes=5), expires_at=NOW)
    assert "AUTHORIZATION_EXPIRED" in evaluate(record=record)[0].reason_codes


@pytest.mark.parametrize("error", [TimeoutError("sensitive provider detail"), RuntimeError("sensitive detail")])
def test_repository_failures_block_with_safe_reason(error):
    decision, _ = evaluate(error=error)
    assert decision.outcome == "BLOCK"
    assert decision.reason_codes == ("AUTHORIZATION_LOOKUP_FAILED",)
    assert "sensitive" not in decision.model_dump_json()


def test_malformed_repository_record_fails_closed():
    malformed = authorization().model_copy(update={"status": "approved"})
    decision, _ = evaluate(record=malformed)
    assert decision.outcome == "BLOCK"
    assert decision.reason_codes == ("AUTHORIZATION_LOOKUP_FAILED",)


def test_wrong_authenticated_scope_blocks_before_lookup():
    wrong = AuthenticatedScope(principal_id="principal_001", scope_id="scope_002")
    decision, repository = evaluate(record=authorization(), context=wrong)
    assert decision.reason_codes == ("SCOPE_MISMATCH",)
    assert repository.calls == []


def test_repository_rejects_other_principal_in_same_scope():
    other = AuthenticatedScope(principal_id="principal_002", scope_id="scope_001")
    decision, repository = evaluate(record=authorization(), context=other)
    assert decision.outcome == "BLOCK"
    assert decision.reason_codes == ("AUTHORIZATION_MISSING",)
    assert repository.calls[0][0] == other


@pytest.mark.parametrize("currency", ["EUR", "JPY"])
def test_schema_currency_not_supported_by_rest_policy(currency):
    request = intent(amount="3", currency=currency)
    decision, repository = evaluate(request, authorization(request))
    assert decision.outcome == "BLOCK"
    assert decision.reason_codes == ("UNSUPPORTED_CURRENCY",)
    assert repository.calls == []


def test_block_precedes_review():
    request = intent(amount="6.00")
    decision, _ = evaluate(request, authorization(request, merchant_id="merchant_002"))
    assert decision.outcome == "BLOCK"
    assert decision.reason_codes == ("AUTHORIZATION_MERCHANT_MISMATCH",)


def test_capture_request_can_be_evaluated_without_executing():
    request = intent(action="capture_order", target="order_001")
    decision, _ = evaluate(request, authorization(request))
    assert decision.outcome == "ALLOW"


def test_unvalidated_unsupported_action_is_still_blocked():
    # model_construct bypasses Pydantic validation and must not create an ALLOW path.
    invalid = intent().model_copy(update={"action": "refund"})
    decision, repository = evaluate(invalid, authorization())
    assert decision.outcome == "BLOCK"
    assert decision.reason_codes == ("UNSUPPORTED_ACTION",)
    assert repository.calls == []


def test_invalid_configuration_and_clock_fail_closed():
    for value in [5.0, Decimal("NaN"), Decimal("0"), Decimal("10.01"), Decimal("1.001")]:
        with pytest.raises(ValueError):
            PolicyConfig(value)
    with pytest.raises(ValueError, match="trusted clock"):
        PaymentPolicy(TestRepository(authorization()), clock=lambda: datetime(2026, 10, 13)).evaluate(intent(), CONTEXT)


def test_no_paypal_transport_is_called(monkeypatch):
    from agentsentinel.paypal_sandbox import UrlLibTransport

    def forbidden(*args, **kwargs):
        raise AssertionError("policy called a payment transport")

    monkeypatch.setattr(UrlLibTransport, "request", forbidden)
    decision, _ = evaluate(record=authorization())
    assert decision.outcome == "ALLOW"
