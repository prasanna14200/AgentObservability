"""Pure payment eligibility policy. Decisions are not payment capabilities."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Literal, Protocol

from pydantic import AwareDatetime, field_validator

from agentsentinel.schema.payment import PaymentAuthorization, PaymentIntent, PaymentModel, _check_id

POLICY_VERSION = "payment-policy-v1"
MIN_USD = Decimal("0.01")
MAX_USD = Decimal("10.00")  # Matches the isolated Sandbox REST adapter, not a production limit.

Outcome = Literal["ALLOW", "REVIEW", "BLOCK"]
ReasonCode = Literal[
    "ELIGIBLE", "REVIEW_AMOUNT_THRESHOLD", "SCOPE_MISMATCH", "UNSUPPORTED_CURRENCY",
    "UNSUPPORTED_ACTION", "AMOUNT_OUT_OF_RANGE", "AUTHORIZATION_MISSING",
    "AUTHORIZATION_LOOKUP_FAILED", "AUTHORIZATION_INTENT_MISMATCH",
    "AUTHORIZATION_OPERATION_MISMATCH", "AUTHORIZATION_SCOPE_MISMATCH",
    "AUTHORIZATION_MERCHANT_MISMATCH", "AUTHORIZATION_AMOUNT_MISMATCH",
    "AUTHORIZATION_CURRENCY_MISMATCH", "AUTHORIZATION_ACTION_MISMATCH",
    "AUTHORIZATION_TARGET_MISMATCH", "AUTHORIZATION_INACTIVE",
    "AUTHORIZATION_NOT_YET_VALID", "AUTHORIZATION_EXPIRED",
    "REVIEW_ADVISORY_SIGNAL",
]


class AuthenticatedScope(PaymentModel):
    """Context supplied by a trusted app entry point, never copied from an AI proposal."""

    principal_id: str
    scope_id: str

    @field_validator("principal_id", "scope_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return _check_id(value, "authenticated context identifier")


class AuthorizationRepository(Protocol):
    """Trusted application-owned lookup. Implementations must authenticate and bind scope."""

    def find(self, context: AuthenticatedScope, intent_id: str, operation_id: str) -> PaymentAuthorization | None: ...


class PolicyAudit(PaymentModel):
    """Fixed safe audit vocabulary; no raw requests or provider payloads."""

    authorization_lookup: Literal["skipped", "found", "missing", "error"]


class PolicyDecision(PaymentModel):
    decision_id: str
    operation_id: str
    outcome: Outcome
    reason_codes: tuple[ReasonCode, ...]
    policy_version: Literal["payment-policy-v1"]
    evaluated_at: AwareDatetime
    audit_metadata: PolicyAudit

    @field_validator("decision_id", "operation_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return _check_id(value, "decision identifier")

    @field_validator("reason_codes")
    @classmethod
    def nonempty_reasons(cls, value: tuple[ReasonCode, ...]) -> tuple[ReasonCode, ...]:
        if not value or len(value) > 20 or len(set(value)) != len(value):
            raise ValueError("reason codes must be nonempty, unique, and bounded")
        return value


@dataclass(frozen=True)
class PolicyConfig:
    """Review strictly above this USD amount; hard adapter limits cannot be raised."""

    review_above_usd: Decimal = Decimal("5.00")

    def __post_init__(self) -> None:
        if (not isinstance(self.review_above_usd, Decimal)
                or not self.review_above_usd.is_finite()
                or not MIN_USD <= self.review_above_usd <= MAX_USD
                or -self.review_above_usd.as_tuple().exponent > 2):
            raise ValueError("review threshold must be an exact USD amount within adapter limits")


class PaymentPolicy:
    """Evaluates data only. The caller must inject a trusted repository, context, and clock."""

    def __init__(
        self,
        repository: AuthorizationRepository,
        *,
        config: PolicyConfig = PolicyConfig(),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._repository = repository
        self._config = config
        self._clock = clock

    def evaluate(
        self, intent: PaymentIntent, context: AuthenticatedScope,
        *, advisory_signals: tuple[Literal["request_review"], ...] = (),
    ) -> PolicyDecision:
        """No authorization payload parameter; caller must control context and repository."""
        if not isinstance(intent, PaymentIntent) or not isinstance(context, AuthenticatedScope):
            raise TypeError("validated intent and authenticated context are required")
        if not isinstance(advisory_signals, tuple) or any(signal != "request_review" for signal in advisory_signals):
            raise ValueError("unsupported advisory signal")
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("trusted clock must return a timezone-aware datetime; no decision issued")

        hard: list[ReasonCode] = []
        if context.scope_id != intent.scope_id:
            hard.append("SCOPE_MISMATCH")
        if intent.money.currency != "USD":
            hard.append("UNSUPPORTED_CURRENCY")
        if intent.action not in {"create_order", "capture_order"}:
            hard.append("UNSUPPORTED_ACTION")
        if not MIN_USD <= intent.money.amount <= MAX_USD:
            hard.append("AMOUNT_OUT_OF_RANGE")

        lookup: Literal["skipped", "found", "missing", "error"] = "skipped"
        authorization: PaymentAuthorization | None = None
        if not hard:
            try:
                authorization = self._repository.find(context, intent.intent_id, intent.operation_id)
                if authorization is not None and not isinstance(authorization, PaymentAuthorization):
                    raise TypeError("repository returned an invalid record")
                if authorization is not None:
                    # Revalidate even if a repository returned a model modified with model_copy.
                    authorization = PaymentAuthorization.model_validate(authorization.model_dump(mode="python"))
                lookup = "found" if authorization is not None else "missing"
            except Exception:
                # Never expose repository exception text or credentials in a decision.
                authorization = None
                lookup = "error"
                hard.append("AUTHORIZATION_LOOKUP_FAILED")

            if lookup == "missing":
                hard.append("AUTHORIZATION_MISSING")
            elif authorization is not None:
                hard.extend(self._check_authorization(authorization, intent, context, now))

        if hard:
            outcome: Outcome = "BLOCK"
            reasons: tuple[ReasonCode, ...] = tuple(hard)
        elif intent.money.amount > self._config.review_above_usd or advisory_signals:
            review_reasons: list[ReasonCode] = []
            if intent.money.amount > self._config.review_above_usd:
                review_reasons.append("REVIEW_AMOUNT_THRESHOLD")
            if advisory_signals:
                review_reasons.append("REVIEW_ADVISORY_SIGNAL")
            outcome, reasons = "REVIEW", tuple(review_reasons)
        else:
            outcome, reasons = "ALLOW", ("ELIGIBLE",)

        # Stable correlation identifier, not a bearer token or replay guard.
        material = (
            f"{POLICY_VERSION}|{intent.operation_id}|{intent.intent_id}|{context.scope_id}|"
            f"{context.principal_id}|{now.isoformat()}|{outcome}|{','.join(reasons)}"
        )
        decision_id = "decision_" + hashlib.sha256(material.encode("ascii")).hexdigest()[:24]
        return PolicyDecision(
            decision_id=decision_id,
            operation_id=intent.operation_id,
            outcome=outcome,
            reason_codes=reasons,
            policy_version=POLICY_VERSION,
            evaluated_at=now,
            audit_metadata=PolicyAudit(authorization_lookup=lookup),
        )

    @staticmethod
    def _check_authorization(
        record: PaymentAuthorization, intent: PaymentIntent, context: AuthenticatedScope, now: datetime
    ) -> list[ReasonCode]:
        issues: list[ReasonCode] = []
        if record.intent_id != intent.intent_id:
            issues.append("AUTHORIZATION_INTENT_MISMATCH")
        if record.operation_id != intent.operation_id:
            issues.append("AUTHORIZATION_OPERATION_MISMATCH")
        if record.scope_id != context.scope_id or record.scope_id != intent.scope_id:
            issues.append("AUTHORIZATION_SCOPE_MISMATCH")
        if record.merchant_id != intent.merchant_id:
            issues.append("AUTHORIZATION_MERCHANT_MISMATCH")
        if record.money.currency != intent.money.currency:
            issues.append("AUTHORIZATION_CURRENCY_MISMATCH")
        if record.money.amount != intent.money.amount:
            issues.append("AUTHORIZATION_AMOUNT_MISMATCH")
        if record.action != intent.action:
            issues.append("AUTHORIZATION_ACTION_MISMATCH")
        if record.target_order_id != intent.target_order_id:
            issues.append("AUTHORIZATION_TARGET_MISMATCH")
        if record.status != "issued":
            issues.append("AUTHORIZATION_INACTIVE")
        if now < record.issued_at:
            issues.append("AUTHORIZATION_NOT_YET_VALID")
        if now >= record.expires_at:
            issues.append("AUTHORIZATION_EXPIRED")
        return issues
