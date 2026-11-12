"""Post-facto evidence for manually executed Sandbox spike operations."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,79}$")
_MERCHANT_ID = re.compile(r"^[A-Z0-9]{8,32}$")
_ORDER_STATUSES = frozenset(
    {"CREATED", "SAVED", "APPROVED", "VOIDED", "COMPLETED", "PAYER_ACTION_REQUIRED"}
)
_CAPTURE_STATUSES = frozenset(
    {"COMPLETED", "PENDING", "DECLINED", "PARTIALLY_REFUNDED", "REFUNDED"}
)
_EVIDENCE_STATUSES = frozenset(
    {"PENDING", "COMPLETED", "FAILED", "RECONCILIATION_ERROR"}
)
_ERROR_CODES = frozenset(
    {"PROVIDER_UNAVAILABLE", "PROVIDER_RESPONSE_INVALID", "BINDING_MISMATCH"}
)


class EvidenceStoreError(RuntimeError):
    """Sanitized evidence persistence failure."""


class EvidenceConflict(EvidenceStoreError):
    """The operation identifier is already bound to different Sandbox data."""


def _validate_amount(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid Sandbox evidence amount")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid Sandbox evidence amount") from None
    if not amount.is_finite() or amount < Decimal("0.01") or amount > Decimal("10.00"):
        raise ValueError("invalid Sandbox evidence amount")
    if amount.as_tuple().exponent < -2:
        raise ValueError("invalid Sandbox evidence amount")
    return f"{amount:.2f}"


def classify_sandbox_status(order_status: str, capture_status: str | None) -> str:
    if order_status not in _ORDER_STATUSES:
        raise ValueError("invalid Sandbox order status")
    if capture_status is not None and capture_status not in _CAPTURE_STATUSES:
        raise ValueError("invalid Sandbox capture status")
    if order_status == "VOIDED" or capture_status == "DECLINED":
        return "FAILED"
    if order_status == "COMPLETED" and capture_status == "COMPLETED":
        return "COMPLETED"
    return "PENDING"


class SandboxReconciliationEvidenceStore:
    """Stores manual spike evidence without manufacturing gateway operations."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if str(self.path) == ":memory:":
            raise ValueError("Sandbox evidence requires the application SQLite file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(str(self.path), timeout=1.0)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sandbox_reconciliation_evidence (
                        operation_id TEXT PRIMARY KEY,
                        source TEXT NOT NULL CHECK(source='manual_sandbox_spike'),
                        provider_mode TEXT NOT NULL CHECK(provider_mode='SANDBOX'),
                        merchant_id TEXT NOT NULL,
                        amount_text TEXT NOT NULL,
                        currency TEXT NOT NULL CHECK(currency='USD'),
                        provider_order_id TEXT NOT NULL,
                        order_status TEXT,
                        provider_capture_id TEXT,
                        capture_status TEXT,
                        reconciliation_status TEXT NOT NULL CHECK(
                            reconciliation_status IN
                            ('PENDING','COMPLETED','FAILED','RECONCILIATION_ERROR')
                        ),
                        error_code TEXT,
                        attempted_at TEXT NOT NULL,
                        verified_at TEXT
                    )
                    """
                )
        except sqlite3.Error:
            raise EvidenceStoreError("Sandbox evidence storage unavailable") from None

    @staticmethod
    def _validate_identity(
        operation_id: str, merchant_id: str, amount_text: str, order_id: str
    ) -> tuple[str, str, str, str]:
        if not isinstance(operation_id, str) or not _IDENTIFIER.fullmatch(operation_id):
            raise ValueError("invalid Sandbox evidence operation")
        if not isinstance(merchant_id, str) or not _MERCHANT_ID.fullmatch(merchant_id):
            raise ValueError("invalid Sandbox evidence merchant")
        if not isinstance(order_id, str) or not _IDENTIFIER.fullmatch(order_id):
            raise ValueError("invalid Sandbox evidence order")
        return operation_id, merchant_id, _validate_amount(amount_text), order_id

    def _save(
        self,
        *,
        operation_id: str,
        merchant_id: str,
        amount_text: str,
        provider_order_id: str,
        order_status: str | None,
        provider_capture_id: str | None,
        capture_status: str | None,
        reconciliation_status: str,
        error_code: str | None,
        attempted_at: datetime,
        verified_at: datetime | None,
    ) -> None:
        identity = self._validate_identity(
            operation_id, merchant_id, amount_text, provider_order_id
        )
        operation_id, merchant_id, amount_text, provider_order_id = identity
        if reconciliation_status not in _EVIDENCE_STATUSES:
            raise ValueError("invalid Sandbox evidence status")
        if order_status is not None and order_status not in _ORDER_STATUSES:
            raise ValueError("invalid Sandbox order status")
        if capture_status is not None and capture_status not in _CAPTURE_STATUSES:
            raise ValueError("invalid Sandbox capture status")
        if provider_capture_id is not None and (
            not isinstance(provider_capture_id, str)
            or not _IDENTIFIER.fullmatch(provider_capture_id)
        ):
            raise ValueError("invalid Sandbox evidence capture")
        if error_code is not None and error_code not in _ERROR_CODES:
            raise ValueError("invalid Sandbox evidence error code")
        if not isinstance(attempted_at, datetime) or attempted_at.tzinfo is None:
            raise ValueError("Sandbox evidence timestamps must be timezone aware")
        if verified_at is not None and (
            not isinstance(verified_at, datetime) or verified_at.tzinfo is None
        ):
            raise ValueError("Sandbox evidence timestamps must be timezone aware")

        try:
            connection = sqlite3.connect(
                str(self.path), timeout=1.0, isolation_level=None
            )
        except sqlite3.Error:
            raise EvidenceStoreError("Sandbox evidence storage unavailable") from None
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT merchant_id,amount_text,currency,provider_order_id "
                "FROM sandbox_reconciliation_evidence WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is not None and (
                existing["merchant_id"] != merchant_id
                or existing["amount_text"] != amount_text
                or existing["currency"] != "USD"
                or existing["provider_order_id"] != provider_order_id
            ):
                raise EvidenceConflict("Sandbox evidence binding conflict")
            connection.execute(
                """
                INSERT INTO sandbox_reconciliation_evidence (
                    operation_id,source,provider_mode,merchant_id,amount_text,currency,
                    provider_order_id,order_status,provider_capture_id,capture_status,
                    reconciliation_status,error_code,attempted_at,verified_at
                ) VALUES(?, 'manual_sandbox_spike', 'SANDBOX', ?, ?, 'USD', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    order_status=COALESCE(excluded.order_status,
                        sandbox_reconciliation_evidence.order_status),
                    provider_capture_id=COALESCE(excluded.provider_capture_id,
                        sandbox_reconciliation_evidence.provider_capture_id),
                    capture_status=COALESCE(excluded.capture_status,
                        sandbox_reconciliation_evidence.capture_status),
                    reconciliation_status=excluded.reconciliation_status,
                    error_code=excluded.error_code,
                    attempted_at=excluded.attempted_at,
                    verified_at=COALESCE(excluded.verified_at,
                        sandbox_reconciliation_evidence.verified_at)
                """,
                (
                    operation_id,
                    merchant_id,
                    amount_text,
                    provider_order_id,
                    order_status,
                    provider_capture_id,
                    capture_status,
                    reconciliation_status,
                    error_code,
                    attempted_at.isoformat(),
                    verified_at.isoformat() if verified_at else None,
                ),
            )
            connection.execute("COMMIT")
        except EvidenceConflict:
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise EvidenceStoreError("Sandbox evidence storage unavailable") from None
        finally:
            connection.close()

    def record_verified(
        self,
        *,
        operation_id: str,
        merchant_id: str,
        amount_text: str,
        provider_order_id: str,
        order_status: str,
        provider_capture_id: str | None,
        capture_status: str | None,
        attempted_at: datetime,
    ) -> str:
        result = classify_sandbox_status(order_status, capture_status)
        if (provider_capture_id is None) != (capture_status is None):
            raise ValueError("Sandbox capture evidence is incomplete")
        if order_status == "COMPLETED" and result != "COMPLETED":
            raise ValueError("completed Sandbox order lacks completed capture evidence")
        if result == "COMPLETED" and provider_capture_id is None:
            raise ValueError("completed Sandbox evidence requires a capture identifier")
        self._save(
            operation_id=operation_id,
            merchant_id=merchant_id,
            amount_text=amount_text,
            provider_order_id=provider_order_id,
            order_status=order_status,
            provider_capture_id=provider_capture_id,
            capture_status=capture_status,
            reconciliation_status=result,
            error_code=None,
            attempted_at=attempted_at,
            verified_at=attempted_at,
        )
        return result

    def record_error(
        self,
        *,
        operation_id: str,
        merchant_id: str,
        amount_text: str,
        provider_order_id: str,
        error_code: str,
        attempted_at: datetime,
    ) -> None:
        self._save(
            operation_id=operation_id,
            merchant_id=merchant_id,
            amount_text=amount_text,
            provider_order_id=provider_order_id,
            order_status=None,
            provider_capture_id=None,
            capture_status=None,
            reconciliation_status="RECONCILIATION_ERROR",
            error_code=error_code,
            attempted_at=attempted_at,
            verified_at=None,
        )
