"""SQLModel tables for cash reconciliation.

Conceptual model:
    Invoice  -- the receivable. Lives here (no other vertical owns it).
    WireTransfer -- raw bank feed line.
    MatchCandidate -- one (wire, invoice-or-bundle) pair scored by the ranker.
    Match -- the chosen association after auto-post or human review.
    PayerAlias -- "ACME CORP", "Acme Corporation" -> canonical payer_id.
    ReviewDecision -- audit trail of human actions on borderline matches.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InvoiceStatus(str, Enum):
    OPEN = "open"
    PARTIAL = "partial"
    PAID = "paid"
    WRITTEN_OFF = "written_off"


class Invoice(SQLModel, table=True):
    invoice_id: str = Field(primary_key=True)
    payer_id: str = Field(index=True, foreign_key="payer.payer_id")
    amount: float
    currency: str = "USD"
    issued_on: date
    due_date: date
    status: InvoiceStatus = InvoiceStatus.OPEN
    amount_paid: float = 0.0
    created_at: datetime = Field(default_factory=_utcnow)


class WireStatus(str, Enum):
    PENDING = "pending"          # not yet processed
    AUTO_MATCHED = "auto_matched"
    UNDER_REVIEW = "under_review"
    MATCHED = "matched"          # confirmed (auto or human)
    UNMATCHED = "unmatched"      # nothing plausible found
    REJECTED = "rejected"        # human said this isn't ours


class WireTransfer(SQLModel, table=True):
    wire_id: str = Field(primary_key=True)
    amount: float
    currency: str = "USD"
    received_on: date
    memo: str = ""
    sender_name: str = ""
    bank_ref: str | None = None
    resolved_payer_id: str | None = Field(default=None, index=True)
    status: WireStatus = WireStatus.PENDING
    created_at: datetime = Field(default_factory=_utcnow)


class MatchCandidate(SQLModel, table=True):
    """One scored hypothesis: this wire pays this invoice (or this bundle).

    For a bundle, invoice_ids holds a comma-separated list and is_bundle=True.
    """

    id: int | None = Field(default=None, primary_key=True)
    wire_id: str = Field(index=True, foreign_key="wiretransfer.wire_id")
    invoice_ids: str  # "INV-1023" or "INV-1023,INV-1024"
    is_bundle: bool = False
    raw_score: float = 0.0          # XGBoost output
    calibrated_prob: float = 0.0    # isotonic-calibrated [0,1]
    features_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)


class MatchOutcome(str, Enum):
    AUTO_POSTED = "auto_posted"
    HUMAN_CONFIRMED = "human_confirmed"
    HUMAN_OVERRIDE = "human_override"   # human picked a different candidate
    HUMAN_REJECTED = "human_rejected"   # not our payment


class Match(SQLModel, table=True):
    """The chosen pairing (wire <-> one or more invoices)."""

    id: int | None = Field(default=None, primary_key=True)
    wire_id: str = Field(index=True, foreign_key="wiretransfer.wire_id")
    invoice_ids: str
    is_bundle: bool = False
    confidence: float = 0.0      # calibrated prob at decision time
    outcome: MatchOutcome
    reviewer: str | None = None  # human id or "auto"
    decided_at: datetime = Field(default_factory=_utcnow)


class ReviewDecision(SQLModel, table=True):
    """Audit log for review-queue actions. One row per human click."""

    id: int | None = Field(default=None, primary_key=True)
    wire_id: str = Field(index=True)
    candidate_id: int | None = None
    action: str  # "confirm", "reject", "override"
    note: str = ""
    reviewer: str = "anonymous"
    decided_at: datetime = Field(default_factory=_utcnow)


class PayerAlias(SQLModel, table=True):
    """Memo / sender-name aliases that map to a canonical payer_id."""

    id: int | None = Field(default=None, primary_key=True)
    alias: str = Field(index=True)
    payer_id: str = Field(index=True, foreign_key="payer.payer_id")
    source: str = "manual"  # "manual" | "learned"
    created_at: datetime = Field(default_factory=_utcnow)
