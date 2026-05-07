"""Worker loop: claim → execute → validate → record → emit trust event.

One iteration is `process_one_job(...)`. The async loop in `run_worker`
just calls it until told to stop. Splitting them out makes each step
unit-testable and lets the demo CLI run a single tick.

Concurrency model:
  - Each job runs in its own session (one transaction per job lifecycle).
  - Multiple workers are safe because `claim_next` flips PENDING→RUNNING
    atomically.
  - The validator's LLM call runs synchronously inside the worker — it's
    a single short call so we don't bother offloading to a thread pool.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session

from browser_orchestration.harness_adapter import (
    HarnessAdapter,
    HarnessRequest,
    HarnessResult,
)
from browser_orchestration.models import (
    ExtractionRecord,
    Job,
    JobAttempt,
    JobStatus,
    Portal,
    PortalHealthEvent,
)
from browser_orchestration.queue import ClaimedJob, SqliteJobQueue
from browser_orchestration.validator import ValidationResult, Verdict, validate
from browser_orchestration.vault import CredentialVault, VaultError
from shared.db import session_scope
from shared.models import TrustEventType
from shared.trust_engine import TrustEngine
from voice_agent.llm import LLMClient

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class JobOutcome:
    job_id: int
    status: JobStatus
    validation: ValidationResult | None
    extraction_id: int | None
    trust_event: TrustEventType | None
    error: str | None = None


# ---------------------------------------------------------------------------
# Streak tracking. CLEAN_EXTRACTION_STREAK fires every N consecutive
# successful extractions; we read recent ExtractionRecord rows to decide.
# Keeps trust events sparse so they don't dominate the score.
# ---------------------------------------------------------------------------

CLEAN_STREAK_THRESHOLD = 5


def _should_emit_clean_streak(session: Session, *, payer_id: str) -> bool:
    """True every Nth consecutive successful extraction for this payer."""
    from sqlmodel import select

    rows = list(
        session.exec(
            select(ExtractionRecord)
            .where(ExtractionRecord.payer_id == payer_id)
            .order_by(ExtractionRecord.extracted_at.desc())
            .limit(CLEAN_STREAK_THRESHOLD)
        )
    )
    return len(rows) >= CLEAN_STREAK_THRESHOLD and len(rows) % CLEAN_STREAK_THRESHOLD == 0


# ---------------------------------------------------------------------------
# Single-job executor.
# ---------------------------------------------------------------------------


def process_one_job(
    *,
    engine,
    harness: HarnessAdapter,
    vault: CredentialVault,
    llm: LLMClient | None,
) -> JobOutcome | None:
    """Pop one job, run it end-to-end, return the outcome.

    Returns None if the queue had nothing ready. Each call uses its own
    DB session so workers can be parallelized later.
    """
    with session_scope(engine) as session:
        queue = SqliteJobQueue(session)
        claimed = queue.claim_next()
        if claimed is None:
            return None

        # Record attempt row up front so its lifecycle survives crashes.
        attempt = JobAttempt(
            job_id=claimed.job_id,
            attempt_number=claimed.attempt_number,
        )
        session.add(attempt)
        session.flush()
        attempt_id = attempt.id

    # Run the actual work in a fresh session so a long harness call
    # doesn't hold a write transaction.
    return _run_attempt(
        engine=engine,
        harness=harness,
        vault=vault,
        llm=llm,
        claimed=claimed,
        attempt_id=attempt_id,  # type: ignore[arg-type]
    )


def _run_attempt(
    *,
    engine,
    harness: HarnessAdapter,
    vault: CredentialVault,
    llm: LLMClient | None,
    claimed: ClaimedJob,
    attempt_id: int,
) -> JobOutcome:
    # ---- Step 1: load portal + credential ------------------------------
    with session_scope(engine) as session:
        portal = session.get(Portal, claimed.portal_id)
        if portal is None:
            return _finalize_hard_failure(
                engine,
                claimed=claimed,
                attempt_id=attempt_id,
                error=f"portal {claimed.portal_id} not found",
            )
        try:
            username, secret = vault.reveal_for(
                session, portal_id=claimed.portal_id, payer_id=claimed.payer_id
            )
        except VaultError as exc:
            return _finalize_hard_failure(
                engine,
                claimed=claimed,
                attempt_id=attempt_id,
                error=str(exc),
            )

        request = HarnessRequest(
            portal_id=portal.portal_id,
            base_url=portal.base_url,
            action=claimed.action,
            payload=claimed.payload,
            username=username,
            secret=secret,
        )

    # ---- Step 2: execute (outside DB transaction) ----------------------
    try:
        harness_result = harness.execute(request)
    except Exception as exc:  # noqa: BLE001
        log.exception("harness raised for job %s", claimed.job_id)
        harness_result = HarnessResult(ok=False, error=f"adapter raised: {exc}")

    # ---- Step 3: validate ----------------------------------------------
    validation = validate(action=claimed.action, harness=harness_result, llm=llm)

    # ---- Step 4: persist outcome ---------------------------------------
    with session_scope(engine) as session:
        attempt = session.get(JobAttempt, attempt_id)
        if attempt is not None:
            attempt.finished_at = _utcnow()
            attempt.harness_output_json = json.dumps(harness_result.output)[:4000]
            attempt.screenshot_path = harness_result.screenshot_path
            attempt.validator_verdict = validation.verdict.value.upper()
            attempt.validator_confidence = validation.confidence
            attempt.validator_rationale = validation.rationale
            if not harness_result.ok:
                attempt.error = harness_result.error
            session.add(attempt)

        queue = SqliteJobQueue(session)
        trust = TrustEngine(session)
        outcome_status: JobStatus
        trust_event: TrustEventType | None = None
        extraction_id: int | None = None
        error_msg = harness_result.error or validation.rationale

        if validation.verdict is Verdict.PASS:
            queue.mark_succeeded(claimed.job_id)
            outcome_status = JobStatus.SUCCEEDED

            # Record the extraction (if there was a payload to record).
            payload_dict = validation.parsed_payload or {}
            invoice_count = (
                len(payload_dict.get("invoices", []))
                if isinstance(payload_dict, dict)
                else 0
            )
            record = ExtractionRecord(
                job_id=claimed.job_id,
                portal_id=claimed.portal_id,
                payer_id=claimed.payer_id,
                invoice_count=invoice_count,
                payload_json=json.dumps(payload_dict)[:4000],
            )
            session.add(record)
            session.flush()
            extraction_id = record.id

            session.add(
                PortalHealthEvent(portal_id=claimed.portal_id, event="succeeded")
            )

            if _should_emit_clean_streak(session, payer_id=claimed.payer_id):
                trust.update_trust(
                    claimed.payer_id,
                    TrustEventType.CLEAN_EXTRACTION_STREAK,
                    source=f"browser.job_{claimed.job_id}",
                )
                trust_event = TrustEventType.CLEAN_EXTRACTION_STREAK

        elif validation.verdict is Verdict.FAIL and harness_result.ok:
            # The defining signal: harness said ok, validator disagreed.
            queue.mark_failed(
                claimed.job_id,
                error=validation.rationale,
                silent_fail=True,
            )
            outcome_status = JobStatus.SILENT_FAIL
            session.add(
                PortalHealthEvent(
                    portal_id=claimed.portal_id,
                    event="silent_fail",
                    detail=validation.rationale[:200],
                )
            )
            trust.update_trust(
                claimed.payer_id,
                TrustEventType.SILENT_FAIL_CAUGHT,
                source=f"browser.job_{claimed.job_id}",
            )
            trust_event = TrustEventType.SILENT_FAIL_CAUGHT

        else:
            # Hard harness failure (or both checks failed) — retryable.
            queue.mark_failed(claimed.job_id, error=error_msg or "unknown error")
            job = session.get(Job, claimed.job_id)
            outcome_status = (
                job.status if job is not None else JobStatus.FAILED
            )
            session.add(
                PortalHealthEvent(
                    portal_id=claimed.portal_id,
                    event="failed",
                    detail=(error_msg or "")[:200],
                )
            )

    return JobOutcome(
        job_id=claimed.job_id,
        status=outcome_status,
        validation=validation,
        extraction_id=extraction_id,
        trust_event=trust_event,
        error=error_msg if outcome_status not in (JobStatus.SUCCEEDED,) else None,
    )


def _finalize_hard_failure(
    engine,
    *,
    claimed: ClaimedJob,
    attempt_id: int,
    error: str,
) -> JobOutcome:
    """Used when we can't even start the attempt (missing portal/cred)."""
    with session_scope(engine) as session:
        attempt = session.get(JobAttempt, attempt_id)
        if attempt is not None:
            attempt.finished_at = _utcnow()
            attempt.error = error
            session.add(attempt)
        queue = SqliteJobQueue(session)
        queue.mark_failed(claimed.job_id, error=error)
        session.add(PortalHealthEvent(portal_id=claimed.portal_id, event="failed", detail=error[:200]))
        job = session.get(Job, claimed.job_id)
        outcome_status = job.status if job is not None else JobStatus.FAILED

    return JobOutcome(
        job_id=claimed.job_id,
        status=outcome_status,
        validation=None,
        extraction_id=None,
        trust_event=None,
        error=error,
    )


# ---------------------------------------------------------------------------
# Async worker loop. Real deployments would run this in a separate process.
# ---------------------------------------------------------------------------


async def run_worker(
    *,
    engine,
    harness: HarnessAdapter,
    vault: CredentialVault,
    llm: LLMClient | None,
    poll_interval: float = 1.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        outcome = await asyncio.to_thread(
            process_one_job,
            engine=engine,
            harness=harness,
            vault=vault,
            llm=llm,
        )
        if outcome is None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
            continue
        log.info(
            "job %s finished status=%s verdict=%s",
            outcome.job_id,
            outcome.status.value,
            outcome.validation.verdict.value if outcome.validation else "n/a",
        )
