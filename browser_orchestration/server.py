"""FastAPI server exposing the browser orchestration vertical.

Endpoints:
  POST /jobs                     enqueue a new browser job
  GET  /jobs/{job_id}            inspect status, attempts, validator verdict
  GET  /portals/{portal_id}/health   rolling success/silent-fail rates
  POST /jobs/{job_id}/run        process this single job in-process (demo)
  GET  /health                   liveness

The /jobs/{job_id}/run endpoint exists so the demo can drive a job
end-to-end without a long-running worker process. In production the
worker runs continuously in its own pod.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from browser_orchestration.harness_adapter import (
    HarnessAdapter,
    MockHarness,
    script_clean_extraction,
    script_silent_fail,
)
from browser_orchestration.models import Job, JobAction, JobAttempt, Portal
from browser_orchestration.queue import SqliteJobQueue
from browser_orchestration.schedule import (
    PortalHealth,
    compute_portal_health,
    scrape_interval,
)
from browser_orchestration.vault import CredentialVault
from browser_orchestration.worker import process_one_job
from shared.db import init_schema, make_engine, session_scope
from shared.trust_engine import TrustEngine
from voice_agent.llm import AnthropicClient, FakeLLMClient, LLMClient


# ---- Request / response models ---------------------------------------------


class EnqueueJobRequest(BaseModel):
    portal_id: str
    payer_id: str
    action: JobAction
    payload: dict[str, Any] | None = None
    max_attempts: int = 3


class EnqueueJobResponse(BaseModel):
    job_id: int
    status: str


class JobView(BaseModel):
    id: int
    portal_id: str
    payer_id: str
    action: str
    status: str
    attempts: int
    last_error: str | None
    attempts_log: list[dict[str, Any]]


class PortalHealthView(BaseModel):
    portal_id: str
    success_rate: float
    silent_fail_rate: float
    sample_size: int
    is_flaky: bool
    recommended_interval_for_trust_0_5: float  # seconds, for quick demo


class RunJobResponse(BaseModel):
    ran: bool
    job_id: int | None
    status: str | None
    verdict: str | None
    confidence: float | None
    rationale: str | None
    trust_event: str | None


# ---- App factory -----------------------------------------------------------


def _build_default_harness() -> HarnessAdapter:
    """Demo harness: portal_acme returns clean invoices, portal_zenith does silent-fail."""
    h = MockHarness()
    h.register(
        "acme_portal",
        script_clean_extraction(
            invoices=[
                {"invoice_id": "INV-1023", "amount": 12000.0, "due_date": "2026-04-01", "status": "open"},
                {"invoice_id": "INV-1041", "amount": 4500.0, "due_date": "2026-05-01", "status": "open"},
            ]
        ),
    )
    h.register("zenith_portal", script_silent_fail())
    return h


def _build_llm() -> LLMClient | None:
    if os.getenv("BROWSER_FAKE_LLM") == "1":
        return FakeLLMClient(complete_responses=["verdict: pass\nconfidence: 0.9\nwhy: ok"])
    if os.getenv("ANTHROPIC_API_KEY"):
        return AnthropicClient()
    # Without an LLM, validation falls back to schema-only.
    return None


def _build_vault() -> CredentialVault:
    return CredentialVault.from_env()


def create_app(
    *,
    harness: HarnessAdapter | None = None,
    llm: LLMClient | None = None,
    vault: CredentialVault | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine()
        init_schema(engine)
        app.state.engine = engine
        app.state.harness = harness or _build_default_harness()
        app.state.vault = vault or _build_vault()
        app.state.llm = llm if llm is not None else _build_llm()
        yield

    app = FastAPI(lifespan=lifespan, title="Iridium browser orchestration")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/jobs", response_model=EnqueueJobResponse)
    def enqueue_job(req: EnqueueJobRequest) -> EnqueueJobResponse:
        with session_scope(app.state.engine) as session:
            portal = session.get(Portal, req.portal_id)
            if portal is None:
                raise HTTPException(404, f"portal {req.portal_id} not found")
            queue = SqliteJobQueue(session)
            job_id = queue.enqueue(
                portal_id=req.portal_id,
                payer_id=req.payer_id,
                action=req.action,
                payload=req.payload or {},
                max_attempts=req.max_attempts,
            )
        return EnqueueJobResponse(job_id=job_id, status="pending")

    @app.get("/jobs/{job_id}", response_model=JobView)
    def get_job(job_id: int) -> JobView:
        with session_scope(app.state.engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(404, "job not found")
            attempts = list(
                session.exec(
                    select(JobAttempt)
                    .where(JobAttempt.job_id == job_id)
                    .order_by(JobAttempt.attempt_number.asc())
                )
            )
            return JobView(
                id=job.id,  # type: ignore[arg-type]
                portal_id=job.portal_id,
                payer_id=job.payer_id,
                action=job.action.value,
                status=job.status.value,
                attempts=job.attempts,
                last_error=job.last_error,
                attempts_log=[
                    {
                        "attempt_number": a.attempt_number,
                        "verdict": a.validator_verdict,
                        "confidence": a.validator_confidence,
                        "rationale": a.validator_rationale,
                        "error": a.error,
                    }
                    for a in attempts
                ],
            )

    @app.post("/jobs/{job_id}/run", response_model=RunJobResponse)
    def run_job(job_id: int) -> RunJobResponse:
        # Simple demo helper: pull *any* ready job (we don't promise it's
        # the requested one; production wouldn't expose this endpoint).
        outcome = process_one_job(
            engine=app.state.engine,
            harness=app.state.harness,
            vault=app.state.vault,
            llm=app.state.llm,
        )
        if outcome is None:
            return RunJobResponse(
                ran=False, job_id=None, status=None, verdict=None,
                confidence=None, rationale=None, trust_event=None,
            )
        return RunJobResponse(
            ran=True,
            job_id=outcome.job_id,
            status=outcome.status.value,
            verdict=outcome.validation.verdict.value if outcome.validation else None,
            confidence=outcome.validation.confidence if outcome.validation else None,
            rationale=outcome.validation.rationale if outcome.validation else None,
            trust_event=outcome.trust_event.value if outcome.trust_event else None,
        )

    @app.get("/portals/{portal_id}/health", response_model=PortalHealthView)
    def portal_health(portal_id: str) -> PortalHealthView:
        with session_scope(app.state.engine) as session:
            portal = session.get(Portal, portal_id)
            if portal is None:
                raise HTTPException(404, "portal not found")
            health = compute_portal_health(session, portal_id=portal_id)
        interval = scrape_interval(trust_score=0.5, portal_health=health)
        return PortalHealthView(
            portal_id=health.portal_id,
            success_rate=health.success_rate,
            silent_fail_rate=health.silent_fail_rate,
            sample_size=health.sample_size,
            is_flaky=health.is_flaky,
            recommended_interval_for_trust_0_5=interval.total_seconds(),
        )

    @app.get("/payers/{payer_id}/scrape-interval")
    def payer_scrape_interval(payer_id: str, portal_id: str | None = None) -> dict[str, Any]:
        """Demo endpoint: shows how trust score drives cadence."""
        with session_scope(app.state.engine) as session:
            trust = TrustEngine(session)
            score = trust.get_trust(payer_id)
            health: PortalHealth | None = None
            if portal_id:
                health = compute_portal_health(session, portal_id=portal_id)
        interval = scrape_interval(trust_score=score, portal_health=health)
        return {
            "payer_id": payer_id,
            "trust_score": round(score, 3),
            "portal_id": portal_id,
            "interval_seconds": interval.total_seconds(),
            "interval_human": _human_interval(interval),
        }

    return app


def _human_interval(td: timedelta) -> str:
    secs = td.total_seconds()
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


# Module-level app for `uvicorn browser_orchestration.server:app`.
app = create_app()
