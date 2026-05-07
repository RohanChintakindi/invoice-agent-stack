"""Per-payer trust score service.

Read path: get_trust(payer_id) returns the score with time-decay applied.
Write path: update_trust(payer_id, event) applies a delta, records the
event, and returns the new raw score.

Decay: after 30 days with no event, the raw score drifts linearly toward
0.5 over a 180-day window. This represents fading confidence — a payer
who paid clean a year ago probably shouldn't keep auto-matching at the
same threshold without recent signal.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from shared.models import EVENT_DELTAS, PayerTrust, TrustEventRecord, TrustEventType

DEFAULT_SCORE = 0.5
DECAY_GRACE_DAYS = 30
DECAY_WINDOW_DAYS = 180


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clamp(score: float) -> float:
    return max(0.0, min(1.0, score))


def _apply_decay(raw_score: float, last_event_at: datetime | None, now: datetime) -> float:
    """Drift score toward 0.5 when no event has occurred recently."""
    if last_event_at is None:
        return raw_score

    # Make sure both datetimes are timezone-aware for subtraction.
    if last_event_at.tzinfo is None:
        last_event_at = last_event_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    days_since = (now - last_event_at).days
    if days_since <= DECAY_GRACE_DAYS:
        return raw_score

    excess = days_since - DECAY_GRACE_DAYS
    drift = min(1.0, excess / DECAY_WINDOW_DAYS)
    return raw_score * (1 - drift) + DEFAULT_SCORE * drift


class TrustEngine:
    """Persistence-backed trust score store. Inject a SQLModel Session."""

    def __init__(self, session: Session, now_fn=_utcnow):
        self._session = session
        self._now = now_fn

    def get_trust(self, payer_id: str) -> float:
        row = self._session.get(PayerTrust, payer_id)
        if row is None:
            return DEFAULT_SCORE
        return _clamp(_apply_decay(row.raw_score, row.last_event_at, self._now()))

    def update_trust(
        self,
        payer_id: str,
        event: TrustEventType,
        source: str | None = None,
    ) -> float:
        delta = EVENT_DELTAS[event]
        now = self._now()

        row = self._session.get(PayerTrust, payer_id)
        if row is None:
            row = PayerTrust(payer_id=payer_id, raw_score=DEFAULT_SCORE, last_event_at=None)

        new_score = _clamp(row.raw_score + delta)
        row.raw_score = new_score
        row.last_event_at = now
        self._session.add(row)

        record = TrustEventRecord(
            payer_id=payer_id,
            event_type=event,
            delta=delta,
            source=source,
            occurred_at=now,
        )
        self._session.add(record)
        self._session.commit()
        self._session.refresh(row)
        return new_score

    def get_history(self, payer_id: str, limit: int = 50) -> list[TrustEventRecord]:
        stmt = (
            select(TrustEventRecord)
            .where(TrustEventRecord.payer_id == payer_id)
            .order_by(TrustEventRecord.occurred_at.desc())
            .limit(limit)
        )
        return list(self._session.exec(stmt))
