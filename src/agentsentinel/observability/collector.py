"""Local Phase 4 trace collector.

The collector observes and records validated traces. It does not make policy
decisions, open network connections, talk to a database, or implement runtime
fail-open/fail-closed behavior.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from pydantic import ValidationError

from agentsentinel.observability.payment_audit import PaymentAuditChain, PaymentAuditUnavailable
from agentsentinel.schema.redaction import redact_mapping
from agentsentinel.schema.payment_telemetry import PaymentTelemetryEvent
from agentsentinel.schema.trace import TraceRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rejection:
    reason: str
    trace_id: str | None
    rejected_at: str


@dataclass(frozen=True)
class PaymentTelemetryCapture:
    status: Literal["accepted", "duplicate", "rejected", "unavailable"]
    reason_code: str | None = None


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
        self.payment_diagnostics: list[PaymentTelemetryCapture] = []
        self._payment_chain: PaymentAuditChain | None = None
        self._payment_chain_lock = Lock()

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

    def capture_payment_event(self, raw_event: PaymentTelemetryEvent | dict[str, Any]) -> PaymentTelemetryCapture:
        """Persist a validated payment event and its hash-chain entry atomically.

        Failures are returned and retained as bounded diagnostics. Callers must
        not treat telemetry status as authorization evidence.
        """
        try:
            event = PaymentTelemetryEvent.model_validate(raw_event)
        except ValidationError as error:
            first_error = error.errors()[0]
            location = ".".join(str(part) for part in first_error["loc"])
            result = PaymentTelemetryCapture("rejected", f"INVALID_PAYMENT_EVENT:{location}")
            self.payment_diagnostics.append(result)
            self._write_payment_diagnostic(result)
            logger.error("payment telemetry rejected (%s)", result.reason_code)
            return result
        try:
            if self._payment_chain is None:
                with self._payment_chain_lock:
                    if self._payment_chain is None:
                        self._payment_chain = PaymentAuditChain(self.sink_root / "payment_telemetry.sqlite")
            stored = self._payment_chain.append(event)
            result = PaymentTelemetryCapture("duplicate" if stored == "duplicate" else "accepted")
            self.payment_diagnostics.append(result)
            return result
        except (PaymentAuditUnavailable, OSError):
            result = PaymentTelemetryCapture("unavailable", "PAYMENT_TELEMETRY_STORE_UNAVAILABLE")
            self.payment_diagnostics.append(result)
            self._write_payment_diagnostic(result, event_id=event.event_id)
            logger.error("payment telemetry store unavailable for event_id=%s", event.event_id)
            return result

    def _write_payment_diagnostic(
        self, result: PaymentTelemetryCapture, *, event_id: str | None = None
    ) -> None:
        payload = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "status": result.status,
            "reason_code": result.reason_code,
            "event_id": event_id,
        }
        try:
            path = self.sink_root / "rejected" / "payment_telemetry.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
        except OSError:
            logger.error("payment telemetry diagnostic could not be persisted")

    def _sanitize(self, raw_event: dict[str, Any]) -> dict[str, Any]:
        copied = copy.deepcopy(raw_event)
        if isinstance(copied, Mapping):
            redacted, _ = redact_mapping(copied)
            return redacted
        # A non-mapping value has no trustworthy field names to redact.
        return {"malformed_payload_type": type(raw_event).__name__}

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