"""SqliteJobQueue: enqueue / claim_next / mark_succeeded / mark_failed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from browser_orchestration.models import (
    Job,
    JobAction,
    JobStatus,
    Portal,
)
from browser_orchestration.queue import SqliteJobQueue


@pytest.fixture
def portal(session) -> str:
    p = Portal(portal_id="acme_portal", name="Acme", base_url="https://x")
    session.add(p)
    session.commit()
    return p.portal_id


def test_enqueue_creates_pending_job(session, portal, payer):
    q = SqliteJobQueue(session)
    job_id = q.enqueue(portal_id=portal, payer_id=payer, action=JobAction.EXTRACT_INVOICES)
    job = session.get(Job, job_id)
    assert job is not None
    assert job.status == JobStatus.PENDING
    assert job.attempts == 0


def test_claim_next_marks_running_and_increments_attempts(session, portal, payer):
    q = SqliteJobQueue(session)
    q.enqueue(portal_id=portal, payer_id=payer, action=JobAction.EXTRACT_INVOICES)
    claimed = q.claim_next()
    assert claimed is not None
    job = session.get(Job, claimed.job_id)
    assert job.status == JobStatus.RUNNING
    assert job.attempts == 1
    assert claimed.attempt_number == 1


def test_claim_next_returns_none_when_empty(session, payer):
    q = SqliteJobQueue(session)
    assert q.claim_next() is None


def test_claim_next_skips_future_scheduled(session, portal, payer):
    q = SqliteJobQueue(session)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    q.enqueue(
        portal_id=portal, payer_id=payer,
        action=JobAction.EXTRACT_INVOICES, scheduled_for=future,
    )
    assert q.claim_next() is None


def test_claim_next_returns_oldest_first(session, portal, payer):
    q = SqliteJobQueue(session)
    older = datetime.now(timezone.utc) - timedelta(minutes=5)
    newer = datetime.now(timezone.utc)
    a = q.enqueue(portal_id=portal, payer_id=payer, action=JobAction.LOGIN, scheduled_for=newer)
    b = q.enqueue(portal_id=portal, payer_id=payer, action=JobAction.LOGIN, scheduled_for=older)
    claimed = q.claim_next()
    assert claimed is not None
    assert claimed.job_id == b
    _ = a  # unused but kept for clarity


def test_mark_succeeded_terminates_job(session, portal, payer):
    q = SqliteJobQueue(session)
    job_id = q.enqueue(portal_id=portal, payer_id=payer, action=JobAction.LOGIN)
    q.claim_next()
    q.mark_succeeded(job_id)
    job = session.get(Job, job_id)
    assert job.status == JobStatus.SUCCEEDED
    assert job.finished_at is not None


def test_mark_failed_retries_with_backoff(session, portal, payer):
    q = SqliteJobQueue(session)
    job_id = q.enqueue(
        portal_id=portal, payer_id=payer,
        action=JobAction.LOGIN, max_attempts=3,
    )
    q.claim_next()
    q.mark_failed(job_id, error="boom")
    job = session.get(Job, job_id)
    assert job.status == JobStatus.PENDING
    # SQLite drops tzinfo on roundtrip — compare against naive utcnow.
    naive_now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert job.scheduled_for > naive_now - timedelta(seconds=1)
    assert job.last_error == "boom"


def test_mark_failed_terminates_after_max_attempts(session, portal, payer):
    q = SqliteJobQueue(session)
    job_id = q.enqueue(
        portal_id=portal, payer_id=payer,
        action=JobAction.LOGIN, max_attempts=2,
    )
    # Attempt 1
    q.claim_next()
    q.mark_failed(job_id, error="boom1")
    # Attempt 2 -- need to advance scheduled_for; just claim again with future now
    job = session.get(Job, job_id)
    job.scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.add(job)
    session.flush()
    q.claim_next()
    q.mark_failed(job_id, error="boom2")
    job = session.get(Job, job_id)
    assert job.status == JobStatus.FAILED


def test_mark_failed_silent_terminates_immediately(session, portal, payer):
    q = SqliteJobQueue(session)
    job_id = q.enqueue(
        portal_id=portal, payer_id=payer,
        action=JobAction.EXTRACT_INVOICES, max_attempts=5,
    )
    q.claim_next()
    q.mark_failed(job_id, error="silent fail caught", silent_fail=True)
    job = session.get(Job, job_id)
    assert job.status == JobStatus.SILENT_FAIL
    assert job.finished_at is not None
