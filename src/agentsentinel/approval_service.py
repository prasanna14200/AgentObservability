"""Durable approval queue and localhost-only human approval service."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import closing, contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import AwareDatetime, field_validator

from agentsentinel.payment_policy import AuthenticatedScope, PolicyAudit, PolicyDecision, POLICY_VERSION
from agentsentinel.schema.payment import Money, PaymentAction, PaymentAuthorization, PaymentIntent, PaymentModel, _check_id

ApprovalState = Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED"]
ApprovalReason = str


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
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("request fingerprint must be a SHA-256 hex digest")
        return value


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
    ) -> None:
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ttl_seconds = int(ttl_seconds)
        self.trusted_issuer_id = _check_id(trusted_issuer_id, "trusted issuer")
        self._initialize()

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
                        decision_reason TEXT
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
    ) -> ApprovalRecord:
        intent = PaymentIntent.model_validate(intent.model_dump(mode="python"))
        context = AuthenticatedScope.model_validate(context.model_dump(mode="python"))
        request_fingerprint = hashlib.sha256((intent.model_dump_json() + "|" + context.model_dump_json()).encode("utf-8")).hexdigest()
        approval_id = approval_id or self._approval_id_for(intent.operation_id, intent.trace_id, intent.intent_id)
        expires = expires_at or (self._now() + timedelta(seconds=self.ttl_seconds))
        with self._write() as conn:
            kill_row = conn.execute("SELECT enabled FROM kill_switch WHERE key='payment_kill_switch'").fetchone()
            if kill_row is not None and bool(kill_row["enabled"]):
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
                "INSERT INTO approvals(approval_id,operation_id,trace_id,intent_id,principal_id,scope_id,action,merchant_id,amount_text,currency,target_order_id,request_fingerprint,created_at,expires_at,status,reason_codes,decision_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                ),
            )
        self._event(approval_id, actor_role="system", action="approval_created", reason_code="APPROVAL_CREATED")
        return self.get_approval(approval_id)

    def create_from_intent(self, intent: PaymentIntent, context: AuthenticatedScope, **kwargs: Any) -> ApprovalRecord:
        return self.create_approval(intent=intent, context=context, **kwargs)

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        return None if row is None else self._row_to_approval(row)

    def list_approvals(self, *, status: str | None = None) -> list[ApprovalRecord]:
        self.expire_pending_approvals()
        with closing(self._connect()) as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM approvals ORDER BY created_at DESC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM approvals WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
        return [self._row_to_approval(row) for row in rows]

    def expire_pending_approvals(self) -> list[ApprovalRecord]:
        now = self._now()
        expired: list[ApprovalRecord] = []
        with self._write() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status='PENDING' AND expires_at <= ?",
                (now.isoformat(),),
            ).fetchall()
            for row in rows:
                record = self._row_to_approval(row)
                conn.execute(
                    "UPDATE approvals SET status='EXPIRED', decision_reason='EXPIRED' WHERE approval_id=? AND status='PENDING'",
                    (record.approval_id,),
                )
                expired.append(record)
                self._event(record.approval_id, actor_role="system", action="approval_expired", reason_code="APPROVAL_EXPIRED", conn=conn)
        return expired

    def reject_approval(self, approval_id: str, *, actor_role: str = "approver", reason_code: str = "HUMAN_REJECTED") -> ApprovalRecord:
        with self._write() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None:
                raise ValueError("unknown approval")
            record = self._row_to_approval(row)
            if record.status == "APPROVED":
                raise ValueError("approval already terminal")
            if record.status in {"REJECTED", "EXPIRED"}:
                raise ValueError("approval already terminal")
            conn.execute(
                "UPDATE approvals SET status='REJECTED', decision_reason=? WHERE approval_id=? and status='PENDING'",
                (reason_code, approval_id),
            )
        self._event(approval_id, actor_role=actor_role, action="approval_rejected", reason_code=reason_code)
        return self.get_approval(approval_id)

    def approve_approval(
        self,
        approval_id: str,
        *,
        actor_role: str = "approver",
        request_fingerprint: str | None = None,
        reason_code: str = "APPROVED",
    ) -> ApprovalRecord:
        now = self._now()
        with self._write() as conn:
            kill_row = conn.execute("SELECT enabled FROM kill_switch WHERE key='payment_kill_switch'").fetchone()
            if kill_row is not None and bool(kill_row["enabled"]):
                raise ValueError("kill switch active")
            row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None:
                raise ValueError("unknown approval")
            record = self._row_to_approval(row)
            if request_fingerprint is not None and request_fingerprint != record.request_fingerprint:
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
            )
            existing = conn.execute(
                "SELECT authorization_id FROM authorizations WHERE principal_id=? AND scope_id=? AND intent_id=? AND operation_id=?",
                (record.principal_id, record.scope_id, record.intent_id, record.operation_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO authorizations(authorization_id,issuer_id,principal_id,scope_id,intent_id,operation_id,merchant_id,amount_text,currency,action,target_order_id,issued_at,expires_at,status,closed_at,record_origin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    ),
                )
            conn.execute(
                "UPDATE approvals SET status='APPROVED', decision_reason=? WHERE approval_id=? AND status='PENDING'",
                (reason_code, approval_id),
            )
            self._event(approval_id, actor_role=actor_role, action="approval_approved", reason_code=reason_code, conn=conn)
        return self.get_approval(approval_id)

    def read_kill_switch(self) -> KillSwitchState:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM kill_switch WHERE key='payment_kill_switch'").fetchone()
        if row is None:
            return KillSwitchState(enabled=False, updated_at=self._now(), reason_code="KILL_SWITCH_DISABLED")
        return KillSwitchState(
            enabled=bool(row["enabled"]),
            updated_at=datetime.fromisoformat(row["updated_at"]).astimezone(timezone.utc),
            reason_code=row["reason_code"],
        )

    def is_kill_switch_active(self) -> bool:
        return self.read_kill_switch().enabled

    def set_kill_switch(self, *, enabled: bool, actor_role: str = "approver", reason_code: str | None = None) -> KillSwitchState:
        now = self._now()
        reason = reason_code or ("KILL_SWITCH_ENABLED" if enabled else "KILL_SWITCH_DISABLED")
        with self._write() as conn:
            conn.execute(
                "INSERT INTO kill_switch(key,enabled,updated_at,reason_code) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at, reason_code=excluded.reason_code",
                ("payment_kill_switch", int(enabled), now.isoformat(), reason),
            )
        self._event("kill_switch", actor_role=actor_role, action="kill_switch_enabled" if enabled else "kill_switch_disabled", reason_code=reason)
        return self.read_kill_switch()

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
    ttl_seconds: int = 3600,
    host: str = "127.0.0.1",
) -> FastAPI:
    db_path = Path(db_path or os.environ.get("APPROVAL_DB_PATH", "data/paypal_sandbox/gateway_demo.sqlite"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = ApprovalStore(db_path, ttl_seconds=ttl_seconds)
    app = FastAPI(title="AgentSentinel approval service", docs_url="/docs", redoc_url=None)
    app.state.approval_store = store
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/approvals")
    async def approvals(request: Request, status: str | None = None) -> Any:
        _require_bearer(request, approver_secret)
        records = store.list_approvals(status=status)
        if "text/html" in request.headers.get("accept", ""):
            csrf = secrets.token_urlsafe(32)
            response = templates.TemplateResponse(
                "approvals.html",
                {"request": request, "items": [record.model_dump(mode="python") for record in records], "status": status or "ALL"},
                headers={"X-CSRF-Token": csrf},
            )
            response.set_cookie("csrf_token", csrf, httponly=True, samesite="lax", secure=False)
            return response
        return [item.model_dump(mode="json") for item in records]

    @app.get("/approvals/{approval_id}")
    async def approval_detail(request: Request, approval_id: str) -> dict[str, Any]:
        _require_bearer(request, approver_secret)
        record = store.get_approval(approval_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
        return record.model_dump(mode="json")

    @app.post("/approvals/{approval_id}/approve")
    async def approve(request: Request, approval_id: str) -> dict[str, Any]:
        _require_bearer(request, approver_secret)
        _require_csrf(request)
        if store.is_kill_switch_active():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="kill switch active")
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        fingerprint = payload.get("request_fingerprint")
        record = store.approve_approval(approval_id, actor_role="approver", request_fingerprint=fingerprint, reason_code="APPROVED")
        return record.model_dump(mode="json")

    @app.post("/approvals/{approval_id}/reject")
    async def reject(request: Request, approval_id: str) -> dict[str, Any]:
        _require_bearer(request, approver_secret)
        _require_csrf(request)
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        reason = payload.get("reason_code", "HUMAN_REJECTED")
        record = store.reject_approval(approval_id, actor_role="approver", reason_code=str(reason))
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
        enabled = bool(payload.get("enabled", False))
        reason = payload.get("reason_code", "KILL_SWITCH_ENABLED" if enabled else "KILL_SWITCH_DISABLED")
        state = store.set_kill_switch(enabled=enabled, actor_role="approver", reason_code=str(reason))
        return {"enabled": state.enabled, "updated_at": state.updated_at.isoformat(), "reason_code": state.reason_code}

    @app.get("/audit")
    async def audit(request: Request) -> list[dict[str, Any]]:
        _require_bearer(request, approver_secret)
        return [event.model_dump(mode="json") for event in store.list_events()]

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
