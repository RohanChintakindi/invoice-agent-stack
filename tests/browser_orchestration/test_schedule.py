"""Trust-aware scrape interval policy."""

from __future__ import annotations

from datetime import timedelta

import pytest

from browser_orchestration.models import Portal, PortalHealthEvent
from browser_orchestration.schedule import (
    PortalHealth,
    compute_portal_health,
    scrape_interval,
)


@pytest.fixture
def portal(session) -> str:
    p = Portal(portal_id="acme_portal", name="Acme", base_url="https://x")
    session.add(p)
    session.commit()
    return p.portal_id


def test_low_trust_gets_aggressive_cadence():
    interval = scrape_interval(trust_score=0.2)
    assert interval == timedelta(hours=6)


def test_mid_trust_gets_daily():
    interval = scrape_interval(trust_score=0.6)
    assert interval == timedelta(hours=24)


def test_high_trust_gets_weekly():
    interval = scrape_interval(trust_score=0.85)
    assert interval == timedelta(days=7)


def test_flaky_portal_throttled_4x():
    flaky = PortalHealth(
        portal_id="x", success_rate=0.4, silent_fail_rate=0.5, sample_size=10
    )
    base = scrape_interval(trust_score=0.2)
    throttled = scrape_interval(trust_score=0.2, portal_health=flaky)
    assert throttled == base * 4


def test_compute_portal_health_with_no_events_returns_clean(session, portal):
    health = compute_portal_health(session, portal_id=portal)
    assert health.success_rate == 1.0
    assert health.silent_fail_rate == 0.0
    assert health.sample_size == 0
    assert not health.is_flaky


def test_compute_portal_health_flags_flaky(session, portal):
    for _ in range(6):
        session.add(PortalHealthEvent(portal_id=portal, event="silent_fail"))
    for _ in range(4):
        session.add(PortalHealthEvent(portal_id=portal, event="succeeded"))
    session.commit()

    health = compute_portal_health(session, portal_id=portal)
    assert health.silent_fail_rate >= 0.4
    assert health.is_flaky
