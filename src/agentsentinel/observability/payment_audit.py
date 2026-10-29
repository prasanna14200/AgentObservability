"""SQLite-backed payment telemetry and tamper-evident audit chain."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentsentinel.schema.payment_telemetry import PaymentTelemetryEvent

ZERO_HASH = "0" * 64


class PaymentAuditUnavailable(RuntimeError):
    """Safe audit storage error without database values or paths."""


@dataclass(frozen=True)
class PaymentAuditVerification:
    valid: bool
    records_checked: int
    first_broken_sequence: int | None = None
    reason_code: str | None = None


class PaymentAuditChain:
    """Append-only-by-convention hash chain for newly captured payment events.

    The chain does not retrofit or modify legacy operation/approval events.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        try:
            with closing(self._connect()) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS payment_events (
                        event_id TEXT PRIMARY KEY,
                        canonical_event TEXT NOT NULL,
                        inserted_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS payment_audit_chain (
                        sequence_number INTEGER PRIMARY KEY,
                        previous_record_hash TEXT NOT NULL,
                        canonical_event TEXT NOT NULL,
                        event_timestamp TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        actor_role TEXT NOT NULL,
                        record_id TEXT NOT NULL,
                        current_record_hash TEXT NOT NULL UNIQUE
                    );
                    """
                )
        except sqlite3.Error:
            raise PaymentAuditUnavailable("payment audit initialization unavailable") from None

    @staticmethod
    def canonical_json(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @classmethod
    def _record_hash(
        cls,
        *,
        sequence_number: int,
        previous_record_hash: str,
        canonical_event: str,
        event_timestamp: str,
        event_type: str,
        actor_role: str,
        record_id: str,
    ) -> str:
        material = cls.canonical_json(
            {
                "sequence_number": sequence_number,
                "previous_record_hash": previous_record_hash,
                "canonical_event": json.loads(canonical_event),
                "event_timestamp": event_timestamp,
                "event_type": event_type,
                "actor_role": actor_role,
                "record_id": record_id,
            }
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def append(self, event: PaymentTelemetryEvent) -> Literal["appended", "duplicate"]:
        event = PaymentTelemetryEvent.model_validate(event.model_dump(mode="python"))
        canonical_event = self.canonical_json(event.model_dump(mode="json"))
        try:
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT canonical_event FROM payment_events WHERE event_id=?",
                    (event.event_id,),
                ).fetchone()
                if existing is not None:
                    old_event = json.loads(existing["canonical_event"])
                    new_event = json.loads(canonical_event)
                    for volatile_field in ("timestamp", "duration_ms"):
                        old_event.pop(volatile_field, None)
                        new_event.pop(volatile_field, None)
                    if old_event != new_event:
                        raise PaymentAuditUnavailable("payment event identifier conflict")
                    conn.execute("COMMIT")
                    return "duplicate"
                last = conn.execute(
                    "SELECT sequence_number,current_record_hash FROM payment_audit_chain "
                    "ORDER BY sequence_number DESC LIMIT 1"
                ).fetchone()
                sequence_number = 1 if last is None else int(last["sequence_number"]) + 1
                previous_hash = ZERO_HASH if last is None else str(last["current_record_hash"])
                event_timestamp = event.timestamp.isoformat()
                actor_role = event.actor_role or "application"
                record_id = event.approval_id or event.operation_id or event.event_id
                current_hash = self._record_hash(
                    sequence_number=sequence_number,
                    previous_record_hash=previous_hash,
                    canonical_event=canonical_event,
                    event_timestamp=event_timestamp,
                    event_type=event.event_type,
                    actor_role=actor_role,
                    record_id=record_id,
                )
                conn.execute(
                    "INSERT INTO payment_events(event_id,canonical_event,inserted_at) VALUES(?,?,?)",
                    (event.event_id, canonical_event, event_timestamp),
                )
                conn.execute(
                    "INSERT INTO payment_audit_chain(sequence_number,previous_record_hash,"
                    "canonical_event,event_timestamp,event_type,actor_role,record_id,current_record_hash) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        sequence_number,
                        previous_hash,
                        canonical_event,
                        event_timestamp,
                        event.event_type,
                        actor_role,
                        record_id,
                        current_hash,
                    ),
                )
                conn.execute("COMMIT")
                return "appended"
        except PaymentAuditUnavailable:
            raise
        except Exception as exc:
            raise PaymentAuditUnavailable("payment audit append unavailable") from exc

    def verify(self) -> PaymentAuditVerification:
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT sequence_number,previous_record_hash,canonical_event,event_timestamp,"
                    "event_type,actor_role,record_id,current_record_hash "
                    "FROM payment_audit_chain ORDER BY sequence_number"
                ).fetchall()
        except sqlite3.Error:
            raise PaymentAuditUnavailable("payment audit verification unavailable") from None

        previous_hash = ZERO_HASH
        for expected_sequence, row in enumerate(rows, start=1):
            sequence = int(row["sequence_number"])
            if sequence != expected_sequence:
                return PaymentAuditVerification(False, expected_sequence - 1, expected_sequence, "SEQUENCE_GAP")
            try:
                parsed_event = json.loads(row["canonical_event"])
                if self.canonical_json(parsed_event) != row["canonical_event"]:
                    return PaymentAuditVerification(False, expected_sequence - 1, sequence, "NONCANONICAL_EVENT")
                PaymentTelemetryEvent.model_validate(parsed_event)
            except (json.JSONDecodeError, ValueError, TypeError):
                return PaymentAuditVerification(False, expected_sequence - 1, sequence, "INVALID_EVENT")
            if row["previous_record_hash"] != previous_hash:
                return PaymentAuditVerification(False, expected_sequence - 1, sequence, "PREVIOUS_HASH_MISMATCH")
            calculated = self._record_hash(
                sequence_number=sequence,
                previous_record_hash=row["previous_record_hash"],
                canonical_event=row["canonical_event"],
                event_timestamp=row["event_timestamp"],
                event_type=row["event_type"],
                actor_role=row["actor_role"],
                record_id=row["record_id"],
            )
            if calculated != row["current_record_hash"]:
                return PaymentAuditVerification(False, expected_sequence - 1, sequence, "RECORD_HASH_MISMATCH")
            previous_hash = calculated
        return PaymentAuditVerification(True, len(rows))

    def record_count(self) -> int:
        try:
            with closing(self._connect()) as conn:
                return int(conn.execute("SELECT COUNT(*) FROM payment_audit_chain").fetchone()[0])
        except sqlite3.Error:
            raise PaymentAuditUnavailable("payment audit count unavailable") from None
