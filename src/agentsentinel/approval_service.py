"""Durable approval queue and localhost-only human approval service."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import AwareDatetime, field_validator

from agentsentinel.observability.payment_events import (
    PaymentTelemetryPublisher,
    build_payment_event,
)
from agentsentinel.payment_policy import AuthenticatedScope, PolicyAudit, PolicyDecision, POLICY_VERSION
from agentsentinel.schema.payment import Money, PaymentAction, PaymentAuthorization, PaymentIntent, PaymentModel, _check_id
from agentsentinel.schema.payment_telemetry import PaymentEventType

ApprovalState = Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED"]
ApprovalReason = str

# Operator reasons are exact codes, never free-form text. Approval codes:
# APPROVED, HUMAN_REJECTED, REJECTION_POLICY_CONCERN,
# REJECTION_INTENT_MISMATCH, REJECTION_UNEXPECTED_DETAILS,
# REJECTION_DUPLICATE, REJECTION_OTHER. Kill-switch codes:
# KILL_SWITCH_ENABLED, KILL_SWITCH_DISABLED, KILL_SWITCH_SECURITY_INCIDENT,
# KILL_SWITCH_MAINTENANCE. Codes are bounded to 64 uppercase ASCII characters
# and are validated against the action-specific allowlist before persistence.
APPROVAL_REASON_CODES = frozenset({
    "APPROVED",
    "HUMAN_REJECTED",
    "REJECTION_POLICY_CONCERN",
    "REJECTION_INTENT_MISMATCH",
    "REJECTION_UNEXPECTED_DETAILS",
    "REJECTION_DUPLICATE",
    "REJECTION_OTHER",
})
KILL_SWITCH_REASON_CODES = frozenset({
    "KILL_SWITCH_ENABLED",
    "KILL_SWITCH_DISABLED",
    "KILL_SWITCH_SECURITY_INCIDENT",
    "KILL_SWITCH_MAINTENANCE",
})


def _validate_reason_code(value: object, allowed: frozenset[str]) -> str:
    if type(value) is not str or len(value) > 64 or value not in allowed:
        raise ValueError("invalid reason code")
    return value


def _http_reason_code(value: object, allowed: frozenset[str]) -> str:
    try:
        return _validate_reason_code(value, allowed)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid reason code") from None


def _validate_request_fingerprint(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("request fingerprint must be a SHA-256 hex digest")
    return value


def _prefers_html(accept: str) -> bool:
    qualities: dict[str, float] = {}
    for media_range in accept.split(","):
        parts = [part.strip() for part in media_range.split(";")]
        media_type = parts[0].lower()
        if media_type not in {"text/html", "application/json", "*/*"}:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            name, separator, value = parameter.partition("=")
            if separator and name.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
                break
        qualities[media_type] = max(qualities.get(media_type, 0.0), quality)
    html_quality = qualities.get("text/html", 0.0)
    return (
        html_quality > 0
        and html_quality > qualities.get("application/json", 0.0)
        and html_quality > qualities.get("*/*", 0.0)
    )


def _effective_approval(record: "ApprovalRecord", now: datetime) -> "ApprovalRecord":
    if record.status == "PENDING" and record.expires_at <= now:
        return record.model_copy(update={"status": "EXPIRED"})
    return record


class ApprovalRecord(PaymentModel):
    approval_id: str
    operation_id: str
    trace_id: str
    intent_id: str
    principal_id: str
    scope_id: str
    action: PaymentAction
    merchant_id: str
    amount_text: str
    currency: str
    target_order_id: str | None = None
    request_fingerprint: str
    created_at: AwareDatetime
    expires_at: AwareDatetime
    status: ApprovalState
    reason_codes: tuple[str, ...]
    decision_reason: str | None = None

    @field_validator("approval_id", "operation_id", "trace_id", "intent_id", "principal_id", "scope_id", "merchant_id", "target_order_id")
    @classmethod
    def valid_id(cls, value: str | None) -> str | None:
        return None if value is None else _check_id(value, "approval identifier")

    @field_validator("request_fingerprint")
    @classmethod
    def valid_fingerprint(cls, value: str) -> str:
        return _validate_request_fingerprint(value)


class ApprovalEvent(PaymentModel):
    event_id: int
    approval_id: str
    actor_role: str
    action: str
    reason_code: str | None = None
    event_at: AwareDatetime
    details: str | None = None


class KillSwitchState(PaymentModel):
    enabled: bool
    updated_at: AwareDatetime
    reason_code: str | None = None


class ApprovalStore:
    """Durable approval queue and kill-switch state backed by SQLite."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Any | None = None,
        ttl_seconds: int = 3600,
        trusted_issuer_id: str = "trusted_approval_service",
        telemetry_publisher: PaymentTelemetryPublisher | None = None,
    ) -> None:
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ttl_seconds = int(ttl_seconds)
        self.trusted_issuer_id = _check_id(trusted_issuer_id, "trusted issuer")
        self.telemetry_publisher = telemetry_publisher
        self._initialize()

    def _publish_approval_event(
        self,
        event_type: PaymentEventType,
        record: ApprovalRecord,
        *,
        actor_role: str,
        reason_code: str | None = None,
    ) -> None:
        if self.telemetry_publisher is None:
            return
        safe_reason = reason_code if reason_code and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", reason_code) else None
        safe_actor = actor_role if actor_role in {"system", "approver", "application"} else "application"
        self.telemetry_publisher.publish(
            build_payment_event(
                event_type=event_type,
                trace_id=record.trace_id,
                operation_id=record.operation_id,
                payment_intent_id=record.intent_id,
                approval_id=record.approval_id,
                action=record.action,
                amount_text=record.amount_text,
                currency=record.currency,
                reason_codes=(safe_reason,) if safe_reason else (),
                actor_role=safe_actor,
            )
        )

    def _now(self) -> datetime:
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("trusted approval clock unavailable")
        return now

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=1.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def _write(self) -> Any:
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
                existing_tables = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0]
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS approvals (
                        approval_id TEXT PRIMARY KEY,
                        operation_id TEXT NOT NULL UNIQUE,
                        trace_id TEXT NOT NULL,
                        intent_id TEXT NOT NULL,
                        principal_id TEXT NOT NULL,
                        scope_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        merchant_id TEXT NOT NULL,
                        amount_text TEXT NOT NULL,
                        currency TEXT NOT NULL,
                        target_order_id TEXT,
                        request_fingerprint TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED','EXPIRED')),
                        reason_codes TEXT NOT NULL,
                        decision_reason TEXT,
                        intent_snapshot TEXT
                    );
                    CREATE TABLE IF NOT EXISTS authorizations (
                        authorization_id TEXT PRIMARY KEY,
                        issuer_id TEXT NOT NULL,
                        principal_id TEXT NOT NULL,
                        scope_id TEXT NOT NULL,
                        intent_id TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        merchant_id TEXT NOT NULL,
                        amount_text TEXT NOT NULL,
                        currency TEXT NOT NULL,
                        action TEXT NOT NULL,
                        target_order_id TEXT,
                        issued_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('issued','revoked','consumed','expired')),
                        closed_at TEXT,
                        record_origin TEXT NOT NULL CHECK(record_origin='application_issued'),
                        approval_id TEXT,
                        approval_reason_codes TEXT,
                        UNIQUE(principal_id, scope_id, intent_id, operation_id)
                    );
                    CREATE TABLE IF NOT EXISTS approval_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        approval_id TEXT NOT NULL,
                        actor_role TEXT NOT NULL,
                        action TEXT NOT NULL,
                        reason_code TEXT,
                        event_at TEXT NOT NULL,
                        details TEXT
                    );
                    CREATE TABLE IF NOT EXISTS kill_switch (
                        key TEXT PRIMARY KEY,
                        enabled INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        reason_code TEXT
                    );
                    """
                )
                conn.execute("BEGIN IMMEDIATE")
                try:
                    approval_columns = {
                        row["name"] for row in conn.execute("PRAGMA table_info(approvals)")
                    }
                    if "intent_snapshot" not in approval_columns:
                        conn.execute("ALTER TABLE approvals ADD COLUMN intent_snapshot TEXT")
                    authorization_columns = {
                        row["name"] for row in conn.execute("PRAGMA table_info(authorizations)")
                    }
                    if "approval_id" not in authorization_columns:
                        conn.execute("ALTER TABLE authorizations ADD COLUMN approval_id TEXT")
                    if "approval_reason_codes" not in authorization_columns:
                        conn.execute("ALTER TABLE authorizations ADD COLUMN approval_reason_codes TEXT")
                    if existing_tables == 0:
                        conn.execute(
                            "INSERT INTO kill_switch(key,enabled,updated_at,reason_code) VALUES(?,?,?,?)",
                            ("payment_kill_switch", 0, self._now().isoformat(), "KILL_SWITCH_DISABLED"),
                        )
                    conn.execute("COMMIT")
                except BaseException:
                    with suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                    raise
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            raise RuntimeError("approval store initialization unavailable") from exc

    @staticmethod
    def _approval_id_for(operation_id: str, trace_id: str, intent_id: str) -> str:
        digest = hashlib.sha256(f"{operation_id}:{trace_id}:{intent_id}".encode("utf-8")).hexdigest()[:24]
        return "approval_" + digest

    @staticmethod
    def _authorization_id_for(intent_id: str, operation_id: str, merchant_id: str) -> str:
        digest = hashlib.sha256(f"{intent_id}:{operation_id}:{merchant_id}".encode("utf-8")).hexdigest()[:24]
        return "auth_" + digest

    def _row_to_approval(self, row: sqlite3.Row) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=row["approval_id"],
            operation_id=row["operation_id"],
            trace_id=row["trace_id"],
            intent_id=row["intent_id"],
            principal_id=row["principal_id"],
            scope_id=row["scope_id"],
            action=row["action"],
            merchant_id=row["merchant_id"],
            amount_text=row["amount_text"],
            currency=row["currency"],
            target_order_id=row["target_order_id"],
            request_fingerprint=row["request_fingerprint"],
            created_at=datetime.fromisoformat(row["created_at"]).astimezone(timezone.utc),
            expires_at=datetime.fromisoformat(row["expires_at"]).astimezone(timezone.utc),
            status=row["status"],
            reason_codes=tuple(json.loads(row["reason_codes"])),
            decision_reason=row["decision_reason"],
        )

    @staticmethod
    def _kill_switch_enabled(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT enabled FROM kill_switch WHERE key='payment_kill_switch'"
        ).fetchone()
        if row is None or type(row["enabled"]) is not int or row["enabled"] not in (0, 1):
            raise RuntimeError("kill switch state unavailable")
        return row["enabled"] == 1

    def _event(
        self,
        approval_id: str,
        *,
        actor_role: str,
        action: str,
        reason_code: str | None = None,
        details: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if conn is not None:
            conn.execute(
                "INSERT INTO approval_events(approval_id,actor_role,action,reason_code,event_at,details) VALUES(?,?,?,?,?,?)",
                (approval_id, actor_role, action, reason_code, self._now().isoformat(), details),
            )
            return
        with self._write() as conn:
            conn.execute(
                "INSERT INTO approval_events(approval_id,actor_role,action,reason_code,event_at,details) VALUES(?,?,?,?,?,?)",
                (approval_id, actor_role, action, reason_code, self._now().isoformat(), details),
            )

    def create_approval(
        self,
        *,
        intent: PaymentIntent,
        context: AuthenticatedScope,
        reason_codes: tuple[str, ...] = ("PENDING",),
        approval_id: str | None = None,
        expires_at: datetime | None = None,
        defer_telemetry: bool = False,
    ) -> ApprovalRecord:
        intent = PaymentIntent.model_validate(intent.model_dump(mode="python"))
        context = AuthenticatedScope.model_validate(context.model_dump(mode="python"))
        request_fingerprint = hashlib.sha256((intent.model_dump_json() + "|" + context.model_dump_json()).encode("utf-8")).hexdigest()
        approval_id = approval_id or self._approval_id_for(intent.operation_id, intent.trace_id, intent.intent_id)
        expires = expires_at or (self._now() + timedelta(seconds=self.ttl_seconds))
        with self._write() as conn:
            if self._kill_switch_enabled(conn):
                raise ValueError("kill switch active")
            existing = conn.execute(
                "SELECT * FROM approvals WHERE operation_id=?",
                (intent.operation_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise ValueError("approval conflict: request fingerprint mismatch")
                return self._row_to_approval(existing)
            row = ApprovalRecord(
                approval_id=approval_id,
                operation_id=intent.operation_id,
                trace_id=intent.trace_id,
                intent_id=intent.intent_id,
                principal_id=context.principal_id,
                scope_id=context.scope_id,
                action=intent.action,
                merchant_id=intent.merchant_id,
                amount_text=str(intent.money.amount),
                currency=intent.money.currency,
                target_order_id=intent.target_order_id,
                request_fingerprint=request_fingerprint,
                created_at=self._now(),
                expires_at=expires,
                status="PENDING",
                reason_codes=tuple(reason_codes),
                decision_reason="PENDING",
            )
            conn.execute(
                "INSERT INTO approvals(approval_id,operation_id,trace_id,intent_id,principal_id,scope_id,action,merchant_id,amount_text,currency,target_order_id,request_fingerprint,created_at,expires_at,status,reason_codes,decision_reason,intent_snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row.approval_id,
                    row.operation_id,
                    row.trace_id,
                    row.intent_id,
                    row.principal_id,
                    row.scope_id,
                    row.action,
                    row.merchant_id,
                    row.amount_text,
                    row.currency,
                    row.target_order_id,
                    row.request_fingerprint,
                    row.created_at.isoformat(),
                    row.expires_at.isoformat(),
                    row.status,
                    json.dumps(list(row.reason_codes)),
                    row.decision_reason,
                    intent.model_dump_json(),
                ),
            )
            self._event(
                approval_id,
                actor_role="system",
                action="approval_created",
                reason_code="APPROVAL_CREATED",
                conn=conn,
            )
        record = self.get_approval(approval_id)
        if record is None:
            raise RuntimeError("created approval could not be read")
        if not defer_telemetry:
            self.publish_approval_requested(record)
        return record

    def publish_approval_requested(self, record: object) -> None:
        if not isinstance(record, ApprovalRecord):
            raise TypeError("approval record required")
        self._publish_approval_event(
            "APPROVAL_REQUESTED", record, actor_role="system", reason_code="APPROVAL_CREATED"
        )

    def create_from_intent(self, intent: PaymentIntent, context: AuthenticatedScope, **kwargs: Any) -> ApprovalRecord:
        return self.create_approval(intent=intent, context=context, **kwargs)

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        return None if row is None else self._row_to_approval(row)

    def approved_payment(
        self, approval_id: str
    ) -> tuple[PaymentIntent, AuthenticatedScope] | None:
        """Return the exact persisted request only after its human approval committed."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT a.*, z.authorization_id, z.issuer_id, z.principal_id AS auth_principal_id,
                       z.scope_id AS auth_scope_id, z.intent_id AS auth_intent_id,
                       z.operation_id AS auth_operation_id, z.merchant_id AS auth_merchant_id,
                       z.amount_text AS auth_amount_text, z.currency AS auth_currency,
                       z.action AS auth_action, z.target_order_id AS auth_target_order_id,
                       z.issued_at AS auth_issued_at, z.expires_at AS auth_expires_at,
                       z.status AS auth_status, z.closed_at AS auth_closed_at,
                       z.record_origin AS auth_record_origin, z.approval_id AS auth_approval_id,
                       z.approval_reason_codes AS auth_approval_reason_codes
                FROM approvals a
                JOIN authorizations z ON z.operation_id=a.operation_id
                WHERE a.approval_id=?
                """,
                (approval_id,),
            ).fetchone()
        if row is None or row["status"] != "APPROVED" or not row["intent_snapshot"]:
            return None
        try:
            intent = PaymentIntent.model_validate_json(row["intent_snapshot"])
            context = AuthenticatedScope(
                principal_id=row["principal_id"],
                scope_id=row["scope_id"],
            )
            expected_fingerprint = hashlib.sha256(
                (intent.model_dump_json() + "|" + context.model_dump_json()).encode("utf-8")
            ).hexdigest()
            authorization = PaymentAuthorization.model_validate({
                "authorization_id": row["authorization_id"],
                "issuer_id": row["issuer_id"],
                "scope_id": row["auth_scope_id"],
                "intent_id": row["auth_intent_id"],
                "operation_id": row["auth_operation_id"],
                "merchant_id": row["auth_merchant_id"],
                "money": Money(amount=row["auth_amount_text"], currency=row["auth_currency"]),
                "action": row["auth_action"],
                "target_order_id": row["auth_target_order_id"],
                "issued_at": row["auth_issued_at"],
                "expires_at": row["auth_expires_at"],
                "status": row["auth_status"],
                "closed_at": row["auth_closed_at"],
                "record_origin": row["auth_record_origin"],
                "approval_id": row["auth_approval_id"],
                "approval_reason_codes": tuple(
                    json.loads(row["auth_approval_reason_codes"] or "[]")
                ),
            })
            if (
                expected_fingerprint != row["request_fingerprint"]
                or authorization.issuer_id != self.trusted_issuer_id
                or authorization.approval_id != approval_id
                or authorization.approval_reason_codes != tuple(json.loads(row["reason_codes"]))
                or authorization.status not in {"issued", "consumed"}
                or not authorization.binds_to(intent)
                or row["auth_principal_id"] != context.principal_id
                or row["auth_scope_id"] != context.scope_id
            ):
                return None
        except (ValueError, TypeError, KeyError):
            return None
        return intent, context

    def list_approvals(self, *, status: str | None = None) -> list[ApprovalRecord]:
        with closing(self._connect()) as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM approvals ORDER BY created_at DESC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM approvals WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
        return [self._row_to_approval(row) for row in rows]

    def expire_pending_approvals(
        self,
        *,
        now: datetime | None = None,
        approval_id: str | None = None,
    ) -> list[ApprovalRecord]:
        now = now or self._now()
        expired: list[ApprovalRecord] = []
        with self._write() as conn:
            query = "SELECT * FROM approvals WHERE status='PENDING' AND expires_at <= ?"
            parameters: tuple[str, ...] = (now.isoformat(),)
            if approval_id is not None:
                query += " AND approval_id=?"
                parameters += (approval_id,)
            rows = conn.execute(query, parameters).fetchall()
            for row in rows:
                record = self._row_to_approval(row)
                conn.execute(
                    "UPDATE approvals SET status='EXPIRED', decision_reason='EXPIRED' WHERE approval_id=? AND status='PENDING'",
                    (record.approval_id,),
                )
                expired.append(record)
                self._event(record.approval_id, actor_role="system", action="approval_expired", reason_code="APPROVAL_EXPIRED", conn=conn)
        for record in expired:
            self._publish_approval_event(
                "APPROVAL_EXPIRED", record, actor_role="system", reason_code="APPROVAL_EXPIRED"
            )
        return expired

    def reject_approval(self, approval_id: str, *, actor_role: str = "approver", reason_code: str = "HUMAN_REJECTED") -> ApprovalRecord:
        reason_code = _validate_reason_code(reason_code, APPROVAL_REASON_CODES - {"APPROVED"})
        if self.get_approval(approval_id) is None:
            raise ValueError("unknown approval")
        expired = self.expire_pending_approvals(
            now=self._now(),
            approval_id=approval_id,
        )
        if expired:
            raise ValueError("approval expired")
        with self._write() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None:
                raise ValueError("unknown approval")
            record = self._row_to_approval(row)
            if record.status == "APPROVED":
                raise ValueError("approval already terminal")
            if record.status in {"REJECTED", "EXPIRED"}:
                raise ValueError("approval already terminal")
            changed = conn.execute(
                "UPDATE approvals SET status='REJECTED', decision_reason=? WHERE approval_id=? and status='PENDING'",
                (reason_code, approval_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("approval state transition unavailable")
            self._event(
                approval_id,
                actor_role=actor_role,
                action="approval_rejected",
                reason_code=reason_code,
                conn=conn,
            )
        updated = self.get_approval(approval_id)
        if updated is None:
            raise RuntimeError("rejected approval could not be read")
        self._publish_approval_event(
            "APPROVAL_REJECTED", updated, actor_role=actor_role, reason_code=reason_code
        )
        return updated

    def approve_approval(
        self,
        approval_id: str,
        *,
        actor_role: str = "approver",
        request_fingerprint: str,
        reason_code: str = "APPROVED",
    ) -> ApprovalRecord:
        request_fingerprint = _validate_request_fingerprint(request_fingerprint)
        reason_code = _validate_reason_code(reason_code, frozenset({"APPROVED"}))
        now = self._now()
        pending = self.get_approval(approval_id)
        if pending is None:
            raise ValueError("unknown approval")
        if not hmac.compare_digest(request_fingerprint, pending.request_fingerprint):
            raise ValueError("approval request fingerprint mismatch")
        expired = self.expire_pending_approvals(now=now, approval_id=approval_id)
        if any(record.approval_id == approval_id for record in expired):
            raise ValueError("approval expired")
        with self._write() as conn:
            if self._kill_switch_enabled(conn):
                raise ValueError("kill switch active")
            row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None:
                raise ValueError("unknown approval")
            record = self._row_to_approval(row)
            if not hmac.compare_digest(request_fingerprint, record.request_fingerprint):
                raise ValueError("approval request fingerprint mismatch")
            if record.status == "APPROVED":
                return record
            if record.status in {"REJECTED", "EXPIRED"}:
                raise ValueError("approval already terminal")
            if record.expires_at <= now:
                conn.execute("UPDATE approvals SET status='EXPIRED', decision_reason='EXPIRED' WHERE approval_id=?", (approval_id,))
                self._event(approval_id, actor_role="system", action="approval_expired", reason_code="APPROVAL_EXPIRED", conn=conn)
                raise ValueError("approval expired")
            auth_id = self._authorization_id_for(record.intent_id, record.operation_id, record.merchant_id)
            auth = PaymentAuthorization(
                authorization_id=auth_id,
                issuer_id=self.trusted_issuer_id,
                scope_id=record.scope_id,
                intent_id=record.intent_id,
                operation_id=record.operation_id,
                merchant_id=record.merchant_id,
                money=Money(amount=record.amount_text, currency=record.currency),
                action=record.action,
                target_order_id=record.target_order_id,
                issued_at=now,
                expires_at=record.expires_at,
                status="issued",
                record_origin="application_issued",
                approval_id=record.approval_id,
            )
            existing = conn.execute(
                "SELECT * FROM authorizations WHERE principal_id=? AND scope_id=? AND intent_id=? AND operation_id=?",
                (record.principal_id, record.scope_id, record.intent_id, record.operation_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO authorizations(authorization_id,issuer_id,principal_id,scope_id,intent_id,operation_id,merchant_id,amount_text,currency,action,target_order_id,issued_at,expires_at,status,closed_at,record_origin,approval_id,approval_reason_codes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        auth.authorization_id,
                        auth.issuer_id,
                        record.principal_id,
                        auth.scope_id,
                        auth.intent_id,
                        auth.operation_id,
                        auth.merchant_id,
                        str(auth.money.amount),
                        auth.money.currency,
                        auth.action,
                        auth.target_order_id,
                        auth.issued_at.isoformat(),
                        auth.expires_at.isoformat(),
                        auth.status,
                        None,
                        auth.record_origin,
                        auth.approval_id,
                        json.dumps(list(record.reason_codes)),
                    ),
                )
            else:
                existing_authorization = PaymentAuthorization.model_validate({
                    "authorization_id": existing["authorization_id"],
                    "issuer_id": existing["issuer_id"],
                    "scope_id": existing["scope_id"],
                    "intent_id": existing["intent_id"],
                    "operation_id": existing["operation_id"],
                    "merchant_id": existing["merchant_id"],
                    "money": Money(amount=existing["amount_text"], currency=existing["currency"]),
                    "action": existing["action"],
                    "target_order_id": existing["target_order_id"],
                    "issued_at": existing["issued_at"],
                    "expires_at": existing["expires_at"],
                    "status": existing["status"],
                    "closed_at": existing["closed_at"],
                    "record_origin": existing["record_origin"],
                    "approval_id": existing["approval_id"],
                    "approval_reason_codes": tuple(
                        json.loads(existing["approval_reason_codes"] or "[]")
                    ),
                })
                intent_snapshot = PaymentIntent.model_validate_json(
                    conn.execute(
                        "SELECT intent_snapshot FROM approvals WHERE approval_id=?",
                        (approval_id,),
                    ).fetchone()["intent_snapshot"]
                )
                if (
                    existing_authorization.issuer_id != self.trusted_issuer_id
                    or existing_authorization.status != "issued"
                    or existing_authorization.approval_id not in {None, approval_id}
                    or not existing_authorization.binds_to(intent_snapshot)
                ):
                    raise ValueError("existing authorization does not match approval")
                conn.execute(
                    "UPDATE authorizations SET approval_id=?,approval_reason_codes=? "
                    "WHERE authorization_id=? AND approval_id IS NULL",
                    (
                        approval_id,
                        json.dumps(list(record.reason_codes)),
                        existing_authorization.authorization_id,
                    ),
                )
            changed = conn.execute(
                "UPDATE approvals SET status='APPROVED', decision_reason=? WHERE approval_id=? AND status='PENDING'",
                (reason_code, approval_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("approval state transition unavailable")
            self._event(approval_id, actor_role=actor_role, action="approval_approved", reason_code=reason_code, conn=conn)
        approved = self.get_approval(approval_id)
        if approved is None:
            raise RuntimeError("approved record could not be read")
        self._publish_approval_event(
            "APPROVAL_APPROVED", approved, actor_role=actor_role, reason_code=reason_code
        )
        self._publish_approval_event(
            "APPROVAL_GRANTED", approved, actor_role=actor_role, reason_code=reason_code
        )
        return approved

    def read_kill_switch(self) -> KillSwitchState:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM kill_switch WHERE key='payment_kill_switch'").fetchone()
        if row is None or type(row["enabled"]) is not int or row["enabled"] not in (0, 1):
            raise RuntimeError("kill switch state unavailable")
        return KillSwitchState(
            enabled=row["enabled"] == 1,
            updated_at=datetime.fromisoformat(row["updated_at"]).astimezone(timezone.utc),
            reason_code=row["reason_code"],
        )

    def is_kill_switch_active(self) -> bool:
        return self.read_kill_switch().enabled

    def set_kill_switch(self, *, enabled: bool, actor_role: str = "approver", reason_code: str | None = None) -> KillSwitchState:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        now = self._now()
        reason = _validate_reason_code(
            reason_code if reason_code is not None else ("KILL_SWITCH_ENABLED" if enabled else "KILL_SWITCH_DISABLED"),
            KILL_SWITCH_REASON_CODES,
        )
        with self._write() as conn:
            conn.execute(
                "INSERT INTO kill_switch(key,enabled,updated_at,reason_code) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at, reason_code=excluded.reason_code",
                ("payment_kill_switch", int(enabled), now.isoformat(), reason),
            )
            self._event(
                "kill_switch",
                actor_role=actor_role,
                action="kill_switch_enabled" if enabled else "kill_switch_disabled",
                reason_code=reason,
                conn=conn,
            )
        state = self.read_kill_switch()
        if self.telemetry_publisher is not None:
            self.telemetry_publisher.publish(
                build_payment_event(
                    event_type="KILL_SWITCH_ENABLED" if enabled else "KILL_SWITCH_DISABLED",
                    actor_role=actor_role if actor_role in {"system", "approver", "application"} else "application",
                    reason_codes=(reason,) if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", reason) else (),
                    timestamp=now,
                )
            )
        return state

    def list_events(self, *, approval_id: str | None = None) -> list[ApprovalEvent]:
        with closing(self._connect()) as conn:
            if approval_id is None:
                rows = conn.execute("SELECT * FROM approval_events ORDER BY event_id DESC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM approval_events WHERE approval_id=? ORDER BY event_id DESC", (approval_id,)).fetchall()
        return [ApprovalEvent(event_id=row["event_id"], approval_id=row["approval_id"], actor_role=row["actor_role"], action=row["action"], reason_code=row["reason_code"], event_at=datetime.fromisoformat(row["event_at"]).astimezone(timezone.utc), details=row["details"]) for row in rows]


class ApprovalService:
    def __init__(self, store: ApprovalStore) -> None:
        self.store = store

    def create_approval(self, intent: PaymentIntent, context: AuthenticatedScope, **kwargs: Any) -> ApprovalRecord:
        return self.store.create_approval(intent=intent, context=context, **kwargs)

    def approve(self, approval_id: str, **kwargs: Any) -> ApprovalRecord:
        return self.store.approve_approval(approval_id, **kwargs)

    def reject(self, approval_id: str, **kwargs: Any) -> ApprovalRecord:
        return self.store.reject_approval(approval_id, **kwargs)


def _require_approver_secret(configured: str | None = None) -> str:
    value = configured or os.environ.get("APPROVER_SECRET")
    if not value or len(value) < 32:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="approver authentication unavailable")
    return value


def _require_bearer(request: Request, configured: str | None = None) -> None:
    secret = _require_approver_secret(configured)
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    token = header.split(" ", 1)[1].strip()
    if not hmac.compare_digest(token, secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")


def _require_csrf(request: Request) -> None:
    token = request.headers.get("X-CSRF-Token")
    cookie = request.cookies.get("csrf_token")
    if not token or not cookie or not hmac.compare_digest(token, cookie):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf validation failed")


def create_app(
    *,
    db_path: str | Path | None = None,
    approver_secret: str | None = None,
    viewer_secret: str | None = None,
    telemetry_db_path: str | Path | None = None,
    failures_path: str | Path | None = None,
    demo_mode: bool = False,
    ttl_seconds: int = 3600,
    host: str = "127.0.0.1",
) -> FastAPI:
    db_path = Path(db_path or os.environ.get("APPROVAL_DB_PATH", "data/paypal_sandbox/gateway_demo.sqlite"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = ApprovalStore(db_path, ttl_seconds=ttl_seconds)
    app = FastAPI(title="AgentSentinel approval service", docs_url="/docs", redoc_url=None)
    app.state.approval_store = store
    from fastapi.staticfiles import StaticFiles

    from agentsentinel.dashboard import create_dashboard_router

    app.mount(
        "/dashboard/static",
        StaticFiles(directory=str(Path(__file__).resolve().parent / "templates" / "static")),
        name="dashboard-static",
    )
    app.include_router(
        create_dashboard_router(
            store=store,
            payment_db_path=db_path,
            approver_secret=approver_secret,
            viewer_secret=viewer_secret,
            telemetry_db_path=Path(telemetry_db_path) if telemetry_db_path else None,
            failures_path=Path(failures_path) if failures_path else None,
            demo_mode=demo_mode,
        )
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def readiness() -> dict[str, str]:
        try:
            _require_approver_secret(approver_secret)
            store.read_kill_switch()
        except (HTTPException, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="service not ready",
            ) from None
        return {"status": "ready"}

    @app.get("/approvals")
    async def approvals(request: Request, status: str | None = None) -> Any:
        if _prefers_html(request.headers.get("accept", "")):
            return RedirectResponse(
                "/dashboard/approvals",
                status_code=303,
            )
        _require_bearer(request, approver_secret)
        now = store._now()
        records = [_effective_approval(record, now) for record in store.list_approvals()]
        if status is not None:
            records = [record for record in records if record.status == status]
        return [item.model_dump(mode="json") for item in records]

    @app.get("/approvals/{approval_id}")
    async def approval_detail(request: Request, approval_id: str) -> dict[str, Any]:
        _require_bearer(request, approver_secret)
        record = store.get_approval(approval_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
        return _effective_approval(record, store._now()).model_dump(mode="json")

    @app.post("/approvals/{approval_id}/approve")
    async def approve(request: Request, approval_id: str) -> dict[str, Any]:
        _require_bearer(request, approver_secret)
        _require_csrf(request)
        if store.is_kill_switch_active():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="kill switch active")
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        if not isinstance(payload, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid request")
        try:
            fingerprint = _validate_request_fingerprint(payload.get("request_fingerprint"))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="approval fingerprint mismatch",
            ) from None
        pending = store.get_approval(approval_id)
        if pending is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
        if not hmac.compare_digest(fingerprint, pending.request_fingerprint):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="approval fingerprint mismatch",
            )
        record = store.approve_approval(approval_id, actor_role="approver", request_fingerprint=fingerprint, reason_code="APPROVED")
        return record.model_dump(mode="json")

    @app.post("/approvals/{approval_id}/reject")
    async def reject(request: Request, approval_id: str) -> dict[str, Any]:
        _require_bearer(request, approver_secret)
        _require_csrf(request)
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        if not isinstance(payload, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid request")
        reason = _http_reason_code(
            payload.get("reason_code", "HUMAN_REJECTED"),
            APPROVAL_REASON_CODES - {"APPROVED"},
        )
        try:
            record = store.reject_approval(
                approval_id,
                actor_role="approver",
                reason_code=reason,
            )
        except ValueError as error:
            if str(error) != "approval expired":
                raise
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="approval expired",
            ) from None
        return record.model_dump(mode="json")

    @app.get("/killswitch")
    async def get_kill_switch(request: Request) -> dict[str, Any]:
        _require_bearer(request, approver_secret)
        state = store.read_kill_switch()
        return {"enabled": state.enabled, "updated_at": state.updated_at.isoformat(), "reason_code": state.reason_code}

    @app.post("/killswitch")
    async def set_kill_switch(request: Request) -> dict[str, Any]:
        _require_bearer(request, approver_secret)
        _require_csrf(request)
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="enabled must be a boolean")
        enabled = payload["enabled"]
        default_reason = "KILL_SWITCH_ENABLED" if enabled else "KILL_SWITCH_DISABLED"
        reason = _http_reason_code(payload.get("reason_code", default_reason), KILL_SWITCH_REASON_CODES)
        state = store.set_kill_switch(enabled=enabled, actor_role="approver", reason_code=reason)
        return {"enabled": state.enabled, "updated_at": state.updated_at.isoformat(), "reason_code": state.reason_code}

    @app.get("/audit")
    async def audit(request: Request) -> list[dict[str, Any]]:
        _require_bearer(request, approver_secret)
        return [event.model_dump(mode="json") for event in store.list_events()]

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)