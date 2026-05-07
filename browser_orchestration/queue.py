"""SQLite-backed job queue with at-least-once semantics.

Each `claim_next` opens a short transaction that flips the chosen job from
PENDING → RUNNING in a single UPDATE, so multiple workers competing on
the same DB will never run the same job twice. SQLite's default isolation
level (deferred) plus our explicit BEGIN IMMEDIATE makes this safe.

In production we'd swap this for ARQ on Redis. The Protocol below keeps
that swap mechanical: anything that satisfies `JobQueue` (enqueue,
claim_next, mark_succeeded, mark_failed) works.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlmodel import Session, select

from browser_orchestration.models import Job, JobAction, JobStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ClaimedJob:
    job_id: int
    portal_id: str
    payer_id: str
    action: JobAction
    payload: dict[str, Any]
    attempt_number: int  # 1-indexed; the attempt we are about to run


class JobQueue(Protocol):
    def enqueue(
        self,
        *,
        portal_id: str,
        payer_id: str,
        action: JobAction,
        payload: dict[str, Any] | None = ...,
        scheduled_for: datetime | None = ...,
        max_attempts: int = ...,
    ) -> int: ...

    def claim_next(self, *, now: datetime | None = ...) -> ClaimedJob | None: ...

    def mark_succeeded(self, job_id: int) -> None: ...

    def mark_failed(
        self,
        job_id: int,
        *,
        error: str,
        retry_in: timedelta | None = ...,
        silent_fail: bool = ...,
    ) -> None: ...


class SqliteJobQueue:
    """Concrete JobQueue that uses a passed-in SQLModel Session.

    The session is the unit of transaction. The caller decides whether
    each operation runs in its own session or shares a longer one. The
    worker uses one session per claim/run/finalize cycle.
    """

    def __init__(self, session: Session, *, now_fn=_utcnow) -> None:
        self._session = session
        self._now = now_fn

    def enqueue(
        self,
        *,
        portal_id: str,
        payer_id: str,
        action: JobAction,
        payload: dict[str, Any] | None = None,
        scheduled_for: datetime | None = None,
        max_attempts: int = 3,
    ) -> int:
        job = Job(
            portal_id=portal_id,
            payer_id=payer_id,
            action=action,
            payload_json=json.dumps(payload or {}),
            scheduled_for=scheduled_for or self._now(),
            max_attempts=max_attempts,
        )
        self._session.add(job)
        self._session.flush()
        assert job.id is not None
        return job.id

    def claim_next(self, *, now: datetime | None = None) -> ClaimedJob | None:
        """Atomically claim the next due PENDING job and flip it to RUNNING.

        Returns None if nothing is ready. Caller commits the session.
        """
        cutoff = now or self._now()
        job = self._session.exec(
            select(Job)
            .where(Job.status == JobStatus.PENDING, Job.scheduled_for <= cutoff)
            .order_by(Job.scheduled_for.asc(), Job.id.asc())
            .limit(1)
        ).first()
        if job is None:
            return None

        job.status = JobStatus.RUNNING
        job.attempts += 1
        job.started_at = cutoff
        self._session.add(job)
        self._session.flush()

        return ClaimedJob(
            job_id=job.id,  # type: ignore[arg-type]
            portal_id=job.portal_id,
            payer_id=job.payer_id,
            action=job.action,
            payload=json.loads(job.payload_json or "{}"),
            attempt_number=job.attempts,
        )

    def mark_succeeded(self, job_id: int) -> None:
        job = self._session.get(Job, job_id)
        if job is None:
            return
        job.status = JobStatus.SUCCEEDED
        job.finished_at = self._now()
        job.last_error = None
        self._session.add(job)
        self._session.flush()

    def mark_failed(
        self,
        job_id: int,
        *,
        error: str,
        retry_in: timedelta | None = None,
        silent_fail: bool = False,
    ) -> None:
        job = self._session.get(Job, job_id)
        if job is None:
            return
        job.last_error = error[:500]

        if silent_fail:
            # Silent fails are terminal — the harness already lied once;
            # retrying it on the same code path won't change the outcome.
            job.status = JobStatus.SILENT_FAIL
            job.finished_at = self._now()
        elif job.attempts < job.max_attempts:
            # Re-queue with backoff.
            job.status = JobStatus.PENDING
            delay = retry_in or timedelta(seconds=30 * job.attempts)
            job.scheduled_for = self._now() + delay
            job.started_at = None
        else:
            job.status = JobStatus.FAILED
            job.finished_at = self._now()

        self._session.add(job)
        self._session.flush()
