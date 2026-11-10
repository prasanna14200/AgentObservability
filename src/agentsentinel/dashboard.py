"""Authenticated, local-only monitoring views over existing AgentSentinel stores."""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from agentsentinel.data.payment_synth_traces import generate_payment_traces
from agentsentinel.detectors.payment import PaymentDetectorAdapter
from agentsentinel.observability.payment_audit import (
    PaymentAuditChain,
    PaymentAuditUnavailable,
)
from agentsentinel.observability.payment_events import build_payment_event
from agentsentinel.schema.payment_telemetry import PaymentTelemetryEvent

_SESSION_COOKIE = "agentsentinel_session"
_CSRF_COOKIE = "csrf_token"
_SESSION_SECONDS = 3600
_TRACE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MAX_PAGE_SIZE = 50
_MAX_TRACE_EVENTS = 500
_MAX_ALERT_EVENTS = 1000
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
_ROOT = Path(__file__).resolve().parents[2]
_TELEMETRY_DB = _ROOT / "data" / "collected" / "payment_telemetry.sqlite"
_FAILURES_FILE = _ROOT / "data" / "collected" / "rejected" / "payment_telemetry.jsonl"


def _configured_secret(value: str | None, env_name: str) -> str:
    secret = value or os.environ.get(env_name)
    if not secret or len(secret) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="dashboard authentication unavailable",
        )
    return secret


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _sign_session(role: Literal["viewer", "approver"], signing_secret: str) -> str:
    payload = f"v1|{role}|{int(time.time()) + _SESSION_SECONDS}|{secrets.token_urlsafe(18)}"
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(signing_secret.encode("utf-8"), encoded.encode("ascii"), "sha256").hexdigest()
    return f"{encoded}.{signature}"


