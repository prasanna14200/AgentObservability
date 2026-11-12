"""Bounded public evaluator over AgentSentinel's trace and detection pipeline."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

import pandas as pd
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, ValidationError

from agentsentinel.data.preprocess import _flatten_trace
from agentsentinel.detectors.rules import apply_rules, get_rules_metadata
from agentsentinel.features.contextual import _add_permission_match, _add_trajectory_features
from agentsentinel.observability.collector import TraceCollector
from agentsentinel.schema.trace import TraceRecord

ScenarioName = Literal["normal_lookup", "repeated_calls", "unauthorized_update"]
SCENARIOS: dict[ScenarioName, dict[str, str]] = {
    "normal_lookup": {
        "label": "Normal customer/order lookup",
        "description": "Two bounded read-only mock tool calls; expected ALLOW.",
    },
    "repeated_calls": {
        "label": "Repeated tool calls",
        "description": "A bounded repeated order lookup; existing loop rules should detect it.",
    },
    "unauthorized_update": {
        "label": "Unauthorized simulated update",
        "description": "An update attempt without orders:write; it must not execute.",
    },
}
TOOL_REQUIRED_PERMISSIONS = {
    "search_customer": ["orders:read"],
    "get_order": ["orders:read"],
    "update_order": ["orders:read", "orders:write"],
}
TOOL_EXECUTORS = {
    "search_customer": lambda _arguments: {"customer_ref_hash": "cust_demo_001"},
    "get_order": lambda _arguments: {
        "order_ref_hash": "order_demo_001",
        "status": "SHIPPED",
    },
}
MAX_RUNS_PER_MINUTE = 10
MAX_STORED_EXECUTIONS = 200
_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates")
)


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: ScenarioName


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class EvaluatorExecutionStore:
    """Ephemeral SQLite record of bounded public scenario executions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS evaluator_executions (
                    request_id TEXT PRIMARY KEY,
                    scenario TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    evidence_json TEXT
                );
                CREATE TABLE IF NOT EXISTS evaluator_audit_events (
                    request_id TEXT NOT NULL REFERENCES evaluator_executions(request_id)
                        ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    occurred_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    PRIMARY KEY(request_id, ordinal)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def begin(self, request_id: str, scenario: ScenarioName, started_at: datetime) -> None:
        now_text = started_at.isoformat()
        cutoff = (started_at - timedelta(minutes=1)).isoformat()
        retention_cutoff = (started_at - timedelta(days=7)).isoformat()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM evaluator_executions WHERE started_at < ?",
                    (retention_cutoff,),
                )
                recent = connection.execute(
                    "SELECT COUNT(*) AS count, MIN(started_at) AS oldest "
                    "FROM evaluator_executions WHERE started_at >= ?",
                    (cutoff,),
                ).fetchone()
                if recent["count"] >= MAX_RUNS_PER_MINUTE:
                    oldest = datetime.fromisoformat(recent["oldest"])
                    retry_after = max(
                        1,
                        math.ceil(
                            (oldest + timedelta(minutes=1) - started_at).total_seconds()
                        ),
                    )
                    connection.rollback()
                    raise RateLimitExceeded(retry_after)
                connection.execute(
                    """INSERT INTO evaluator_executions
                       (request_id,scenario,started_at,status)
                       VALUES(?,?,?,'RUNNING')""",
                    (request_id, scenario, now_text),
                )
                connection.execute(
                    """DELETE FROM evaluator_executions
                       WHERE request_id IN (
                           SELECT request_id FROM evaluator_executions
                           ORDER BY started_at DESC LIMIT -1 OFFSET ?
                       )""",
                    (MAX_STORED_EXECUTIONS,),
                )
                connection.commit()
        except RateLimitExceeded:
            raise

    def complete(
        self,
        request_id: str,
        final_status: str,
        evidence: dict[str, Any],
    ) -> None:
        serialized = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for ordinal, event in enumerate(evidence["audit_events"]):
                connection.execute(
                    """INSERT INTO evaluator_audit_events
                       (request_id,ordinal,event_id,occurred_at,event_type,detail_json)
                       VALUES(?,?,?,?,?,?)""",
                    (
                        request_id,
                        ordinal,
                        event["event_id"],
                        event["timestamp"],
                        event["event_type"],
                        json.dumps(event["details"], sort_keys=True),
                    ),
                )
            updated = connection.execute(
                """UPDATE evaluator_executions
                   SET completed_at=?,status=?,evidence_json=?
                   WHERE request_id=? AND status='RUNNING'""",
                (
                    evidence["completed_at"],
                    final_status,
                    serialized,
                    request_id,
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                raise RuntimeError("evaluator execution state could not be completed")
            connection.commit()

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT evidence_json FROM evaluator_executions WHERE request_id=?",
                (request_id,),
            ).fetchone()
            audit_rows = connection.execute(
                """SELECT event_id,occurred_at,event_type,detail_json
                   FROM evaluator_audit_events WHERE request_id=? ORDER BY ordinal""",
                (request_id,),
            ).fetchall()
        if row is None or row["evidence_json"] is None:
            return None
        evidence = json.loads(row["evidence_json"])
        evidence["audit_events"] = [
            {
                "event_id": audit["event_id"],
                "request_id": request_id,
                "timestamp": audit["occurred_at"],
                "event_type": audit["event_type"],
                "details": json.loads(audit["detail_json"]),
            }
            for audit in audit_rows
        ]
        return evidence

    def list_recent(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT request_id,scenario,started_at,completed_at,status,evidence_json
                   FROM evaluator_executions ORDER BY started_at DESC LIMIT 20"""
            ).fetchall()
        results = []
        for row in rows:
            evidence = json.loads(row["evidence_json"]) if row["evidence_json"] else {}
            results.append(
                {
                    "request_id": row["request_id"],
                    "scenario": row["scenario"],
                    "scenario_name": SCENARIOS[row["scenario"]]["label"],
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                    "status": row["status"],
                    "decision": evidence.get("policy_decision", {}).get("decision"),
                }
            )
        return results


def _scenario_schedule(
    scenario: ScenarioName,
) -> tuple[tuple[str, dict[str, str], list[str]], ...]:
    if scenario == "normal_lookup":
        return (
            ("search_customer", {"query_kind": "customer_lookup"}, ["orders:read"]),
            ("get_order", {"customer_ref_hash": "cust_demo_001"}, ["orders:read"]),
        )
    if scenario == "repeated_calls":
        return (
            ("search_customer", {"query_kind": "customer_lookup"}, ["orders:read"]),
            *(
                ("get_order", {"order_ref_hash": "order_demo_001"}, ["orders:read"])
                for _ in range(4)
            ),
        )
    return (
        ("search_customer", {"query_kind": "customer_lookup"}, ["orders:read"]),
        ("get_order", {"customer_ref_hash": "cust_demo_001"}, ["orders:read"]),
        ("update_order", {"update_kind": "delivery_instruction"}, ["orders:read"]),
    )


def _permission_match(tool_name: str, permissions: list[str]) -> bool:
    frame = pd.DataFrame([{"tool_name": tool_name, "permissions": json.dumps(permissions)}])
    return bool(
        _add_permission_match(frame, TOOL_REQUIRED_PERMISSIONS).iloc[0]["permission_match"]
    )


def _build_event(
    request_id: str,
    index: int,
    tool_name: str,
    arguments: dict[str, str],
    permissions: list[str],
    occurred_at: datetime,
    *,
    outcome: dict[str, Any] | None,
    status_value: str,
) -> dict[str, Any]:
    result_size = len(json.dumps(outcome, sort_keys=True).encode("utf-8")) if outcome else 0
    return {
        "event_id": f"event_{request_id[8:]}_{index:02d}",
        "trace_id": request_id,
        "agent_id": "public_evaluator_agent",
        "task_id": request_id,
        "task_type": "order_lookup",
        "timestamp": occurred_at.isoformat(),
        "event_type": "tool_call",
        "tool_name": tool_name,
        "tool_input_metadata": arguments,
        "tool_output_metadata": {
            "record_count": 1 if outcome else 0,
            "result_size_bytes": result_size,
        },
        "latency_ms": 1,
        "status": status_value,
        "retry_count": 0,
        "token_usage": 0,
        "estimated_cost_usd": 0.0,
        "permissions": permissions,
        "_scenario_output": outcome,
    }


def _trace_frame(trace: TraceRecord) -> pd.DataFrame:
    rows = _flatten_trace(trace.to_phase2_record(), "synthetic")
    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = _add_trajectory_features(frame)
    frame["task_retry_zscore"] = 0.0
    frame["task_token_zscore"] = 0.0
    frame["task_cost_zscore"] = 0.0
    return frame


def _audit_event(
    request_id: str,
    ordinal: int,
    event_type: str,
    details: dict[str, Any],
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "event_id": f"audit_{request_id[8:]}_{ordinal:02d}",
        "request_id": request_id,
        "timestamp": (occurred_at or datetime.now(timezone.utc)).isoformat(),
        "event_type": event_type,
        "details": details,
    }


def _execute_scenario(
    request_id: str,
    scenario: ScenarioName,
    started_at: datetime,
    store: EvaluatorExecutionStore,
) -> dict[str, Any]:
    tool_calls = []
    raw_events = []
    permission_denied = False
    for index, (tool_name, arguments, permissions) in enumerate(_scenario_schedule(scenario)):
        allowed = _permission_match(tool_name, permissions)
        output = None
        call_status = "error"
        reason = "TOOL_PERMISSION_DENIED"
        if allowed:
            executor = TOOL_EXECUTORS.get(tool_name)
            if executor is not None:
                output = executor(arguments)
                call_status = "success"
                reason = "TOOL_EXECUTED"
            else:
                permission_denied = True
                reason = "TOOL_NOT_AVAILABLE"
        else:
            permission_denied = True
        occurred_at = datetime.now(timezone.utc)
        raw_event = _build_event(
            request_id,
            index,
            tool_name,
            arguments,
            permissions,
            occurred_at,
            outcome=output,
            status_value=call_status,
        )
        raw_events.append(raw_event)
        tool_calls.append(
            {
                "request_id": request_id,
                "event_id": raw_event["event_id"],
                "tool_name": tool_name,
                "arguments": arguments,
                "permissions": permissions,
                "required_permissions": TOOL_REQUIRED_PERMISSIONS[tool_name],
                "status": call_status,
                "outcome": output,
                "reason_code": reason,
                "timestamp": occurred_at.isoformat(),
            }
        )

    trace_payload = {
        "trace_id": request_id,
        "dataset_version": "public-evaluator-v1",
        "data_type": "synthetic",
        "limitations": (
            "Fixed mock-only evaluator scenario; tool outputs are synthetic and "
            "do not access customer, order, or payment systems."
        ),
        "label": "anomalous" if scenario == "repeated_calls" else "normal",
        "anomaly_type": "tool_loop" if scenario == "repeated_calls" else None,
        "agent_id": "public_evaluator_agent",
        "task_id": request_id,
        "task_type": "order_lookup",
        "events": [
            {key: value for key, value in event.items() if not key.startswith("_")}
            for event in raw_events
        ],
    }
    with TemporaryDirectory(prefix="agentsentinel-evaluator-") as directory:
        trace = TraceCollector(Path(directory) / "collected").capture(trace_payload)
    if trace is None:
        raise RuntimeError("evaluator trace failed validation")

    frame = _trace_frame(trace)
    detected_rows = apply_rules(frame)
    loop_detected = bool(detected_rows["rules_loop"].any())
    retry_detected = bool(detected_rows["rules_retry_storm"].any())
    resource_detected = bool(detected_rows["rules_resource_spike"].any())
    max_repeat = int(frame["tool_repeat_in_session"].max())
    detection_reasons = []
    if loop_detected:
        detection_reasons.append("TOOL_LOOP_DETECTED")
    if retry_detected:
        detection_reasons.append("RETRY_STORM_DETECTED")
    if resource_detected:
        detection_reasons.append("RESOURCE_SPIKE_DETECTED")
    detection = {
        "engine": "agentsentinel.detectors.rules.apply_rules",
        "anomalous": bool(detection_reasons),
        "reason_codes": detection_reasons,
        "explanation": (
            f"Maximum repeated calls to one tool: {max_repeat}; "
            f"loop threshold: {get_rules_metadata()['loop_threshold']}."
        ),
        "features": {
            "tool_repeat_in_session_max": max_repeat,
            "loop_threshold": get_rules_metadata()["loop_threshold"],
        },
    }
    permission_matches = [
        _permission_match(call["tool_name"], call["permissions"])
        for call in tool_calls
    ]
    if permission_denied or not all(permission_matches):
        decision, reason_code = "BLOCK", "TOOL_PERMISSION_DENIED"
    elif detection["anomalous"]:
        decision, reason_code = "REVIEW", detection_reasons[0]
    else:
        decision, reason_code = "ALLOW", "TOOL_PERMISSIONS_MATCHED"
    final_status = {
        "ALLOW": "COMPLETED",
        "REVIEW": "REVIEW_REQUIRED",
        "BLOCK": "BLOCKED",
    }[decision]
    missing_permissions = sorted(
        {
            permission
            for call in tool_calls
            for permission in set(call["required_permissions"])
            - set(call["permissions"])
        }
    )
    decision_explanations = {
        "ALLOW": "Every dispatched synthetic tool call had its required read permission.",
        "REVIEW": "AgentSentinel's existing loop detector flagged repeated tool calls; no more privileged operation is available.",
        "BLOCK": (
            "The simulated tool's required permission was absent"
            + (f" ({', '.join(missing_permissions)})" if missing_permissions else "")
            + "; the tool function was not invoked."
        ),
    }

    audit_events = [
        _audit_event(
            request_id,
            0,
            "REQUEST_STARTED",
            {"scenario": scenario},
            started_at,
        )
    ]
    for index, call in enumerate(tool_calls, start=1):
        audit_events.append(
            _audit_event(
                request_id,
                index,
                "TOOL_CALL_COMPLETED" if call["status"] == "success" else "TOOL_CALL_BLOCKED",
                {"event_id": call["event_id"], "tool_name": call["tool_name"],
                 "reason_code": call["reason_code"]},
                datetime.fromisoformat(call["timestamp"]),
            )
        )
    detection_at = datetime.now(timezone.utc)
    audit_events.append(
        _audit_event(
            request_id,
            len(audit_events),
            "DETECTION_EVALUATED",
            {"anomalous": detection["anomalous"], "reason_codes": detection_reasons},
            detection_at,
        )
    )
    policy_at = datetime.now(timezone.utc)
    audit_events.append(
        _audit_event(
            request_id,
            len(audit_events),
            "POLICY_DECISION",
            {"decision": decision, "reason_code": reason_code},
            policy_at,
        )
    )
    completed_at = datetime.now(timezone.utc)
    audit_events.append(
        _audit_event(
            request_id,
            len(audit_events),
            "EXECUTION_COMPLETED",
            {"status": final_status},
            completed_at,
        )
    )
    evidence = {
        "request_id": request_id,
        "scenario": scenario,
        "scenario_name": SCENARIOS[scenario]["label"],
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "status": final_status,
        "tool_calls": tool_calls,
        "trace": trace.to_phase2_record(),
        "detection": detection,
        "policy_decision": {
            "decision": decision,
            "reason_code": reason_code,
            "explanation": decision_explanations[decision],
            "missing_permissions": missing_permissions,
        },
        "audit_events": audit_events,
    }
    store.complete(request_id, final_status, evidence)
    return evidence


def create_evaluator_router(store: EvaluatorExecutionStore) -> APIRouter:
    router = APIRouter()

    @router.get("/evaluator", response_class=HTMLResponse)
    async def evaluator_page(request: Request) -> HTMLResponse:
        response = _TEMPLATES.TemplateResponse(
            request=request,
            name="evaluator.html",
            context={"scenarios": SCENARIOS},
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; base-uri 'none'; "
                "object-src 'none'; frame-ancestors 'none'",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
            },
        )
        return response

    @router.post("/evaluator/executions")
    async def run_scenario(request: Request) -> JSONResponse:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="application/json is required",
            )
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 1024:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="scenario request is too large",
                )
            chunks.append(chunk)
        try:
            payload = ScenarioRequest.model_validate(json.loads(b"".join(chunks)))
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="invalid predefined scenario request",
            ) from None
        request_id = "request_" + uuid.uuid4().hex
        started_at = datetime.now(timezone.utc)
        try:
            store.begin(request_id, payload.scenario, started_at)
        except RateLimitExceeded as error:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="scenario rate limit reached; wait before running another scenario",
                headers={"Retry-After": str(error.retry_after)},
            ) from None
        evidence = _execute_scenario(request_id, payload.scenario, started_at, store)
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=evidence)

    @router.get("/evaluator/executions")
    async def list_executions() -> list[dict[str, Any]]:
        return store.list_recent()

    @router.get("/evaluator/executions/{request_id}")
    async def get_execution(request_id: str) -> dict[str, Any]:
        if not request_id.startswith("request_") or len(request_id) != 40:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="execution not found")
        evidence = store.get(request_id)
        if evidence is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="execution not found")
        return evidence

    return router
