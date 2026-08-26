"""Canonical Pydantic trace schema for AgentSentinel.

The schema accepts Phase 2 synthetic trace records without dropping fields, but
stores audit metadata separately from tool-call content for later phases.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, NonNegativeInt, model_validator

from agentsentinel.schema.redaction import redact_mapping

JsonValue = str | int | float | bool | None | dict[str, Any] | list[Any]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ToolArguments(StrictModel):
    values: dict[str, JsonValue]
    redacted: bool = False

    @model_validator(mode="before")
    @classmethod
    def accept_plain_mapping(cls, data: Any) -> Any:
        if isinstance(data, dict) and "values" not in data:
            return {"values": data, "redacted": False}
        return data

    @model_validator(mode="after")
    def redact_sensitive_values(self) -> "ToolArguments":
        clean, was_redacted = redact_mapping(self.values)
        self.values = clean
        self.redacted = self.redacted or was_redacted
        return self


class ToolOutputMetadata(StrictModel):
    record_count: NonNegativeInt
    result_size_bytes: NonNegativeInt


class ResourceUsage(StrictModel):
    latency_ms: NonNegativeInt
    token_usage: NonNegativeInt
    estimated_cost_usd: NonNegativeFloat


class ToolCallAuditMetadata(StrictModel):
    event_id: str
    trace_id: str
    agent_id: str
    task_id: str
    task_type: str
    timestamp: datetime
    event_type: Literal["tool_call"]
    status: Literal["success", "error"]
    retry_count: NonNegativeInt
    permissions: list[str]


class ToolCallContent(StrictModel):
    tool_name: Literal["search_customer", "get_order", "update_order"]
    arguments: ToolArguments
    output_metadata: ToolOutputMetadata
    resource_usage: ResourceUsage


class ToolCallEvent(StrictModel):
    audit: ToolCallAuditMetadata
    content: ToolCallContent

    @model_validator(mode="before")
    @classmethod
    def accept_phase2_event(cls, data: Any) -> Any:
        if not isinstance(data, dict) or {"audit", "content"}.issubset(data):
            return data

        return {
            "audit": {
                "event_id": data["event_id"],
                "trace_id": data["trace_id"],
                "agent_id": data["agent_id"],
                "task_id": data["task_id"],
                "task_type": data["task_type"],
                "timestamp": data["timestamp"],
                "event_type": data["event_type"],
                "status": data["status"],
                "retry_count": data["retry_count"],
                "permissions": data["permissions"],
            },
            "content": {
                "tool_name": data["tool_name"],
                "arguments": data["tool_input_metadata"],
                "output_metadata": data["tool_output_metadata"],
                "resource_usage": {
                    "latency_ms": data["latency_ms"],
                    "token_usage": data["token_usage"],
                    "estimated_cost_usd": data["estimated_cost_usd"],
                },
            },
        }

    def to_phase2_event(self) -> dict[str, Any]:
        return {
            "event_id": self.audit.event_id,
            "trace_id": self.audit.trace_id,
            "agent_id": self.audit.agent_id,
            "task_id": self.audit.task_id,
            "timestamp": self.audit.timestamp.isoformat(),
            "event_type": self.audit.event_type,
            "tool_name": self.content.tool_name,
            "tool_input_metadata": self.content.arguments.values,
            "tool_output_metadata": self.content.output_metadata.model_dump(),
            "latency_ms": self.content.resource_usage.latency_ms,
            "status": self.audit.status,
            "retry_count": self.audit.retry_count,
            "token_usage": self.content.resource_usage.token_usage,
            "estimated_cost_usd": self.content.resource_usage.estimated_cost_usd,
            "permissions": self.audit.permissions,
            "task_type": self.audit.task_type,
        }


class TraceAuditMetadata(StrictModel):
    trace_id: str
    agent_id: str
    task_id: str
    task_type: str
    dataset_version: str | None = None
    data_type: Literal["synthetic", "real"]
    limitations: str | None = None
    label: Literal["normal", "anomalous"] | None = None
    anomaly_type: Literal["tool_loop", "retry_storm", "resource_spike"] | None = None
    policy_version: str | None = None

    @model_validator(mode="after")
    def require_synthetic_limitations(self) -> "TraceAuditMetadata":
        if self.data_type == "synthetic" and not self.limitations:
            raise ValueError("synthetic traces must include limitations")
        return self


class TraceRecord(StrictModel):
    audit: TraceAuditMetadata
    events: list[ToolCallEvent] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def accept_phase2_trace(cls, data: Any) -> Any:
        if not isinstance(data, dict) or {"audit", "events"}.issubset(data):
            return data

        return {
            "audit": {
                "trace_id": data["trace_id"],
                "agent_id": data["agent_id"],
                "task_id": data["task_id"],
                "task_type": data["task_type"],
                "dataset_version": data.get("dataset_version"),
                "data_type": data["data_type"],
                "limitations": data.get("limitations"),
                "label": data.get("label"),
                "anomaly_type": data.get("anomaly_type"),
                "policy_version": data.get("policy_version"),
            },
            "events": data["events"],
        }

    @model_validator(mode="after")
    def require_consistent_event_identity(self) -> "TraceRecord":
        for event in self.events:
            if event.audit.trace_id != self.audit.trace_id:
                raise ValueError("event trace_id must match parent trace_id")
            if event.audit.agent_id != self.audit.agent_id:
                raise ValueError("event agent_id must match parent agent_id")
            if event.audit.task_id != self.audit.task_id:
                raise ValueError("event task_id must match parent task_id")
            if event.audit.task_type != self.audit.task_type:
                raise ValueError("event task_type must match parent task_type")
        return self

    def to_phase2_record(self) -> dict[str, Any]:
        return {
            "trace_id": self.audit.trace_id,
            "dataset_version": self.audit.dataset_version,
            "data_type": self.audit.data_type,
            "limitations": self.audit.limitations,
            "label": self.audit.label,
            "anomaly_type": self.audit.anomaly_type,
            "agent_id": self.audit.agent_id,
            "task_id": self.audit.task_id,
            "task_type": self.audit.task_type,
            "events": [event.to_phase2_event() for event in self.events],
        }
