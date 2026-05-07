"""Trust-aware scrape scheduling.

The scrape interval is chosen by the per-payer trust score plus a
per-portal health adjustment:

  - Low-trust payers get scraped *more* often. They're the ones likely
    to silently delay AP processing, change payment portals, etc.
  - High-trust payers get scraped *less* often. Their portals rarely
    surprise us, so cadence wastes worker minutes.
  - A flaky portal (lots of recent silent fails) is throttled regardless
    of trust — pounding a broken portal just generates more silent fails
    and no signal.

This is the cleanest place for vertical-1 signals to influence vertical-2
behavior, which is the whole point of the shared trust engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from browser_orchestration.models import PortalHealthEvent


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PortalHealth:
    portal_id: str
    success_rate: float  # 0.0–1.0 over the lookback window
    silent_fail_rate: float  # 0.0–1.0
    sample_size: int

    @property
    def is_flaky(self) -> bool:
        return self.silent_fail_rate >= 0.4 and self.sample_size >= 5


def compute_portal_health(
    session: Session,
    *,
    portal_id: str,
    lookback: timedelta = timedelta(days=7),
    now: datetime | None = None,
) -> PortalHealth:
    """Aggregate the last `lookback` events for one portal."""
    cutoff = (now or _utcnow()) - lookback
    rows = list(
        session.exec(
            select(PortalHealthEvent).where(
                PortalHealthEvent.portal_id == portal_id,
                PortalHealthEvent.occurred_at >= cutoff,
            )
        )
    )
    if not rows:
        return PortalHealth(portal_id, success_rate=1.0, silent_fail_rate=0.0, sample_size=0)

    total = len(rows)
    succeeded = sum(1 for r in rows if r.event == "succeeded")
    silent_fails = sum(1 for r in rows if r.event == "silent_fail")

    return PortalHealth(
        portal_id=portal_id,
        success_rate=succeeded / total,
        silent_fail_rate=silent_fails / total,
        sample_size=total,
    )


# Trust-score → base interval. Lower trust = more frequent scrapes.
# Buckets keep the policy explainable in interviews.
def _base_interval(trust_score: float) -> timedelta:
    if trust_score < 0.30:
        return timedelta(hours=6)
    if trust_score < 0.50:
        return timedelta(hours=12)
    if trust_score < 0.70:
        return timedelta(hours=24)
    return timedelta(days=7)


def scrape_interval(
    *,
    trust_score: float,
    portal_health: PortalHealth | None = None,
) -> timedelta:
    """Pick the next scrape interval for (payer, portal).

    Flaky portals get throttled by 4x to avoid burning workers on a
    broken integration; an oncall engineer should fix the portal before
    cadence resumes.
    """
    base = _base_interval(trust_score)
    if portal_health is not None and portal_health.is_flaky:
        return base * 4
    return base
