"""Worker integration: enqueue → process → outcome + trust event."""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

# Register models with SQLModel.metadata.
import browser_orchestration.models  # noqa: F401
import shared.models  # noqa: F401
import voice_agent.memory_models  # noqa: F401

from browser_orchestration.harness_adapter import (
    MockHarness,
    script_clean_extraction,
    script_hard_failure,
    script_silent_fail,
)
from browser_orchestration.models import (
    ExtractionRecord,
    JobAction,
    JobStatus,
    Portal,
    PortalHealthEvent,
)
from browser_orchestration.queue import SqliteJobQueue
from browser_orchestration.vault import CredentialVault, generate_key
from browser_orchestration.worker import (
    CLEAN_STREAK_THRESHOLD,
    process_one_job,
)
from shared.models import Payer, TrustEventRecord, TrustEventType
from voice_agent.llm import FakeLLMClient


@pytest.fixture
def shared_engine():
    """File-less engine that stays alive across all sessions in one test."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def vault() -> CredentialVault:
    return CredentialVault(generate_key())


def _bootstrap(engine, vault: CredentialVault, *, portal_id: str) -> None:
    with Session(engine) as s:
        if s.get(Payer, "acme") is None:
            s.add(Payer(payer_id="acme", name="Acme"))
        if s.get(Portal, portal_id) is None:
            s.add(Portal(portal_id=portal_id, name=portal_id, base_url="https://x"))
        s.commit()
        vault.store(s, portal_id=portal_id, payer_id="acme",
                    username="ap@acme", secret="hunter2")
        s.commit()


def _enqueue(engine, *, portal_id: str) -> int:
    with Session(engine) as s:
        q = SqliteJobQueue(s)
        job_id = q.enqueue(
            portal_id=portal_id, payer_id="acme",
            action=JobAction.EXTRACT_INVOICES,
        )
        s.commit()
    return job_id


def test_happy_path_writes_extraction_no_trust_event_yet(shared_engine, vault):
    _bootstrap(shared_engine, vault, portal_id="acme_portal")
    _enqueue(shared_engine, portal_id="acme_portal")

    harness = MockHarness({"acme_portal": script_clean_extraction(
        invoices=[{"invoice_id": "INV-1", "amount": 100.0,
                   "due_date": "2026-01-01", "status": "open"}]
    )})

    outcome = process_one_job(
        engine=shared_engine, harness=harness, vault=vault, llm=None,
    )
    assert outcome is not None
    assert outcome.status is JobStatus.SUCCEEDED
    assert outcome.validation.verdict.value == "pass"

    with Session(shared_engine) as s:
        rec = s.exec(select(ExtractionRecord)).first()
        assert rec is not None
        assert rec.invoice_count == 1
        # Streak threshold is 5, so first success should NOT emit a trust event.
        events = s.exec(select(TrustEventRecord)).all()
        assert events == []


def test_silent_fail_caught_emits_trust_event(shared_engine, vault):
    _bootstrap(shared_engine, vault, portal_id="zenith_portal")
    _enqueue(shared_engine, portal_id="zenith_portal")

    harness = MockHarness({"zenith_portal": script_silent_fail()})
    llm = FakeLLMClient(
        complete_responses=["verdict: fail\nconfidence: 0.9\nwhy: session expired\n"]
    )

    outcome = process_one_job(
        engine=shared_engine, harness=harness, vault=vault, llm=llm,
    )
    assert outcome is not None
    assert outcome.status is JobStatus.SILENT_FAIL
    assert outcome.trust_event is TrustEventType.SILENT_FAIL_CAUGHT

    with Session(shared_engine) as s:
        events = list(s.exec(select(TrustEventRecord)))
        assert len(events) == 1
        assert events[0].event_type is TrustEventType.SILENT_FAIL_CAUGHT
        health = list(s.exec(select(PortalHealthEvent)))
        assert any(h.event == "silent_fail" for h in health)


def test_hard_failure_retries(shared_engine, vault):
    _bootstrap(shared_engine, vault, portal_id="zenith_portal")
    _enqueue(shared_engine, portal_id="zenith_portal")

    harness = MockHarness({"zenith_portal": script_hard_failure("timeout")})

    outcome = process_one_job(
        engine=shared_engine, harness=harness, vault=vault, llm=None,
    )
    assert outcome is not None
    # max_attempts default is 3, so this is retryable → status PENDING.
    assert outcome.status is JobStatus.PENDING


def test_clean_streak_emits_trust_event_at_threshold(shared_engine, vault):
    _bootstrap(shared_engine, vault, portal_id="acme_portal")
    harness = MockHarness({"acme_portal": script_clean_extraction(
        invoices=[{"invoice_id": "X", "amount": 1.0,
                   "due_date": "2026-01-01", "status": "open"}]
    )})

    last_outcome = None
    for _ in range(CLEAN_STREAK_THRESHOLD):
        _enqueue(shared_engine, portal_id="acme_portal")
        last_outcome = process_one_job(
            engine=shared_engine, harness=harness, vault=vault, llm=None,
        )

    assert last_outcome is not None
    assert last_outcome.trust_event is TrustEventType.CLEAN_EXTRACTION_STREAK


def test_process_one_job_returns_none_when_queue_empty(shared_engine, vault):
    harness = MockHarness()
    assert process_one_job(
        engine=shared_engine, harness=harness, vault=vault, llm=None,
    ) is None


def test_missing_credential_marks_failed(shared_engine, vault):
    # Bootstrap portal but DON'T store a credential.
    with Session(shared_engine) as s:
        s.add(Payer(payer_id="acme", name="Acme"))
        s.add(Portal(portal_id="naked_portal", name="Naked", base_url="https://x"))
        s.commit()
    _enqueue(shared_engine, portal_id="naked_portal")

    harness = MockHarness()
    outcome = process_one_job(
        engine=shared_engine, harness=harness, vault=vault, llm=None,
    )
    assert outcome is not None
    # Retryable error since attempts < max_attempts.
    assert outcome.status in {JobStatus.PENDING, JobStatus.FAILED}
    assert outcome.error is not None
