"""SQLite-backed payment telemetry and externally anchored audit chain."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from agentsentinel.schema.payment_telemetry import PaymentTelemetryEvent

ZERO_HASH = "0" * 64
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class PaymentAuditUnavailable(RuntimeError):
    """Safe audit storage error without database values or paths."""


@dataclass(frozen=True)
class PaymentAuditVerification:
    valid: bool
    records_checked: int
    first_broken_sequence: int | None = None
    reason_code: str | None = None


class PaymentAuditChain:
    """Tamper-evident audit chain with a separately stored local checkpoint.

    The checkpoint is a sidecar file, not part of the SQLite database. It detects
    database-only edits, truncation, and rollback after the anchor is established.
    It is not protection against an attacker able to modify both files.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        read_only: bool = False,
        anchor_path: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.anchor_path = Path(anchor_path) if anchor_path is not None else Path(
            str(self.path) + ".anchor"
        )
        self.lock_path = Path(str(self.anchor_path) + ".lock")
        self.read_only = read_only
        if read_only:
            if not self.path.is_file() or not self.anchor_path.is_file():
                raise PaymentAuditUnavailable("payment audit database or checkpoint unavailable")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.anchor_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    @classmethod
    def open_readonly(
        cls, path: str | Path, *, anchor_path: str | Path | None = None
    ) -> PaymentAuditChain:
        """Open an existing chain and checkpoint without initializing either."""
        return cls(path, read_only=True, anchor_path=anchor_path)

    @classmethod
    def initialize_legacy_checkpoint(
        cls, path: str | Path, *, anchor_path: str | Path | None = None
    ) -> None:
        """Explicitly anchor a pre-checkpoint chain after an operator verifies its provenance.

        Normal initialization never anchors a non-empty database without an existing
        checkpoint; this migration is intentionally opt-in because it cannot detect
        tampering that predates the checkpoint.
        """
        chain = cls.__new__(cls)
        chain.path = Path(path)
        chain.anchor_path = Path(anchor_path) if anchor_path is not None else Path(
            str(chain.path) + ".anchor"
        )
        chain.lock_path = Path(str(chain.anchor_path) + ".lock")
        chain.read_only = False
        if not chain.path.is_file():
            raise PaymentAuditUnavailable("legacy payment audit database unavailable")
        try:
            with chain._anchor_lock():
                with closing(chain._connect()) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        if chain.anchor_path.exists():
                            chain._recover_checkpoint(conn)
                        else:
                            head, verification = chain._database_head(conn)
                            if not verification.valid or head[0] == 0:
                                raise PaymentAuditUnavailable(
                                    "legacy payment audit chain cannot be anchored"
                                )
                            chain._write_anchor(chain._state(head))
                        conn.execute("COMMIT")
                    except BaseException:
                        conn.rollback()
                        raise
        except PaymentAuditUnavailable:
            raise
        except (sqlite3.Error, OSError):
            raise PaymentAuditUnavailable("legacy payment audit checkpoint unavailable") from None

    @contextmanager
    def _anchor_lock(self) -> Iterator[None]:
        """Serialize checkpoint and database operations across local processes."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.lock_path.open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except PaymentAuditUnavailable:
            raise
        except OSError:
            raise PaymentAuditUnavailable("payment audit checkpoint lock unavailable") from None

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            conn = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=10.0,
                isolation_level=None,
            )
        else:
            conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        if not self.read_only:
            conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        try:
            had_audit_table = False
            if self.path.is_file():
                with closing(self._connect()) as existing_conn:
                    had_audit_table = existing_conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='payment_audit_chain'"
                    ).fetchone() is not None
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
            with self._anchor_lock():
                with closing(self._connect()) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        if not self.anchor_path.exists():
                            head, verification = self._database_head(conn)
                            if not verification.valid:
                                raise PaymentAuditUnavailable(
                                    "legacy payment audit chain is invalid"
                                )
                            if head[0] != 0 or had_audit_table:
                                raise PaymentAuditUnavailable(
                                    "payment audit checkpoint missing; explicit legacy migration required"
                                )
                            self._write_anchor(self._state(head))
                        else:
                            self._recover_checkpoint(conn)
                        conn.execute("COMMIT")
                    except BaseException:
                        conn.rollback()
                        raise
        except PaymentAuditUnavailable:
            raise
        except (sqlite3.Error, OSError):
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

    @staticmethod
    def _state(
        head: tuple[int, str], pending: tuple[int, str] | None = None
    ) -> dict[str, object]:
        return {
            "version": 1,
            "sequence": head[0],
            "record_hash": head[1],
            "pending": (
                {"sequence": pending[0], "record_hash": pending[1]}
                if pending is not None
                else None
            ),
        }

    def _read_anchor(self) -> dict[str, object]:
        try:
            state = json.loads(self.anchor_path.read_text(encoding="utf-8"))
            if (
                not isinstance(state, dict)
                or set(state) != {"version", "sequence", "record_hash", "pending"}
                or type(state["version"]) is not int
                or state["version"] != 1
                or type(state["sequence"]) is not int
                or state["sequence"] < 0
                or not isinstance(state["record_hash"], str)
                or not _HASH.fullmatch(state["record_hash"])
            ):
                raise ValueError
            pending = state["pending"]
            if pending is not None and (
                not isinstance(pending, dict)
                or set(pending) != {"sequence", "record_hash"}
                or type(pending["sequence"]) is not int
                or pending["sequence"] != state["sequence"] + 1
                or not isinstance(pending["record_hash"], str)
                or not _HASH.fullmatch(pending["record_hash"])
            ):
                raise ValueError
            return state
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise PaymentAuditUnavailable("payment audit checkpoint invalid or unavailable") from None

    def _write_anchor(self, state: dict[str, object]) -> None:
        """Atomically replace the external checkpoint after syncing its contents."""
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=self.anchor_path.name + ".", suffix=".tmp", dir=self.anchor_path.parent
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(self.canonical_json(state))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.anchor_path)
            temporary = None
            if os.name != "nt":
                directory_fd = os.open(self.anchor_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError:
            raise PaymentAuditUnavailable("payment audit checkpoint update unavailable") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _verify_rows(self, rows: list[sqlite3.Row]) -> PaymentAuditVerification:
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

    def _database_head(
        self, conn: sqlite3.Connection
    ) -> tuple[tuple[int, str], PaymentAuditVerification]:
        rows = conn.execute(
            "SELECT sequence_number,previous_record_hash,canonical_event,event_timestamp,"
            "event_type,actor_role,record_id,current_record_hash "
            "FROM payment_audit_chain ORDER BY sequence_number"
        ).fetchall()
        verification = self._verify_rows(rows)
        head = (
            (0, ZERO_HASH)
            if not rows
            else (int(rows[-1]["sequence_number"]), str(rows[-1]["current_record_hash"]))
        )
        return head, verification

    def _recover_checkpoint(self, conn: sqlite3.Connection) -> None:
        state = self._read_anchor()
        head, verification = self._database_head(conn)
        if not verification.valid:
            raise PaymentAuditUnavailable("payment audit chain invalid")
        current = (int(state["sequence"]), str(state["record_hash"]))
        pending_raw = state["pending"]
        pending = (
            (int(pending_raw["sequence"]), str(pending_raw["record_hash"]))
            if isinstance(pending_raw, dict)
            else None
        )
        if pending is None:
            if head != current:
                raise PaymentAuditUnavailable("payment audit checkpoint mismatch")
            return
        if head == current:
            self._write_anchor(self._state(current))
        elif head == pending:
            self._write_anchor(self._state(pending))
        else:
            raise PaymentAuditUnavailable("payment audit interrupted checkpoint mismatch")

    def append(self, event: PaymentTelemetryEvent) -> Literal["appended", "duplicate"]:
        if self.read_only:
            raise PaymentAuditUnavailable("payment audit is read only")
        event = PaymentTelemetryEvent.model_validate(event.model_dump(mode="python"))
        canonical_event = self.canonical_json(event.model_dump(mode="json"))
        try:
            with self._anchor_lock():
                with closing(self._connect()) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        self._recover_checkpoint(conn)
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
                        head, verification = self._database_head(conn)
                        if not verification.valid:
                            raise PaymentAuditUnavailable("payment audit chain invalid")
                        anchor = self._read_anchor()
                        if head != (int(anchor["sequence"]), str(anchor["record_hash"])):
                            raise PaymentAuditUnavailable("payment audit checkpoint mismatch")
                        last = conn.execute(
                            "SELECT current_record_hash FROM payment_audit_chain "
                            "ORDER BY sequence_number DESC LIMIT 1"
                        ).fetchone()
                        sequence_number = head[0] + 1
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
                        pending = (sequence_number, current_hash)
                        self._write_anchor(self._state(head, pending))
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
                        self._write_anchor(self._state(pending))
                        return "appended"
                    except BaseException:
                        if conn.in_transaction:
                            conn.rollback()
                        raise
        except PaymentAuditUnavailable:
            raise
        except Exception:
            raise PaymentAuditUnavailable("payment audit append unavailable") from None

    @contextmanager
    def verified_snapshot(
        self,
    ) -> Iterator[tuple[sqlite3.Connection, PaymentAuditVerification]]:
        """Hold one verified SQLite read snapshot for the full caller read."""
        if not self.anchor_path.is_file():
            raise PaymentAuditUnavailable("payment audit checkpoint unavailable")
        try:
            with self._anchor_lock():
                with closing(self._connect()) as conn:
                    conn.execute("BEGIN")
                    try:
                        state = self._read_anchor()
                        if state["pending"] is not None:
                            raise PaymentAuditUnavailable("payment audit checkpoint recovery required")
                        head, verification = self._database_head(conn)
                        if verification.valid:
                            expected = (int(state["sequence"]), str(state["record_hash"]))
                            if head != expected:
                                verification = PaymentAuditVerification(
                                    False,
                                    verification.records_checked,
                                    head[0] + 1,
                                    "CHECKPOINT_MISMATCH",
                                )
                        yield conn, verification
                    finally:
                        if conn.in_transaction:
                            conn.rollback()
        except PaymentAuditUnavailable:
            raise
        except (sqlite3.Error, OSError):
            raise PaymentAuditUnavailable("payment audit verification unavailable") from None

    def verify(self) -> PaymentAuditVerification:
        with self.verified_snapshot() as (_, verification):
            return verification

    def record_count(self) -> int:
        try:
            with closing(self._connect()) as conn:
                return int(conn.execute("SELECT COUNT(*) FROM payment_audit_chain").fetchone()[0])
        except sqlite3.Error:
            raise PaymentAuditUnavailable("payment audit count unavailable") from None
