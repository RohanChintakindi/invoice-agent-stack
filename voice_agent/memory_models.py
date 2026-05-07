"""Per-payer voice-agent memory: contacts, calls, promises, objections."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Contact(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    payer_id: str = Field(index=True, foreign_key="payer.payer_id")
    name: str
    role: str | None = None
    preferred_time: str | None = None  # e.g. "mornings", "after 2pm Eastern"
    notes: str | None = None


class CallOutcome(str, Enum):
    PROMISE_MADE = "promise_made"
    PARTIAL_PROMISE = "partial_promise"
    DISPUTED = "disputed"
    CALLBACK_REQUESTED = "callback_requested"
    HANDOFF_TO_HUMAN = "handoff_to_human"
    NO_ANSWER = "no_answer"
    HOSTILE = "hostile"
    OTHER = "other"


class Call(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    payer_id: str = Field(index=True, foreign_key="payer.payer_id")
    invoice_id: str | None = None  # optional — call may concern a specific invoice
    occurred_at: datetime = Field(default_factory=_utcnow, index=True)
    duration_sec: int | None = None
    summary: str
    outcome: CallOutcome
    final_phase: str | None = None  # captured from state machine at end of call
    final_tone: str | None = None
    contact_name: str | None = None  # who we spoke with on this call


class Promise(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    payer_id: str = Field(index=True, foreign_key="payer.payer_id")
    call_id: int | None = Field(default=None, foreign_key="call.id")
    invoice_id: str | None = None
    promised_date: datetime
    promised_amount: float | None = None
    kept: bool | None = None  # None = unresolved
    resolved_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class Objection(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    payer_id: str = Field(index=True, foreign_key="payer.payer_id")
    kind: str  # e.g. "approvals_delay", "missing_po", "amount_dispute"
    text: str  # verbatim or summarized
    occurred_at: datetime = Field(default_factory=_utcnow)
