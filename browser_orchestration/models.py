"""DB models for the browser orchestration vertical.

Shared cross-vertical entities (Payer, TrustEvent) live in shared/models.py.
Anything specific to portals, jobs, and extractions lives here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SILENT_FAIL = "silent_fail"  # harness reported success, validator disagreed


class JobAction(str, Enum):
    LOGIN = "login"
    EXTRACT_INVOICES = "extract_invoices"
    DOWNLOAD_STATEMENT = "download_statement"
    CONFIRM_PAYMENT = "confirm_payment"


class Portal(SQLModel, table=True):
    portal_id: str = Field(primary_key=True)
    name: str
    base_url: str
    login_url: str | None = None
    requires_2fa: bool = False
    notes: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class Credential(SQLModel, table=True):
    """Encrypted credential — username plaintext, secret stored as Fernet ciphertext.

    The secret blob is opaque to the DB. Only the vault module decrypts it.
    """

    id: int | None = Field(default=None, primary_key=True)
    portal_id: str = Field(index=True, foreign_key="portal.portal_id")
    payer_id: str = Field(index=True, foreign_key="payer.payer_id")
    username: str
    secret_ciphertext: str  # base64-encoded Fernet token
    rotated_at: datetime = Field(default_factory=_utcnow)


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    portal_id: str = Field(index=True, foreign_key="portal.portal_id")
    payer_id: str = Field(index=True, foreign_key="payer.payer_id")
    action: JobAction
    payload_json: str = "{}"  # JSON-encoded action-specific args
    status: JobStatus = Field(default=JobStatus.PENDING, index=True)
    attempts: int = 0
    max_attempts: int = 3
    scheduled_for: datetime = Field(default_factory=_utcnow, index=True)
    last_error: str | None = None
    enqueued_at: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobAttempt(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(index=True, foreign_key="job.id")
    attempt_number: int
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None
    harness_output_json: str | None = None
    screenshot_path: str | None = None
    validator_verdict: str | None = None  # PASS / FAIL / UNKNOWN
    validator_confidence: float | None = None
    validator_rationale: str | None = None
    error: str | None = None


class ExtractionRecord(SQLModel, table=True):
    """One row per validated extraction. Drives the trust signal."""

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(index=True, foreign_key="job.id")
    portal_id: str = Field(index=True, foreign_key="portal.portal_id")
    payer_id: str = Field(index=True, foreign_key="payer.payer_id")
    invoice_count: int = 0
    payload_json: str = "{}"  # JSON list of extracted invoices
    extracted_at: datetime = Field(default_factory=_utcnow)


class PortalHealthEvent(SQLModel, table=True):
    """Per-portal rolling event log used by the observability metrics."""

    id: int | None = Field(default=None, primary_key=True)
    portal_id: str = Field(index=True, foreign_key="portal.portal_id")
    event: str  # "succeeded" | "failed" | "silent_fail"
    occurred_at: datetime = Field(default_factory=_utcnow, index=True)
    detail: str | None = None
