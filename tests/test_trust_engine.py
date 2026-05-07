from datetime import datetime, timedelta, timezone

import pytest

from shared.trust_engine import (
    DECAY_GRACE_DAYS,
    DECAY_WINDOW_DAYS,
    DEFAULT_SCORE,
    TrustEngine,
    _apply_decay,
)
from shared.models import TrustEventType


# ---- defaults / unknown payer ------------------------------------------------


def test_unknown_payer_returns_default_score(session):
    engine = TrustEngine(session)
    assert engine.get_trust("ghost") == DEFAULT_SCORE


# ---- single-event updates ----------------------------------------------------


def test_promise_kept_increases_score(session, payer):
    engine = TrustEngine(session)
    new_score = engine.update_trust(payer, TrustEventType.PROMISE_KEPT)
    assert new_score == pytest.approx(0.55)
    assert engine.get_trust(payer) == pytest.approx(0.55)


def test_promise_broken_decreases_score(session, payer):
    engine = TrustEngine(session)
    new_score = engine.update_trust(payer, TrustEventType.PROMISE_BROKEN)
    assert new_score == pytest.approx(0.40)


def test_score_clamps_at_one(session, payer):
    engine = TrustEngine(session)
    for _ in range(20):  # 20 * 0.05 = 1.0+, must clamp
        engine.update_trust(payer, TrustEventType.PROMISE_KEPT)
    assert engine.get_trust(payer) == 1.0


def test_score_clamps_at_zero(session, payer):
    engine = TrustEngine(session)
    for _ in range(20):
        engine.update_trust(payer, TrustEventType.PROMISE_BROKEN)
    assert engine.get_trust(payer) == 0.0


# ---- event history -----------------------------------------------------------


def test_history_records_each_event(session, payer):
    engine = TrustEngine(session)
    engine.update_trust(payer, TrustEventType.PROMISE_KEPT, source="test_a")
    engine.update_trust(payer, TrustEventType.CALL_HOSTILE, source="test_b")
    history = engine.get_history(payer)
    assert len(history) == 2
    # Most recent first
    assert history[0].event_type == TrustEventType.CALL_HOSTILE
    assert history[0].source == "test_b"
    assert history[1].event_type == TrustEventType.PROMISE_KEPT


# ---- decay -------------------------------------------------------------------


def test_no_decay_within_grace_period():
    fresh = datetime(2026, 5, 1, tzinfo=timezone.utc)
    later = fresh + timedelta(days=15)
    assert _apply_decay(0.9, fresh, later) == 0.9


def test_decay_starts_after_grace_period():
    fresh = datetime(2026, 5, 1, tzinfo=timezone.utc)
    later = fresh + timedelta(days=DECAY_GRACE_DAYS + 1)
    decayed = _apply_decay(0.9, fresh, later)
    assert decayed < 0.9
    assert decayed > 0.5


def test_full_decay_after_grace_plus_window():
    fresh = datetime(2026, 5, 1, tzinfo=timezone.utc)
    later = fresh + timedelta(days=DECAY_GRACE_DAYS + DECAY_WINDOW_DAYS + 10)
    assert _apply_decay(0.9, fresh, later) == pytest.approx(0.5)


def test_decay_drifts_low_scores_upward_too():
    fresh = datetime(2026, 5, 1, tzinfo=timezone.utc)
    later = fresh + timedelta(days=DECAY_GRACE_DAYS + DECAY_WINDOW_DAYS)
    assert _apply_decay(0.1, fresh, later) == pytest.approx(0.5)


def test_get_trust_applies_decay(session, payer):
    fresh_now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    much_later = fresh_now + timedelta(days=400)

    write_engine = TrustEngine(session, now_fn=lambda: fresh_now)
    write_engine.update_trust(payer, TrustEventType.PROMISE_KEPT)
    raw = write_engine.get_trust(payer)
    assert raw == pytest.approx(0.55)

    read_engine = TrustEngine(session, now_fn=lambda: much_later)
    decayed = read_engine.get_trust(payer)
    assert decayed == pytest.approx(0.5, abs=0.01)


# ---- multi-event interaction -------------------------------------------------


def test_mixed_events_compose(session, payer):
    engine = TrustEngine(session)
    engine.update_trust(payer, TrustEventType.PROMISE_KEPT)  # +0.05
    engine.update_trust(payer, TrustEventType.PROMISE_KEPT)  # +0.05
    engine.update_trust(payer, TrustEventType.PROMISE_BROKEN)  # -0.10
    assert engine.get_trust(payer) == pytest.approx(0.50)
