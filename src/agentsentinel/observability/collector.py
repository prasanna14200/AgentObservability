"""Local Phase 4 trace collector.

The collector observes and records validated traces. It does not make policy
decisions, open network connections, talk to a database, or implement runtime
fail-open/fail-closed behavior.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agentsentinel.schema.redaction import redact_mapping
from agentsentinel.schema.trace import TraceRecord


@dataclass(frozen=True)
class Rejection:
    reason: str
    trace_id: str | None
    rejected_at: str


class TraceCollector:
    """Validate, redact, and persist local trace records.

    Malformed input behavior is explicit: invalid payloads are rejected, the
    sanitized payload and validation reason are written to a rejection JSONL
    file, and `capture` returns `None` instead of crashing the caller.
    """

    def __init__(self, sink_root: Path | str = Path("data/collected")) -> None:
        self.sink_root = Path(sink_root)
        self.rejection_sink = self.sink_root / "rejected" / "rejections.jsonl"
        self.rejections: list[Rejection] = []

    def capture(self, raw_event: dict[str, Any]) -> TraceRecord | None:
        """Capture one raw trace payload.

        Returns a validated `TraceRecord` for accepted input. Returns `None`
        for malformed input after logging a rejection reason and sanitized copy
        to the local rejection sink.
        """

        sanitized = self._sanitize(raw_event)

        try:
            trace = TraceRecord.model_validate(sanitized)
        except ValidationError as error:
            self._reject(sanitized, self._validation_reason(error))
            return None
        except (KeyError, TypeError, ValueError) as error:
            self._reject(sanitized, str(error))
            return None

        self._write_trace(trace)
        return trace

    def _sanitize(self, raw_event: dict[str, Any]) -> dict[str, Any]:
        copied = copy.deepcopy(raw_event)
        if isinstance(copied, Mapping):
            redacted, _ = redact_mapping(copied)
            return redacted
        return {"malformed_payload": repr(raw_event)}

    def _write_trace(self, trace: TraceRecord) -> None:
        label = trace.audit.label or "unlabeled"
        sink = self.sink_root / label / "traces.jsonl"
        sink.parent.mkdir(parents=True, exist_ok=True)
        with sink.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(trace.to_phase2_record(), sort_keys=True) + "\n")

    def _reject(self, sanitized_event: dict[str, Any], reason: str) -> None:
        trace_id = sanitized_event.get("trace_id") if isinstance(sanitized_event, dict) else None
        rejected_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self.rejections.append(
            Rejection(reason=reason, trace_id=trace_id, rejected_at=rejected_at)
        )
        payload = {
            "rejected_at": rejected_at,
            "reason": reason,
            "trace_id": trace_id,
            "sanitized_event": sanitized_event,
        }
        self.rejection_sink.parent.mkdir(parents=True, exist_ok=True)
        with self.rejection_sink.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    @staticmethod
    def _validation_reason(error: ValidationError) -> str:
        first_error = error.errors()[0]
        location = ".".join(str(part) for part in first_error["loc"])
        return f"{location}: {first_error['msg']}"
