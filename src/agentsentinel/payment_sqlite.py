"""Local SQLite payment safety store with durable operation and claim transitions."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from contextlib import closing, contextmanager, suppress
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterator, Literal, Protocol

from pydantic import AwareDatetime, field_validator, model_validator

from agentsentinel.payment_gateway import GatewayResult, Reservation, _fingerprint
from agentsentinel.payment_policy import AuthenticatedScope, PolicyDecision
from agentsentinel.paypal_sandbox import request_id
from agentsentinel.schema.payment import Money, PaymentAuthorization, PaymentIntent, PaymentModel, _check_id


class StoreUnavailable(RuntimeError):
    """Safe storage failure; never includes SQL or stored values."""


@dataclass(frozen=True)
class SafetyLimits:
    """Trusted application configuration; attempt counts and spend live in SQLite."""

    max_attempts_per_operation: int = 10
    max_attempts_per_trace: int = 100
    daily_spend_usd: Decimal = Decimal("100.00")
    merchant_daily_spend_usd: Decimal = Decimal("50.00")

    def __post_init__(self) -> None:
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in
               (self.max_attempts_per_operation, self.max_attempts_per_trace)):
            raise ValueError("attempt limits must be positive integers")
        for value in (self.daily_spend_usd, self.merchant_daily_spend_usd):
            if (not isinstance(value, Decimal) or not value.is_finite() or value <= 0
                    or -value.as_tuple().exponent > 2):
                raise ValueError("budget limits must be positive exact USD cents")


class ReconciliationSnapshot(PaymentModel):
    operation_id: str
    state: Literal["RESERVED", "CLAIMED", "UNCERTAIN"]
    action: Literal["create_order", "capture_order"]
    merchant_id: str
    amount_text: str
    currency: str
    target_order_id: str | None
    attempt_id: str | None
    idempotency_key: str | None
    provider_order_id: str | None
    provider_capture_id: str | None
    fingerprint: str


class VerifiedOutcome(PaymentModel):
    """Result from a trusted verifier, never from an agent or raw provider JSON."""

    operation_id: str
    status: Literal["COMPLETED", "NO_PAYMENT", "STILL_UNCERTAIN"]
    attempt_id: str | None
    idempotency_key: str | None
    provider_order_id: str | None = None
    provider_capture_id: str | None = None
    verified_at: AwareDatetime

    @field_validator("operation_id", "attempt_id", "provider_order_id", "provider_capture_id")
    @classmethod
    def valid_id(cls, value: str | None) -> str | None:
        return None if value is None else _check_id(value, "reconciliation identifier")

    @field_validator("idempotency_key")
    @classmethod
    def valid_key(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"as-(?:create|capture)-[0-9a-f]{32}", value):
            raise ValueError("invalid idempotency key format")
        return value

    @model_validator(mode="after")
    def completed_has_provider_id(self) -> VerifiedOutcome:
        if self.status == "COMPLETED" and self.provider_order_id is None:
            raise ValueError("completed reconciliation requires a provider order ID")
        if self.status == "NO_PAYMENT" and self.provider_capture_id is not None:
            raise ValueError("no-payment outcome cannot contain a capture ID")
        return self


class OperationAudit(PaymentModel):
    """Bounded status view for the trusted demo CLI; no request body or credentials."""

    operation_id: str
    state: Literal["RESERVED", "CLAIMED", "COMPLETED", "REJECTED", "REVIEW_REQUIRED", "UNCERTAIN"]
    attempt_id: str | None
    idempotency_key: str | None
    provider_order_id: str | None
    provider_capture_id: str | None


class OutcomeVerifier(Protocol):
    def verify(self, snapshot: ReconciliationSnapshot) -> VerifiedOutcome: ...


class TestOnlyOutcomeVerifier(ABC):
    """Fake verifier marker for offline tests."""

    @abstractmethod
    def verify(self, snapshot: ReconciliationSnapshot) -> VerifiedOutcome:
        ...


class SandboxReadOnlyVerifier(ABC):
    """Trusted application verifier restricted to read-only Sandbox status calls."""

    @abstractmethod
    def verify(self, snapshot: ReconciliationSnapshot) -> VerifiedOutcome:
        ...


class SQLitePaymentStore:
    """One local SQLite file for operations and trusted application-held records.

    There is intentionally no public authorization issuance/import method.
    """

    def __init__(
        self, path: Path, *, trusted_issuer_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        timeout: float = 1.0,
        limits: SafetyLimits = SafetyLimits(),
    ) -> None:
        self.path = Path(path)
        self.trusted_issuer_id = _check_id(trusted_issuer_id, "trusted issuer")
        self.clock = clock
        self.timeout = timeout
        if not isinstance(limits, SafetyLimits):
            raise ValueError("trusted safety limits required")
        self.limits = limits
        if str(path) == ":memory:" or timeout < 0:
            raise ValueError("a local file path and nonnegative timeout are required")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        try:
            with closing(self._connect()) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS operations (
                        operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                        principal_id TEXT NOT NULL, scope_id TEXT NOT NULL,
                        intent_id TEXT NOT NULL, merchant_id TEXT NOT NULL,
                        amount_text TEXT NOT NULL, currency TEXT NOT NULL,
                        action TEXT NOT NULL, target_order_id TEXT,
                        state TEXT NOT NULL CHECK(state IN
                          ('RESERVED','CLAIMED','COMPLETED','REJECTED','REVIEW_REQUIRED','UNCERTAIN')),
                        result_json TEXT, authorization_id TEXT, attempt_id TEXT,
                        idempotency_key TEXT, provider_order_id TEXT, provider_capture_id TEXT,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS authorizations (
                        authorization_id TEXT PRIMARY KEY, issuer_id TEXT NOT NULL,
                        principal_id TEXT NOT NULL, scope_id TEXT NOT NULL,
                        intent_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                        merchant_id TEXT NOT NULL, amount_text TEXT NOT NULL,
                        currency TEXT NOT NULL, action TEXT NOT NULL,
                        target_order_id TEXT, issued_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN
                          ('issued','revoked','consumed','expired')),
                        closed_at TEXT, record_origin TEXT NOT NULL CHECK(record_origin='application_issued'),
                        UNIQUE(principal_id, scope_id, intent_id, operation_id)
                    );
                    CREATE TABLE IF NOT EXISTS operation_events (
                        event_id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL,
                        from_state TEXT, to_state TEXT NOT NULL,
                        event_type TEXT NOT NULL, occurred_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS operation_attempts (
                        operation_id TEXT NOT NULL, trace_id TEXT NOT NULL,
                        count INTEGER NOT NULL CHECK(count > 0),
                        PRIMARY KEY(operation_id,trace_id)
                    );
                """)
        except sqlite3.Error:
            raise StoreUnavailable("payment store initialization unavailable") from None

    def _now(self) -> datetime:
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise StoreUnavailable("trusted store clock unavailable")
        return now

    @staticmethod
    def _event(conn: sqlite3.Connection, operation_id: str, before: str | None, after: str, kind: str, now: datetime) -> None:
        conn.execute(
            "INSERT INTO operation_events(operation_id,from_state,to_state,event_type,occurred_at) VALUES(?,?,?,?,?)",
            (operation_id, before, after, kind, now.isoformat()),
        )

    def reserve_intent(self, intent: PaymentIntent, context: AuthenticatedScope, fingerprint: str) -> Reservation:
        """Atomic new/duplicate decision across SQLite connections and processes."""
        try:
            intent = PaymentIntent.model_validate(intent.model_dump(mode="python"))
            context = AuthenticatedScope.model_validate(context.model_dump(mode="python"))
        except Exception:
            raise StoreUnavailable("invalid operation input") from None
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or fingerprint != _fingerprint(intent, context):
            raise StoreUnavailable("invalid operation binding")
        try:
            now = self._now()
            with self._write() as conn:
                # BEGIN IMMEDIATE serializes the attempt, budget check, and reservation.
                conn.execute("""INSERT INTO operation_attempts(operation_id,trace_id,count) VALUES(?,?,1)
                    ON CONFLICT(operation_id,trace_id) DO UPDATE SET count=count+1""",
                             (intent.operation_id, intent.trace_id))
                operation_count = conn.execute("SELECT SUM(count) FROM operation_attempts WHERE operation_id=?",
                                               (intent.operation_id,)).fetchone()[0]
                trace_count = conn.execute("SELECT SUM(count) FROM operation_attempts WHERE trace_id=?",
                                           (intent.trace_id,)).fetchone()[0]
                if operation_count > self.limits.max_attempts_per_operation:
                    return Reservation("SAFETY_BLOCK", guard_reason="OPERATION_LOOP_LIMIT")
                if trace_count > self.limits.max_attempts_per_trace:
                    return Reservation("SAFETY_BLOCK", guard_reason="TRACE_LOOP_LIMIT")
                row = conn.execute("SELECT fingerprint,state,result_json FROM operations WHERE operation_id=?",
                                   (intent.operation_id,)).fetchone()
                if row is not None:
                    if row["fingerprint"] != fingerprint:
                        return Reservation("CONFLICT")
                    result = GatewayResult.model_validate_json(row["result_json"]) if row["result_json"] else None
                    return Reservation(row["state"], result)
                if intent.action == "capture_order":
                    day = now.astimezone(timezone.utc).date()
                    spent_daily = spent_merchant = 0
                    rows = conn.execute("""SELECT o.merchant_id,o.amount_text,o.state,e.occurred_at
                        FROM operations o JOIN operation_events e ON e.operation_id=o.operation_id
                        AND e.event_type='reserve' WHERE o.action='capture_order'
                        AND o.state IN ('RESERVED','CLAIMED','UNCERTAIN','COMPLETED')""").fetchall()
                    for prior in rows:
                        prior_day = datetime.fromisoformat(prior["occurred_at"]).astimezone(timezone.utc).date()
                        # An unresolved reservation from a prior day may still settle
                        # today, so hold its amount until reconciliation releases it.
                        if prior_day != day and prior["state"] == "COMPLETED":
                            continue
                        cents = int(Decimal(prior["amount_text"]) * 100)
                        spent_daily += cents
                        if prior["merchant_id"] == intent.merchant_id:
                            spent_merchant += cents
                    requested = int(intent.money.amount * 100)
                    if spent_daily + requested > int(self.limits.daily_spend_usd * 100):
                        return Reservation("SAFETY_BLOCK", guard_reason="DAILY_BUDGET_LIMIT")
                    if spent_merchant + requested > int(self.limits.merchant_daily_spend_usd * 100):
                        return Reservation("SAFETY_BLOCK", guard_reason="MERCHANT_BUDGET_LIMIT")
                conn.execute("""INSERT INTO operations
                    (operation_id,fingerprint,principal_id,scope_id,intent_id,merchant_id,amount_text,currency,
                     action,target_order_id,state,provider_order_id,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (intent.operation_id, fingerprint, context.principal_id, context.scope_id,
                     intent.intent_id, intent.merchant_id, str(intent.money.amount), intent.money.currency,
                     intent.action, intent.target_order_id, "RESERVED", None, now.isoformat()),
                )
                self._event(conn, intent.operation_id, None, "RESERVED", "reserve", now)
                return Reservation("NEW")
        except Exception:
            raise StoreUnavailable("operation reservation unavailable") from None

    def _authorization(self, row: sqlite3.Row) -> PaymentAuthorization:
        return PaymentAuthorization.model_validate({
            "authorization_id": row["authorization_id"], "issuer_id": row["issuer_id"],
            "scope_id": row["scope_id"], "intent_id": row["intent_id"],
            "operation_id": row["operation_id"], "merchant_id": row["merchant_id"],
            "money": Money(amount=row["amount_text"], currency=row["currency"]),
            "action": row["action"], "target_order_id": row["target_order_id"],
            "issued_at": row["issued_at"], "expires_at": row["expires_at"],
            "status": row["status"], "closed_at": row["closed_at"],
            "record_origin": row["record_origin"],
        })

    def find(self, context: AuthenticatedScope, intent_id: str, operation_id: str) -> PaymentAuthorization | None:
        """Day 5 repository interface; principal and scope come from trusted context."""
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("""SELECT * FROM authorizations
                    WHERE principal_id=? AND scope_id=? AND intent_id=? AND operation_id=?""",
                    (context.principal_id, context.scope_id, intent_id, operation_id)).fetchone()
            if row is None:
                return None
            record = self._authorization(row)
            if record.issuer_id != self.trusted_issuer_id:
                return None
            return record
        except Exception:
            raise StoreUnavailable("authorization lookup unavailable") from None

    def claim(self, context: AuthenticatedScope, intent: PaymentIntent, decision: PolicyDecision) -> bool:
        """One transaction consumes authorization and marks the reserved operation CLAIMED."""
        try:
            intent = PaymentIntent.model_validate(intent.model_dump(mode="python"))
            context = AuthenticatedScope.model_validate(context.model_dump(mode="python"))
            decision = PolicyDecision.model_validate(decision.model_dump(mode="python"))
        except Exception:
            return False
        if decision.outcome != "ALLOW" or decision.operation_id != intent.operation_id:
            return False
        try:
            now = self._now()
            fingerprint = _fingerprint(intent, context)
            with self._write() as conn:
                operation = conn.execute("SELECT * FROM operations WHERE operation_id=?",
                                         (intent.operation_id,)).fetchone()
                if (operation is None or operation["fingerprint"] != fingerprint
                        or operation["state"] != "RESERVED"):
                    return False
                row = conn.execute("""SELECT * FROM authorizations
                    WHERE principal_id=? AND scope_id=? AND intent_id=? AND operation_id=?""",
                    (context.principal_id, context.scope_id, intent.intent_id, intent.operation_id)).fetchone()
                if row is None:
                    return False
                record = self._authorization(row)
                if (record.issuer_id != self.trusted_issuer_id or not record.binds_to(intent)
                        or record.status != "issued" or not record.issued_at <= now < record.expires_at):
                    return False
                changed = conn.execute("""UPDATE authorizations SET status='consumed',closed_at=?
                    WHERE authorization_id=? AND status='issued'""",
                    (now.isoformat(), record.authorization_id)).rowcount
                if changed != 1:
                    return False
                attempt_id = "attempt_" + hashlib.sha256(
                    f"{intent.operation_id}:{record.authorization_id}".encode("ascii")
                ).hexdigest()[:24]
                kind = "create" if intent.action == "create_order" else "capture"
                key = request_id(intent.merchant_id, kind, intent.operation_id)
                changed = conn.execute("""UPDATE operations SET state='CLAIMED',authorization_id=?,
                    attempt_id=?,idempotency_key=?,updated_at=? WHERE operation_id=? AND state='RESERVED'""",
                    (record.authorization_id, attempt_id, key, now.isoformat(), intent.operation_id)).rowcount
                if changed != 1:
                    raise StoreUnavailable("authorization claim unavailable")
                self._event(conn, intent.operation_id, "RESERVED", "CLAIMED", "claim", now)
                return True
        except Exception:
            raise StoreUnavailable("authorization claim unavailable") from None

    def finish(self, operation_id: str, fingerprint: str, result: GatewayResult) -> None:
        """Commit a sanitized terminal result after an adapter returns."""
        try:
            now = self._now()
            with self._write() as conn:
                row = conn.execute("SELECT state,fingerprint FROM operations WHERE operation_id=?",
                                   (operation_id,)).fetchone()
                if (row is None or row["fingerprint"] != fingerprint or result.operation_id != operation_id
                        or (row["state"] == "RESERVED" and result.status not in {"REJECTED", "REVIEW_REQUIRED"})
                        or (row["state"] == "CLAIMED" and result.status not in {"COMPLETED", "UNCERTAIN"})
                        or row["state"] not in {"RESERVED", "CLAIMED"}):
                    raise StoreUnavailable("invalid operation transition")
                conn.execute("""UPDATE operations SET state=?,result_json=?,
                    provider_order_id=COALESCE(?,provider_order_id),
                    provider_capture_id=COALESCE(?,provider_capture_id),updated_at=? WHERE operation_id=?""",
                    (result.status, result.model_dump_json(), result.provider_order_id,
                     result.provider_capture_id, now.isoformat(), operation_id))
                self._event(conn, operation_id, row["state"], result.status, "finish", now)
        except Exception:
            raise StoreUnavailable("operation finalization unavailable") from None

    def revoke(self, authorization_id: str, context: AuthenticatedScope) -> bool:
        """Trusted application-only revocation; no approval issuance or agent endpoint."""
        try:
            with self._write() as conn:
                changed = conn.execute("""UPDATE authorizations SET status='revoked',closed_at=?
                    WHERE authorization_id=? AND principal_id=? AND scope_id=? AND status='issued'""",
                    (self._now().isoformat(), authorization_id, context.principal_id, context.scope_id)).rowcount
                return changed == 1
        except Exception:
            raise StoreUnavailable("authorization revocation unavailable") from None

    def audit_operation(self, operation_id: str) -> OperationAudit | None:
        """Read-only safe operation view; application must control who can call it."""
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("""SELECT operation_id,state,attempt_id,idempotency_key,
                    provider_order_id,provider_capture_id FROM operations WHERE operation_id=?""",
                    (_check_id(operation_id, "operation identifier"),)).fetchone()
            return OperationAudit.model_validate(dict(row)) if row else None
        except Exception:
            raise StoreUnavailable("operation audit unavailable") from None

    def _snapshot(self, operation_id: str) -> ReconciliationSnapshot:
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or row["state"] not in {"RESERVED", "CLAIMED", "UNCERTAIN"}:
                raise StoreUnavailable("operation is not reconcilable")
            return ReconciliationSnapshot(
                operation_id=operation_id, state=row["state"], action=row["action"],
                merchant_id=row["merchant_id"], amount_text=row["amount_text"], currency=row["currency"],
                target_order_id=row["target_order_id"],
                attempt_id=row["attempt_id"], idempotency_key=row["idempotency_key"],
                provider_order_id=row["provider_order_id"], provider_capture_id=row["provider_capture_id"],
                fingerprint=row["fingerprint"],
            )
        except Exception:
            raise StoreUnavailable("reconciliation read unavailable") from None

    def _apply_verified(self, snapshot: ReconciliationSnapshot, outcome: VerifiedOutcome) -> GatewayResult:
        """Internal transition; caller must be the trusted reconciler, not an agent."""
        if (outcome.operation_id != snapshot.operation_id or outcome.attempt_id != snapshot.attempt_id
                or outcome.idempotency_key != snapshot.idempotency_key):
            raise StoreUnavailable("verified outcome does not match operation")
        if outcome.status == "COMPLETED" and (snapshot.attempt_id is None
                or (snapshot.action == "capture_order" and outcome.provider_capture_id is None)):
            raise StoreUnavailable("completed outcome lacks verified execution identifiers")
        if (snapshot.provider_order_id is not None and outcome.provider_order_id is not None
                and snapshot.provider_order_id != outcome.provider_order_id):
            raise StoreUnavailable("provider order mismatch")
        if (snapshot.action == "capture_order" and outcome.provider_order_id is not None
                and outcome.provider_order_id != snapshot.target_order_id):
            raise StoreUnavailable("verified capture order mismatch")
        if snapshot.action == "create_order" and outcome.provider_capture_id is not None:
            raise StoreUnavailable("create-order outcome cannot contain a capture ID")
        status = ("COMPLETED" if outcome.status == "COMPLETED" else
                  "REJECTED" if outcome.status == "NO_PAYMENT" else "UNCERTAIN")
        reason = ("VERIFIED_COMPLETED" if status == "COMPLETED" else
                  "VERIFIED_NO_PAYMENT" if status == "REJECTED" else "RECONCILIATION_REQUIRED")
        result = GatewayResult(operation_id=snapshot.operation_id, decision=None,
                               status=status, reason_codes=(reason,))
        try:
            with self._write() as conn:
                row = conn.execute("SELECT state,fingerprint,attempt_id,idempotency_key FROM operations WHERE operation_id=?",
                                   (snapshot.operation_id,)).fetchone()
                if (row is None or row["fingerprint"] != snapshot.fingerprint
                        or row["state"] not in {"RESERVED", "CLAIMED", "UNCERTAIN"}
                        or row["attempt_id"] != snapshot.attempt_id
                        or row["idempotency_key"] != snapshot.idempotency_key):
                    raise StoreUnavailable("operation changed during reconciliation")
                conn.execute("""UPDATE operations SET state=?,result_json=?,provider_order_id=?,
                    provider_capture_id=?,updated_at=? WHERE operation_id=?""",
                    (status, result.model_dump_json(), outcome.provider_order_id or snapshot.provider_order_id,
                     outcome.provider_capture_id or snapshot.provider_capture_id,
                     outcome.verified_at.isoformat(), snapshot.operation_id))
                self._event(conn, snapshot.operation_id, row["state"], status, "verified_reconciliation",
                            outcome.verified_at)
            return result
        except Exception:
            raise StoreUnavailable("reconciliation update unavailable") from None


class SQLiteReconciler:
    """Trusted app component; accepts only fake or read-only Sandbox verifiers."""

    def __init__(self, store: SQLitePaymentStore, verifier: OutcomeVerifier) -> None:
        if not isinstance(verifier, (TestOnlyOutcomeVerifier, SandboxReadOnlyVerifier)):
            raise TypeError("reconciliation requires a trusted verifier")
        self._store = store
        self._verifier = verifier

    def reconcile(self, operation_id: str) -> GatewayResult:
        """No agent-supplied status argument; never retries an adapter operation."""
        snapshot = self._store._snapshot(_check_id(operation_id, "operation identifier"))
        outcome = self._verifier.verify(snapshot)
        if not isinstance(outcome, VerifiedOutcome):
            raise StoreUnavailable("verifier returned an invalid outcome")
        outcome = VerifiedOutcome.model_validate(outcome.model_dump(mode="python"))
        return self._store._apply_verified(snapshot, outcome)
