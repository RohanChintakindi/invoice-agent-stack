"""Cross-vertical entities: Payer and TrustEventRecord.

Per-vertical data (calls, invoices, scrape jobs) lives in each vertical's
own models module. Anything that's referenced from more than one vertical
lives here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Payer(SQLModel, table=True):
    payer_id: str = Field(primary_key=True)
    name: str
    created_at: datetime = Field(default_factory=_utcnow)


class TrustEventType(str, Enum):
    PROMISE_KEPT = "payment.promise_kept"
    PROMISE_BROKEN = "payment.promise_broken"
    PARTIAL_RECEIVED = "payment.partial_received"
    AUTO_MATCHED = "recon.auto_matched"
    HUMAN_OVERRIDE = "recon.human_override"
    SILENT_FAIL_CAUGHT = "browser.silent_fail_caught"
    CLEAN_EXTRACTION_STREAK = "browser.clean_extraction_streak"
    CALL_HOSTILE = "voice.call_hostile"


# Event → delta applied to the raw trust score (clamped to [0, 1]).
EVENT_DELTAS: dict[TrustEventType, float] = {
    TrustEventType.PROMISE_KEPT: +0.05,
    TrustEventType.PROMISE_BROKEN: -0.10,
    TrustEventType.PARTIAL_RECEIVED: +0.02,
    TrustEventType.AUTO_MATCHED: +0.01,
    TrustEventType.HUMAN_OVERRIDE: -0.02,
    TrustEventType.SILENT_FAIL_CAUGHT: -0.03,
    TrustEventType.CLEAN_EXTRACTION_STREAK: +0.01,
    TrustEventType.CALL_HOSTILE: -0.02,
}


class TrustEventRecord(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    payer_id: str = Field(index=True, foreign_key="payer.payer_id")
    event_type: TrustEventType
    delta: float
    source: str | None = None  # free-text origin: e.g. "voice_agent.call_42"
    occurred_at: datetime = Field(default_factory=_utcnow, index=True)


class PayerTrust(SQLModel, table=True):
    """Cached current raw trust score per payer.

    Updated on each event. Decay is applied at read time, not stored here.
    """

    payer_id: str = Field(primary_key=True, foreign_key="payer.payer_id")
    raw_score: float = 0.5
    last_event_at: datetime | None = None