def _session_role(request: Request, signing_secret: str) -> Literal["viewer", "approver"]:
    token = request.cookies.get(_SESSION_COOKIE, "")
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected = hmac.new(
            signing_secret.encode("utf-8"), encoded.encode("ascii"), "sha256"
        ).hexdigest()
        if not hmac.compare_digest(supplied_signature, expected):
            raise ValueError
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        version, role, expires_at, nonce = payload.split("|", 3)
        if version != "v1" or role not in {"viewer", "approver"} or int(expires_at) <= int(time.time()) or not nonce:
            raise ValueError
        return role  # role is signed and only assigned after credential verification at login
    except (ValueError, UnicodeError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required") from None


def _read_only_connection(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _read_only(path: Path):
    conn = _read_only_connection(path)
    try:
        yield conn
    finally:
        if conn is not None:
            conn.close()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _csrf_token(request: Request) -> str:
    token = request.cookies.get(_CSRF_COOKIE, "")
    return token or secrets.token_urlsafe(32)


def _render(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    csrf = _csrf_token(request)
    response = _TEMPLATES.TemplateResponse(
        request=request,
        name=template,
        context={"csrf_token": csrf, **context},
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'self'; form-action 'self'; "
            "base-uri 'none'; object-src 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        },
    )
    response.set_cookie(
        _CSRF_COOKIE,
        csrf,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return response


async def _form_data(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > 4096:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="form too large")
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=12)
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid form") from None
    return {key: values[-1] for key, values in parsed.items() if values}


def _require_form_csrf(request: Request, form: dict[str, str]) -> None:
    cookie = request.cookies.get(_CSRF_COOKIE, "")
    supplied = form.get("csrf_token", "")
    if not cookie or not supplied or not _constant_time_equal(cookie, supplied):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf validation failed")


def _metric(value: Any, source: str, time_range: str) -> dict[str, str]:
    return {
        "value": "No data" if value is None else str(value),
        "source": source,
        "time_range": time_range,
    }


def _read_decision_counts(path: Path, since: str) -> dict[str, int] | None:
    with _read_only(path) as conn:
        if conn is None or not _has_table(conn, "payment_audit_chain"):
            return None
        rows = conn.execute(
            """
            SELECT CASE WHEN json_valid(canonical_event)
                        THEN json_extract(canonical_event, '$.gateway_decision') END AS decision,
                   COUNT(*) AS count
            FROM payment_audit_chain
            WHERE event_type='POLICY_EVALUATED'
              AND CASE WHEN json_valid(canonical_event)
                       THEN json_extract(canonical_event, '$.timestamp') END >= ?
            GROUP BY decision
            """,
            (since,),
        ).fetchall()
    if not rows:
        return None
    return {str(row["decision"]): int(row["count"]) for row in rows if row["decision"] is not None}


def _read_outcome_counts(path: Path, since: str) -> dict[str, int] | None:
    with _read_only(path) as conn:
        if conn is None or not _has_table(conn, "payment_audit_chain"):
            return None
        rows = conn.execute(
            """
            SELECT event_type, COUNT(*) AS count
            FROM payment_audit_chain
            WHERE event_type IN (
                'PAYMENT_COMPLETED', 'PAYMENT_FAILED', 'PAYMENT_UNCERTAIN', 'PAYMENT_REJECTED'
            )
              AND CASE WHEN json_valid(canonical_event)
                       THEN json_extract(canonical_event, '$.timestamp') END >= ?
            GROUP BY event_type
            """,
            (since,),
        ).fetchall()
    if not rows:
        return None
    return {str(row["event_type"]): int(row["count"]) for row in rows}


def _read_approval_metrics(path: Path, now: datetime) -> dict[str, int] | None:
    with _read_only(path) as conn:
        if conn is None or not _has_table(conn, "approvals"):
            return None
        rows = conn.execute(
            """
            SELECT status,
                   SUM(CASE WHEN status='PENDING' AND expires_at <= ? THEN 0 ELSE 1 END) AS active_count,
                   SUM(CASE WHEN status='PENDING' AND expires_at <= ? THEN 1 ELSE 0 END) AS expired_count
            FROM approvals GROUP BY status
            """,
            (now.isoformat(), now.isoformat()),
        ).fetchall()
    if not rows:
        return None
    counts = {"PENDING": 0, "APPROVED": 0, "REJECTED": 0, "EXPIRED": 0}
    for row in rows:
        state = str(row["status"])
        if state not in counts:
            continue
        if state == "PENDING":
            counts[state] += int(row["active_count"] or 0)
            counts["EXPIRED"] += int(row["expired_count"] or 0)
        else:
            counts[state] += int(row["active_count"] or 0)
    return counts


def _read_kill_switch(path: Path) -> tuple[str, str] | None:
    with _read_only(path) as conn:
        if conn is None or not _has_table(conn, "kill_switch"):
            return None
        row = conn.execute(
            "SELECT enabled,updated_at,reason_code FROM kill_switch WHERE key='payment_kill_switch'"
        ).fetchone()
    if row is None:
        return ("No persisted state (gateway default is disabled)", "approval database / kill_switch")
    return (
        "ENABLED" if bool(row["enabled"]) else "DISABLED",
        f"approval database / kill_switch, updated {row['updated_at']}",
    )


def _read_operation_count(path: Path) -> int | None:
    with _read_only(path) as conn:
        if conn is None or not _has_table(conn, "operations"):
            return None
        row = conn.execute("SELECT COUNT(*) AS count FROM operations").fetchone()
    return int(row["count"])


def _read_recent_failures(path: Path, since: datetime) -> int | None:
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 512 * 1024))
            raw_lines = handle.read().splitlines()[-1000:]
    except OSError:
        return None
    count = 0
    for raw in raw_lines:
        try:
            record = json.loads(raw)
            recorded_at = datetime.fromisoformat(record["recorded_at"]).astimezone(timezone.utc)
            if recorded_at >= since and record.get("status") in {"rejected", "unavailable"}:
                count += 1
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return count


def _read_trace_events(path: Path, trace_id: str) -> tuple[list[dict[str, Any]], bool]:
    with _read_only(path) as conn:
        if conn is None or not _has_table(conn, "payment_audit_chain"):
            return [], False
        rows = conn.execute(
            """
            SELECT sequence_number,canonical_event
            FROM payment_audit_chain
            WHERE CASE WHEN json_valid(canonical_event)
                       THEN json_extract(canonical_event, '$.trace_id') END=?
            ORDER BY CASE WHEN json_valid(canonical_event)
                          THEN json_extract(canonical_event, '$.timestamp') END,sequence_number
            LIMIT ?
            """,
            (trace_id, _MAX_TRACE_EVENTS + 1),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows[:_MAX_TRACE_EVENTS]:
        try:
            event = PaymentTelemetryEvent.model_validate_json(row["canonical_event"])
        except ValueError:
            continue
        events.append({**event.model_dump(mode="json"), "sequence_number": int(row["sequence_number"])})
    return events, len(rows) > _MAX_TRACE_EVENTS


def _read_operation_lifecycle(path: Path, operation_id: str) -> dict[str, str]:
    unavailable = {
        "operation_state": "Unavailable",
        "dispatch_state": "Unavailable",
        "reconciliation_state": "Unavailable",
    }
    if not operation_id or operation_id == "No data":
        return {key: "No data" for key in unavailable}
    try:
        with _read_only(path) as conn:
            if conn is None or not _has_table(conn, "operations"):
                return {key: "No data" for key in unavailable}
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(operations)")}
            if not {"operation_id", "state", "dispatch_state"} <= columns:
                return {key: "No data" for key in unavailable}
            row = conn.execute(
                "SELECT state,dispatch_state FROM operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                return {key: "No data" for key in unavailable}
            state = str(row["state"])
            dispatch_state = str(row["dispatch_state"])
            if state not in {
                "RESERVED", "CLAIMED", "COMPLETED", "REJECTED",
                "REVIEW_REQUIRED", "UNCERTAIN",
            } or dispatch_state not in {"NOT_DISPATCHED", "MAY_HAVE_DISPATCHED"}:
                return unavailable
            reconciled = False
            if _has_table(conn, "operation_events"):
                reconciled = conn.execute(
                    "SELECT 1 FROM operation_events WHERE operation_id=? "
                    "AND event_type='verified_reconciliation' LIMIT 1",
                    (operation_id,),
                ).fetchone() is not None
    except sqlite3.Error:
        return unavailable

    reconciliation_state = (
        "VERIFIED" if reconciled
        else "RECONCILIATION_REQUIRED" if state == "UNCERTAIN"
        else "Not required"
    )
    return {
        "operation_state": state,
        "dispatch_state": dispatch_state,
        "reconciliation_state": reconciliation_state,
    }


def _trace_summary(trace_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = [event for event in events if event["event_type"] == "POLICY_EVALUATED"]
    outcomes = [
        event for event in events
        if event["event_type"] in {
            "PAYMENT_COMPLETED", "PAYMENT_FAILED", "PAYMENT_UNCERTAIN",
            "PAYMENT_BLOCKED", "PAYMENT_REJECTED"
        }
    ]
    reason_codes = list(dict.fromkeys(
        reason for event in events for reason in event.get("reason_codes", [])
    ))
    detector_events = [
        event for event in events
        if event.get("detector_version") is not None and event.get("anomaly_score") is not None
    ]
    dispatches = sum(event["event_type"] == "PAYMENT_DISPATCHED" for event in events)
    advisory = " | ".join(
        f"{event['detector_version']} advisory score {event['anomaly_score']}"
        for event in detector_events
    ) or ("Repeated dispatches (advisory)" if dispatches > 3 else "No persisted signal")
    return {
        "trace_id": trace_id,
        "operation_id": next((event.get("operation_id") for event in events if event.get("operation_id")), "No data"),
        "payment_intent_id": next(
            (event.get("payment_intent_id") for event in events if event.get("payment_intent_id")),
            "No data",
        ),
        "approval_id": next(
            (event.get("approval_id") for event in reversed(events) if event.get("approval_id")),
            "No data",
        ),
        "amount_text": next(
            (event.get("amount_text") for event in events if event.get("amount_text")),
            "No data",
        ),
        "currency": next(
            (event.get("currency") for event in events if event.get("currency")),
            "No data",
        ),
        "event_timestamp": max((event["timestamp"] for event in events), default="No data"),
        "action": next((event.get("action") for event in reversed(events) if event.get("action")), "No data"),
        "decision": decisions[-1].get("gateway_decision") if decisions else "No data",
        "policy_version": next(
            (event.get("policy_version") for event in reversed(decisions) if event.get("policy_version")),
            "No data",
        ),
        "reason_codes": reason_codes,
        "provider_outcome": outcomes[-1]["event_type"].removeprefix("PAYMENT_") if outcomes else "No data",
        "anomaly_signal": advisory,
        "event_count": len(events),
    }


def _read_trace_page(
    path: Path,
    *,
    search: str,
    days: int,
    page: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], int]:
    with _read_only(path) as conn:
        if conn is None or not _has_table(conn, "payment_audit_chain"):
            return [], 0
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        where = """
            WHERE CASE WHEN json_valid(canonical_event)
                       THEN json_extract(canonical_event, '$.trace_id') END IS NOT NULL
              AND CASE WHEN json_valid(canonical_event)
                       THEN json_extract(canonical_event, '$.timestamp') END >= ?
              AND (?='' OR instr(CASE WHEN json_valid(canonical_event)
                                      THEN json_extract(canonical_event, '$.trace_id') END, ?) > 0
                   OR instr(COALESCE(CASE WHEN json_valid(canonical_event)
                                          THEN json_extract(canonical_event, '$.operation_id') END, ''), ?) > 0)
        """
        params = (since, search, search, search)
        total = int(conn.execute(
            "SELECT COUNT(DISTINCT CASE WHEN json_valid(canonical_event) "
            "THEN json_extract(canonical_event, '$.trace_id') END) "
            "FROM payment_audit_chain " + where,
            params,
        ).fetchone()[0] or 0)
        rows = conn.execute(
            """
            SELECT CASE WHEN json_valid(canonical_event)
                        THEN json_extract(canonical_event, '$.trace_id') END AS trace_id,
                   MAX(CASE WHEN json_valid(canonical_event)
                            THEN json_extract(canonical_event, '$.timestamp') END) AS latest
            FROM payment_audit_chain
            """
            + where
            + " GROUP BY trace_id ORDER BY latest DESC,trace_id LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    summaries = []
    for row in rows:
        trace_id = row["trace_id"]
        events, truncated = _read_trace_events(path, trace_id)
        summary = _trace_summary(trace_id, events)
        summary["truncated"] = truncated
        summaries.append(summary)
    return summaries, total


def _read_alerts(path: Path, since: str) -> list[dict[str, str]]:
    with _read_only(path) as conn:
        if conn is None or not _has_table(conn, "payment_audit_chain"):
            return []
        rows = conn.execute(
            """
            SELECT canonical_event FROM payment_audit_chain
            WHERE CASE WHEN json_valid(canonical_event)
                       THEN json_extract(canonical_event, '$.timestamp') END >= ?
            ORDER BY sequence_number DESC LIMIT ?
            """,
            (since, _MAX_ALERT_EVENTS),
        ).fetchall()
    alerts: list[dict[str, str]] = []
    dispatches: dict[str, int] = {}
    trace_events: dict[str, list[PaymentTelemetryEvent]] = {}
    for row in rows:
        try:
            event = PaymentTelemetryEvent.model_validate_json(row["canonical_event"])
        except ValueError:
            continue
        reason = ", ".join(event.reason_codes) or "No reason code"
        if event.event_type == "POLICY_EVALUATED" and event.gateway_decision == "BLOCK":
            alerts.append({"kind": "Deterministic enforcement", "label": "Policy BLOCK", "trace_id": event.trace_id or "No data", "details": reason, "timestamp": event.timestamp.isoformat()})
        elif event.event_type == "POLICY_EVALUATED" and event.gateway_decision == "REVIEW":
            alerts.append({"kind": "Deterministic enforcement", "label": "Human review required", "trace_id": event.trace_id or "No data", "details": reason, "timestamp": event.timestamp.isoformat()})
        elif event.event_type in {"KILL_SWITCH_ENABLED", "KILL_SWITCH_DISABLED"}:
            alerts.append({"kind": "Control activity", "label": event.event_type.replace("_", " "), "trace_id": event.trace_id or "No trace", "details": reason, "timestamp": event.timestamp.isoformat()})
        elif event.event_type == "PAYMENT_UNCERTAIN":
            alerts.append({"kind": "Operational alert", "label": "Uncertain payment outcome", "trace_id": event.trace_id or "No data", "details": reason, "timestamp": event.timestamp.isoformat()})
        elif event.detector_version is not None and event.anomaly_score is not None:
            alerts.append({"kind": "Advisory detection (not confirmed fraud)", "label": event.detector_version, "trace_id": event.trace_id or "No data", "details": f"score={event.anomaly_score}; {reason}", "timestamp": event.timestamp.isoformat()})
        elif event.event_type == "PAYMENT_DISPATCHED" and event.trace_id:
            dispatches[event.trace_id] = dispatches.get(event.trace_id, 0) + 1
        if event.trace_id:
            trace_events.setdefault(event.trace_id, []).append(event)
        if any(code in {"LOOP_LIMIT_EXCEEDED", "PAYMENT_RETRY_STORM"} for code in event.reason_codes):
            alerts.append({
                "kind": "Deterministic / operational alert",
                "label": "Retry or loop limit",
                "trace_id": event.trace_id or "No data",
                "details": reason,
                "timestamp": event.timestamp.isoformat(),
            })
    rules = PaymentDetectorAdapter()
    for trace_id, events in trace_events.items():
        detection = rules.rules(events)
        if detection.anomaly:
            alerts.append({
                "kind": "Advisory detection (not confirmed fraud)",
                "label": detection.detector_version,
                "trace_id": trace_id,
                "details": ", ".join(detection.reason_codes) or "Anomalous operational pattern",
                "timestamp": max(event.timestamp for event in events).isoformat(),
            })
        if dispatches.get(trace_id, 0) > 3:
            alerts.append({
                "kind": "Advisory operational signal",
                "label": "Repeated dispatches",
                "trace_id": trace_id,
                "details": f"{dispatches[trace_id]} dispatch events; not a fraud determination",
                "timestamp": max(event.timestamp for event in trace_events[trace_id]).isoformat(),
            })
    return alerts[:100]


def _read_approval_rows(path: Path, now: datetime) -> list[dict[str, Any]] | None:
    with _read_only(path) as conn:
        if conn is None or not _has_table(conn, "approvals"):
            return None
        rows = conn.execute(
            """
            SELECT approval_id,operation_id,trace_id,intent_id,action,merchant_id,amount_text,
                   currency,target_order_id,request_fingerprint,created_at,expires_at,status,reason_codes
            FROM approvals ORDER BY created_at DESC LIMIT 501
            """
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            reasons = json.loads(row["reason_codes"])
            expires = datetime.fromisoformat(row["expires_at"]).astimezone(timezone.utc)
            state = "EXPIRED" if row["status"] == "PENDING" and expires <= now else row["status"]
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if state not in {"PENDING", "APPROVED", "REJECTED", "EXPIRED"}:
            continue
        result.append({
            "approval_id": row["approval_id"],
            "operation_id": row["operation_id"],
            "action": row["action"],
            "merchant_id": row["merchant_id"],
            "amount_text": row["amount_text"],
            "currency": row["currency"],
            "target_order_id": row["target_order_id"],
            "request_fingerprint": row["request_fingerprint"],
            "expires_at": row["expires_at"],
            "status": state,
            "reason_codes": reasons if isinstance(reasons, list) else [],
        })
    return result


def _audit_summary(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "No data", "records_checked": "No data", "first_broken_sequence": "No data", "verified_at": "No data", "mode": "No chain file"}
    try:
        result = PaymentAuditChain.open_readonly(path).verify()
    except (PaymentAuditUnavailable, OSError):
        return {"status": "Unavailable", "records_checked": "No data", "first_broken_sequence": "No data", "verified_at": "No data", "mode": "Live verification failed"}
    if result.records_checked == 0 and result.valid:
        state = "No data"
    else:
        state = "VALID" if result.valid else "INVALID"
    return {
        "status": state,
        "records_checked": result.records_checked,
        "first_broken_sequence": result.first_broken_sequence or "None",
        "reason_code": result.reason_code or "None",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "mode": "Live verification; result not persisted",
    }


def _overview_context(
    *,
    payment_db_path: Path,
    telemetry_db_path: Path,
    failures_path: Path,
    store: Any,
    role: str,
    audit_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    time_range = f"Last 24 hours (since {since})"
    metrics: dict[str, dict[str, str]] = {}
    operation_count = _read_operation_count(payment_db_path)
    metrics["operations"] = _metric(operation_count, "operations table", "All persisted operations")
    decisions = _read_decision_counts(telemetry_db_path, since)
    for decision in ("ALLOW", "BLOCK", "REVIEW"):
        value = None if decisions is None else decisions.get(decision, 0)
        metrics[f"decision_{decision.lower()}"] = _metric(value, "payment_audit_chain / POLICY_EVALUATED", time_range)
    outcomes = _read_outcome_counts(telemetry_db_path, since)
    for event_type, key in (
        ("PAYMENT_COMPLETED", "completed"),
        ("PAYMENT_FAILED", "failed"),
        ("PAYMENT_UNCERTAIN", "uncertain"),
        ("PAYMENT_REJECTED", "rejected"),
    ):
        metrics[key] = _metric(
            None if outcomes is None else outcomes.get(event_type, 0),
            "payment_audit_chain lifecycle events",
            time_range,
        )
    approval_counts = _read_approval_metrics(payment_db_path, now)
    metrics["pending_approvals"] = _metric(
        None if approval_counts is None else approval_counts.get("PENDING", 0),
        "approvals table (unexpired PENDING)",
        "Current state",
    )
    metrics["kill_switch"] = _metric(
        (_read_kill_switch(payment_db_path) or ("No data", ""))[0],
        "approval database / kill_switch",
        "Current persisted state",
    )
    metrics["telemetry_failures"] = _metric(
        _read_recent_failures(failures_path, now - timedelta(hours=24)),
        "rejected/payment_telemetry.jsonl",
        time_range,
    )
    audit = audit_override or _audit_summary(telemetry_db_path)
    metrics["audit_status"] = _metric(audit["status"], "payment_audit_chain", "Live verification at page render")
    metrics["audit_records"] = _metric(audit["records_checked"], "payment_audit_chain", "All chained records checked")
    return {
        "role": role,
        "metrics": metrics,
        "decision_counts": decisions,
        "approval_counts": approval_counts,
        "audit": audit,
        "alerts": _read_alerts(telemetry_db_path, since),
        "time_range": time_range,
    }


def create_dashboard_router(
    *,
    store: Any,
    payment_db_path: Path,
    approver_secret: str | None = None,
    viewer_secret: str | None = None,
    telemetry_db_path: Path | None = None,
    failures_path: Path = _FAILURES_FILE,
) -> APIRouter:
    router = APIRouter()
    telemetry_db_path = telemetry_db_path or _TELEMETRY_DB

    def authenticated_role(request: Request) -> Literal["viewer", "approver"]:
        signing_secret = _configured_secret(approver_secret, "APPROVER_SECRET")
        return _session_role(request, signing_secret)

    def operator_role(request: Request) -> Literal["viewer", "approver"]:
        role = authenticated_role(request)
        if role != "approver":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="approver role required")
        return role

    @router.get("/dashboard/login", response_class=HTMLResponse)
    async def dashboard_login(request: Request) -> HTMLResponse:
        return _render(request, "dashboard_login.html", {"error": None})

    @router.post("/dashboard/login")
    async def dashboard_login_submit(request: Request) -> Response:
        form = await _form_data(request)
        _require_form_csrf(request, form)
        approver = _configured_secret(approver_secret, "APPROVER_SECRET")
        viewer = viewer_secret or os.environ.get("DASHBOARD_VIEWER_SECRET")
        supplied = form.get("credential", "")
        if len(supplied) > 512:
            supplied = ""
        if _constant_time_equal(supplied, approver):
            role: Literal["viewer", "approver"] = "approver"
        elif viewer and len(viewer) >= 32 and _constant_time_equal(supplied, viewer):
            role = "viewer"
        else:
            return _render(
                request,
                "dashboard_login.html",
                {"error": "Authentication failed."},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        response = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            _SESSION_COOKIE,
            _sign_session(role, approver),
            httponly=True,
            samesite="strict",
            secure=False,
            max_age=_SESSION_SECONDS,
            path="/",
        )
        response.set_cookie(
            _CSRF_COOKIE,
            form["csrf_token"],
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    @router.post("/dashboard/logout")
    async def dashboard_logout(request: Request) -> Response:
        authenticated_role(request)
        form = await _form_data(request)
        _require_form_csrf(request, form)
        response = RedirectResponse("/dashboard/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(_SESSION_COOKIE, path="/", samesite="strict")
        response.delete_cookie(_CSRF_COOKIE, path="/", samesite="strict")
        return response

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_overview(request: Request) -> HTMLResponse:
        role = authenticated_role(request)
        context = _overview_context(
            payment_db_path=payment_db_path,
            telemetry_db_path=telemetry_db_path,
            failures_path=failures_path,
            store=store,
            role=role,
        )
        return _render(request, "dashboard_overview.html", context)

    @router.get("/dashboard/traces", response_class=HTMLResponse)
    async def dashboard_traces(
        request: Request,
        q: str = "",
        page: int = 1,
        page_size: int = 25,
        days: int = 7,
    ) -> HTMLResponse:
        role = authenticated_role(request)
        if len(q) > 128:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="search too long")
        if page < 1 or page_size < 1 or page_size > _MAX_PAGE_SIZE or days not in {1, 7, 30}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid pagination or time range")
        traces, total = _read_trace_page(
            telemetry_db_path,
            search=q,
            days=days,
            page=page,
            page_size=page_size,
        )
        return _render(
            request,
            "dashboard_traces.html",
            {
                "role": role,
                "traces": traces,
                "q": q,
                "page": page,
                "page_size": page_size,
                "days": days,
                "total": total,
                "pages": max(1, (total + page_size - 1) // page_size),
            },
        )

    @router.get("/dashboard/traces/{trace_id}", response_class=HTMLResponse)
    async def dashboard_trace_detail(request: Request, trace_id: str) -> HTMLResponse:
        role = authenticated_role(request)
        if not _TRACE_ID.fullmatch(trace_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trace not found")
        events, truncated = _read_trace_events(telemetry_db_path, trace_id)
        if not events:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trace not found")
        trace = _trace_summary(trace_id, events)
        trace.update(_read_operation_lifecycle(payment_db_path, trace["operation_id"]))
        return _render(
            request,
            "dashboard_trace_detail.html",
            {"role": role, "trace": trace, "events": events, "truncated": truncated},
        )

    @router.get("/dashboard/alerts", response_class=HTMLResponse)
    async def dashboard_alerts(request: Request) -> HTMLResponse:
        role = authenticated_role(request)
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        alerts = _read_alerts(telemetry_db_path, since)
        return _render(
            request,
            "dashboard_alerts.html",
            {"role": role, "alerts": alerts, "time_range": f"Last 24 hours (since {since})"},
        )

    @router.get("/dashboard/approvals", response_class=HTMLResponse)
    async def dashboard_approvals(request: Request) -> HTMLResponse:
        role = authenticated_role(request)
        now = datetime.now(timezone.utc)
        items = _read_approval_rows(payment_db_path, now)
        return _render(
            request,
            "dashboard_approvals.html",
            {"role": role, "items": items, "now": now.isoformat()},
        )

    @router.post("/dashboard/approvals/{approval_id}/{decision}")
    async def dashboard_approval_action(request: Request, approval_id: str, decision: str) -> Response:
        operator_role(request)
        form = await _form_data(request)
        _require_form_csrf(request, form)
        if decision not in {"approve", "reject"} or form.get("confirm") != "yes":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="explicit confirmation required")
        record = store.get_approval(approval_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
        if not form.get("request_fingerprint") or not _constant_time_equal(
            form["request_fingerprint"], record.request_fingerprint
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="approval fingerprint mismatch")
        try:
            kill_switch = store.read_kill_switch()
            now = store._now()
        except (sqlite3.Error, RuntimeError, OSError):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="control state unavailable") from None
        if record.status != "PENDING":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="approval is not pending")
        if record.expires_at <= now:
            store.expire_pending_approvals(now=now)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="approval expired")
        if decision == "approve":
            if kill_switch.enabled:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="kill switch active")
            store.approve_approval(
                approval_id,
                actor_role="approver",
                request_fingerprint=record.request_fingerprint,
            )
        else:
            store.reject_approval(approval_id, actor_role="approver")
        return RedirectResponse("/dashboard/approvals", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/dashboard/killswitch")
    async def dashboard_kill_switch(request: Request) -> Response:
        operator_role(request)
        form = await _form_data(request)
        _require_form_csrf(request, form)
        if form.get("confirm") != "yes" or form.get("enabled") not in {"true", "false"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="explicit confirmation required")
        try:
            store.read_kill_switch()
            state = store.set_kill_switch(
                enabled=form["enabled"] == "true",
                actor_role="approver",
                reason_code="KILL_SWITCH_ENABLED" if form["enabled"] == "true" else "KILL_SWITCH_DISABLED",
            )
            if state.enabled != (form["enabled"] == "true"):
                raise RuntimeError("kill switch state transition did not persist")
        except (sqlite3.Error, RuntimeError, OSError):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="kill switch state unavailable") from None
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/dashboard/audit/verify", response_class=HTMLResponse)
    async def dashboard_verify_audit(request: Request) -> HTMLResponse:
        role = operator_role(request)
        form = await _form_data(request)
        _require_form_csrf(request, form)
        if form.get("confirm") != "yes":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="explicit confirmation required")
        audit = _audit_summary(telemetry_db_path)
        context = _overview_context(
            payment_db_path=payment_db_path,
            telemetry_db_path=telemetry_db_path,
            failures_path=failures_path,
            store=store,
            role=role,
            audit_override=audit,
        )
        return _render(request, "dashboard_overview.html", context)

    @router.get("/dashboard/demo", response_class=HTMLResponse)
    async def dashboard_demo(request: Request) -> HTMLResponse:
        role = authenticated_role(request)
        traces = generate_payment_traces(normal_count=70, per_scenario=1)
        by_scenario = {trace.scenario: trace for trace in traces}
        cases = [
            ("Normal completed event (synthetic only)", by_scenario["normal authorized payment"], "Synthetic PAYMENT_COMPLETED; no authorization created and no mock request executed."),
            ("Merchant mismatch blocked", by_scenario["merchant mismatch"], "AUTHORIZATION_MERCHANT_MISMATCH"),
            ("Amount mismatch blocked", by_scenario["amount mismatch"], "AUTHORIZATION_AMOUNT_MISMATCH"),
            ("Human review required", by_scenario["approval expiry"], "REVIEW and APPROVAL_REQUESTED are represented; no live approval is created."),
            ("Kill switch enabled then action blocked", by_scenario["kill-switch block"], "Synthetic KILL_SWITCH_ACTIVE block; no persistent switch is changed."),
            ("Uncertain provider outcome", by_scenario["uncertain provider outcome"], "Synthetic PAYMENT_UNCERTAIN; no provider call is made."),
            ("Advisory anomaly", by_scenario["retry storm"], "Existing rules adapter applied to synthetic repeat-dispatch events."),
        ]
        demo_rows = []
        adapter = PaymentDetectorAdapter()
        model = adapter.fit_isolation_forest(traces[:70])
        for label, trace, note in cases:
            summary = _trace_summary(trace.trace_id, [event.model_dump(mode="json") for event in trace.events])
            signal = adapter.rules(trace.events) if trace.scenario == "retry storm" else None
            forest = adapter.isolation_forest(trace.events, model) if trace.scenario == "retry storm" else None
            demo_rows.append({
                "label": label,
                "trace_id": trace.trace_id,
                "decision": summary["decision"],
                "reasons": ", ".join(summary["reason_codes"]) or "None",
                "outcome": summary["provider_outcome"],
                "advisory": "No persisted signal" if signal is None else (
                    f"{signal.detector_version}: {', '.join(signal.reason_codes) or 'no rule alert'}; "
                    f"{forest.detector_version}: {'anomaly' if forest and forest.anomaly else 'no alert'}"
                ),
                "note": note,
            })
        kill_trace_id = "demo_killswitch_trace"
        kill_op = "demo_killswitch_operation"
        kill_events = [
            build_payment_event(
                event_type="KILL_SWITCH_ENABLED",
                trace_id=kill_trace_id,
                operation_id=kill_op,
                reason_codes=("KILL_SWITCH_ENABLED",),
                actor_role="approver",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            build_payment_event(
                event_type="POLICY_EVALUATED",
                trace_id=kill_trace_id,
                operation_id=kill_op,
                gateway_decision="BLOCK",
                reason_codes=("KILL_SWITCH_ACTIVE",),
                actor_role="application",
                timestamp=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
            ),
            build_payment_event(
                event_type="PAYMENT_BLOCKED",
                trace_id=kill_trace_id,
                operation_id=kill_op,
                gateway_decision="BLOCK",
                reason_codes=("KILL_SWITCH_ACTIVE",),
                actor_role="application",
                timestamp=datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc),
            ),
        ]
        demo_rows[4]["trace_id"] = kill_trace_id
        demo_rows[4]["reasons"] = "KILL_SWITCH_ENABLED → KILL_SWITCH_ACTIVE"
        demo_rows[4]["note"] = "Deterministic synthetic sequence; persistent kill-switch state is untouched."
        with TemporaryDirectory(prefix="agentsentinel_dashboard_demo_") as temporary:
            chain = PaymentAuditChain(Path(temporary) / "demo.sqlite")
            for event in kill_events:
                chain.append(event)
            audit = chain.verify()
            audit_status = "VALID" if audit.valid else "INVALID"
        return _render(
            request,
            "dashboard_demo.html",
            {
                "role": role,
                "cases": demo_rows,
                "demo_audit_status": audit_status,
                "demo_audit_records": audit.records_checked,
                "seed": 20261028,
                "unsupported": "A real authorization-backed mock payment is intentionally not executed in this walkthrough. The normal row is a synthetic completed-event illustration only.",
            },
        )

    return router
