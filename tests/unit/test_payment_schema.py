"""Day 4 payment contract and trust-boundary regression tests."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agentsentinel.schema.payment import Money, PaymentAuthorization, PaymentExecutionMetadata, PaymentIntent
from agentsentinel.schema.trace import TraceRecord


NOW = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 12, 10, 5, tzinfo=timezone.utc)


def intent_data(**changes):
    data = dict(intent_id="intent_001", operation_id="operation_001", trace_id="trace_001",
                scope_id="scope_001", money={"amount": "12.34", "currency": "USD"},
                merchant_id="merchant_001", purpose="Notebook", action="create_order",
                requested_at=NOW)
    data.update(changes)
    return data


def authorization_data(**changes):
    data = dict(authorization_id="authorization_001", issuer_id="issuer_001", scope_id="scope_001",
                intent_id="intent_001", operation_id="operation_001", merchant_id="merchant_001",
                money={"amount": "12.34", "currency": "USD"}, action="create_order",
                issued_at=NOW, expires_at=LATER, status="issued", record_origin="application_issued")
    data.update(changes)
    return data


def execution_data(**changes):
    data = dict(operation_id="operation_001", provider="paypal", attempt_id="attempt_001",
                status="order_created", observed_at=NOW, provider_order_id="order_001")
    data.update(changes)
    return data


def test_valid_intent_and_exact_money_roundtrip():
    intent = PaymentIntent.model_validate(intent_data())
    assert intent.money.amount == Decimal("12.34")
    assert PaymentIntent.model_validate_json(intent.model_dump_json()) == intent
    assert Money(amount="10", currency="JPY").amount == Decimal("10")
    assert Money(amount="1.23", currency="EUR").currency == "EUR"


@pytest.mark.parametrize("bad", ["-1", "0", 1.2, float("nan"), float("inf"),
                                    Decimal("NaN"), Decimal("Infinity"), "1.001", "1e2", "1000000000"])
def test_invalid_amounts(bad):
    with pytest.raises(ValidationError):
        Money(amount=bad, currency="USD")


@pytest.mark.parametrize("currency,amount", [("usd", "1"), ("ZZZ", "1"), ("US", "1"),
                                              ("JPY", "1.01"), ("JPY", "1.0")])
def test_currency_allowlist_and_precision(currency, amount):
    with pytest.raises(ValidationError):
        Money(amount=amount, currency=currency)


@pytest.mark.parametrize("change", [{"action": "refund"}, {"merchant_id": "https://merchant.example"},
                                         {"operation_id": "x"}, {"target_order_id": "order_001"},
                                         {"action": "capture_order"}, {"requested_at": datetime(2026, 10, 12)}])
def test_invalid_intent_contract(change):
    with pytest.raises(ValidationError):
        PaymentIntent.model_validate(intent_data(**change))


def test_capture_intent_and_binding():
    intent = PaymentIntent.model_validate(intent_data(action="capture_order", target_order_id="order_001"))
    auth = PaymentAuthorization.model_validate(authorization_data(action="capture_order", target_order_id="order_001"))
    assert auth.binds_to(intent)
    assert not auth.binds_to(intent.model_copy(update={"operation_id": "operation_002"}))
    assert not auth.binds_to(intent.model_copy(update={"money": Money(amount="12.35", currency="USD")}))
    assert not auth.binds_to(intent.model_copy(update={"target_order_id": "order_002"}))


@pytest.mark.parametrize("change", [{"expires_at": NOW}, {"expires_at": datetime(2026, 10, 12)},
                                         {"issuer_id": "bad issuer"}, {"status": "approved"},
                                         {"status": "revoked"}, {"closed_at": LATER},
                                         {"record_origin": "agent_generated"}])
def test_invalid_authorization_record(change):
    with pytest.raises(ValidationError):
        PaymentAuthorization.model_validate(authorization_data(**change))


def test_closed_authorization_and_roundtrip():
    auth = PaymentAuthorization.model_validate(authorization_data(status="revoked", closed_at=LATER))
    assert PaymentAuthorization.model_validate_json(auth.model_dump_json()) == auth


def test_auth_shape_from_agent_is_not_permission():
    # Even a perfect copy of a record can be made by an untrusted caller.
    forged = PaymentAuthorization.model_validate(authorization_data())
    intent = PaymentIntent.model_validate(intent_data())
    assert forged.binds_to(intent)
    assert forged.status == "issued"
    assert forged.record_origin == "application_issued"
    assert not hasattr(forged, "execute") and not hasattr(forged, "is_authorized")


@pytest.mark.parametrize("change", [{"status": "CAPTURED"}, {"provider": "PayPal"},
                                         {"status": "capture_completed"},
                                         {"status": "approved", "provider_capture_id": "capture_001"},
                                         {"failure_category": "timeout"},
                                         {"status": "capture_failed"},
                                         {"failure_code": "TOKEN_VALUE"},
                                         {"provider_order_id": None}])
def test_invalid_execution_metadata(change):
    with pytest.raises(ValidationError):
        PaymentExecutionMetadata.model_validate(execution_data(**change))


def test_valid_execution_metadata_roundtrip():
    metadata = PaymentExecutionMetadata.model_validate(execution_data(
        status="capture_completed", provider_capture_id="capture_001"))
    assert PaymentExecutionMetadata.model_validate_json(metadata.model_dump_json()) == metadata
    uncertain = PaymentExecutionMetadata.model_validate(execution_data(
        status="order_outcome_uncertain", provider_order_id=None, failure_category="timeout",
        failure_code="TIMEOUT"))
    assert uncertain.provider_order_id is None


def test_extra_fields_sensitive_values_and_error_string():
    marker = "TOPSECRET-DO-NOT-LOG"
    for model, data in [
        (PaymentIntent, intent_data(access_token=marker)),
        (PaymentAuthorization, authorization_data(approved=marker)),
        (PaymentExecutionMetadata, execution_data(raw_response={"secret": marker})),
        (PaymentIntent, intent_data(purpose=f"access token {marker}")),
        (PaymentIntent, intent_data(purpose="Card 4111 1111 1111 1111")),
    ]:
        with pytest.raises(ValidationError) as caught:
            model.model_validate(data)
        assert marker not in str(caught.value)
    with pytest.raises(ValidationError):
        PaymentIntent.model_validate(intent_data(money={"amount": "12.34", "currency": "USD", "card": marker}))


def test_existing_trace_schema_remains_separate():
    assert "money" not in TraceRecord.model_fields
    assert "payment_authorization" not in TraceRecord.model_fields
