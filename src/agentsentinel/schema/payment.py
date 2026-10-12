"""Provider-neutral payment data contracts. Parsing these models grants no authority."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class PaymentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


Identifier = str
PaymentAction = Literal["create_order", "capture_order"]
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,79}\Z")
_MERCHANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:-]{2,79}\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:https?://|\b(?:password|client[_ -]?secret|access[_ -]?token|bearer|cvv|card[_ -]?number)\b|"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|(?:\d[ -]?){13,19})"
)
_CURRENCY_DIGITS = {"USD": 2, "EUR": 2, "JPY": 0}


def _check_id(value: str, label: str, pattern: re.Pattern[str] = _ID) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{label} must be a bounded ASCII identifier")
    return value


class Money(PaymentModel):
    """Exact positive amount in a deliberately small currency allowlist."""

    amount: Decimal = Field(repr=False)
    currency: str

    @field_validator("amount", mode="before")
    @classmethod
    def exact_amount(cls, value: object) -> Decimal:
        if isinstance(value, Decimal):
            amount = value
        elif isinstance(value, str) and len(value) <= 18 and _DECIMAL.fullmatch(value):
            amount = Decimal(value)
        else:
            raise ValueError("amount must be a plain decimal string or Decimal")
        if not amount.is_finite() or amount <= 0 or amount > Decimal("999999999"):
            raise ValueError("amount must be finite, positive, and within schema bounds")
        return amount

    @field_validator("currency")
    @classmethod
    def supported_currency(cls, value: str) -> str:
        if value not in _CURRENCY_DIGITS:
            raise ValueError("unsupported currency; use a three-letter allowlisted code")
        return value

    @model_validator(mode="after")
    def currency_precision(self) -> Money:
        if -self.amount.as_tuple().exponent > _CURRENCY_DIGITS[self.currency]:
            raise ValueError("amount exceeds currency decimal precision")
        return self


class PaymentIntent(PaymentModel):
    """Untrusted proposal. No field or value here authorizes execution."""

    intent_id: Identifier
    operation_id: Identifier
    trace_id: Identifier
    scope_id: Identifier
    money: Money
    merchant_id: Identifier
    purpose: str = Field(min_length=1, max_length=160, repr=False)
    action: PaymentAction
    target_order_id: Identifier | None = None
    requested_at: AwareDatetime

    @field_validator("intent_id", "operation_id", "trace_id", "scope_id", "target_order_id")
    @classmethod
    def valid_id(cls, value: str | None) -> str | None:
        return None if value is None else _check_id(value, "identifier")

    @field_validator("merchant_id")
    @classmethod
    def valid_merchant(cls, value: str) -> str:
        return _check_id(value, "merchant", _MERCHANT)

    @field_validator("purpose")
    @classmethod
    def safe_purpose(cls, value: str) -> str:
        if value != value.strip() or not value.isprintable() or _SENSITIVE_TEXT.search(value):
            raise ValueError("purpose must be short, printable, and free of sensitive markers")
        return value

    @model_validator(mode="after")
    def target_matches_action(self) -> PaymentIntent:
        if (self.action == "capture_order") != (self.target_order_id is not None):
            raise ValueError("capture requires a target order; create must not specify one")
        return self


class PaymentAuthorization(PaymentModel):
    """Claimed application record; authenticity requires a trusted server-side lookup."""

    authorization_id: Identifier
    issuer_id: Identifier
    scope_id: Identifier
    intent_id: Identifier
    operation_id: Identifier
    merchant_id: Identifier
    money: Money
    action: PaymentAction
    target_order_id: Identifier | None = None
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    status: Literal["issued", "revoked", "consumed", "expired"]
    closed_at: AwareDatetime | None = None
    record_origin: Literal["application_issued"]

    @field_validator("authorization_id", "issuer_id", "scope_id", "intent_id", "operation_id", "target_order_id")
    @classmethod
    def valid_id(cls, value: str | None) -> str | None:
        return None if value is None else _check_id(value, "identifier")

    @field_validator("merchant_id")
    @classmethod
    def valid_merchant(cls, value: str) -> str:
        return _check_id(value, "merchant", _MERCHANT)

    @model_validator(mode="after")
    def consistent_record(self) -> PaymentAuthorization:
        if self.expires_at <= self.issued_at:
            raise ValueError("expiry must follow issuance")
        if (self.action == "capture_order") != (self.target_order_id is not None):
            raise ValueError("capture requires a target order; create must not specify one")
        if (self.status == "issued") != (self.closed_at is None):
            raise ValueError("issued records must be open; closed records need a closing timestamp")
        if self.closed_at is not None and self.closed_at < self.issued_at:
            raise ValueError("closure cannot precede issuance")
        return self

    def binds_to(self, intent: PaymentIntent) -> bool:
        """Compare request fields only; a match is never proof of consent or permission."""
        return all(
            (
                self.intent_id == intent.intent_id,
                self.operation_id == intent.operation_id,
                self.scope_id == intent.scope_id,
                self.merchant_id == intent.merchant_id,
                self.money == intent.money,
                self.action == intent.action,
                self.target_order_id == intent.target_order_id,
            )
        )


class PaymentExecutionMetadata(PaymentModel):
    """Sanitized telemetry only; never a raw provider response."""

    operation_id: Identifier
    provider: str
    attempt_id: Identifier
    status: Literal[
        "order_created", "awaiting_buyer_approval", "approved", "capture_pending",
        "capture_completed", "capture_failed", "order_outcome_uncertain", "capture_outcome_uncertain",
    ]
    observed_at: AwareDatetime
    provider_order_id: Identifier | None = None
    provider_capture_id: Identifier | None = None
    failure_category: Literal["validation", "provider_rejected", "network", "timeout", "unknown"] | None = None
    failure_code: Literal[
        "INVALID_REQUEST", "DECLINED", "TIMEOUT", "NETWORK_ERROR", "PROVIDER_ERROR", "UNKNOWN"
    ] | None = None

    @field_validator("operation_id", "attempt_id", "provider_order_id", "provider_capture_id")
    @classmethod
    def valid_id(cls, value: str | None) -> str | None:
        return None if value is None else _check_id(value, "identifier")

    @field_validator("provider")
    @classmethod
    def valid_provider(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", value):
            raise ValueError("provider must be a bounded lowercase identifier")
        return value

    @model_validator(mode="after")
    def consistent_status(self) -> PaymentExecutionMetadata:
        if self.provider_order_id is None and self.status != "order_outcome_uncertain":
            raise ValueError("execution metadata requires an order ID")
        if self.status == "order_outcome_uncertain" and self.provider_capture_id is not None:
            raise ValueError("uncertain order creation cannot contain a capture ID")
        if self.status == "capture_completed" and self.provider_capture_id is None:
            raise ValueError("completed capture requires a capture ID")
        if self.status in {"order_created", "awaiting_buyer_approval", "approved"} and self.provider_capture_id is not None:
            raise ValueError("pre-capture status cannot contain a capture ID")
        failed = self.status in {"capture_failed", "order_outcome_uncertain", "capture_outcome_uncertain"}
        if failed != (self.failure_category is not None):
            raise ValueError("failure category must match failure or uncertain status")
        if self.failure_code is not None and self.failure_category is None:
            raise ValueError("failure code requires failure category")
        return self
