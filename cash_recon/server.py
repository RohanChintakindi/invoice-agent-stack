"""FastAPI server for the cash reconciliation vertical.

Endpoints:
  GET  /health
  POST /wires                       ingest a wire, returns the IngestResult
  GET  /wires/{wire_id}             show wire + candidates + final match
  GET  /reviews                     list wires currently in UNDER_REVIEW
  POST /reviews/{wire_id}/confirm   confirm a candidate (auto-post or human)
  POST /reviews/{wire_id}/override  pick different invoice ids
  POST /reviews/{wire_id}/reject    not our payment
  GET  /payers/{payer_id}/threshold  show trust-aware threshold for ingest

The trained model is loaded on first ingest (or trained on the fly if
no artifacts exist).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from cash_recon import service as recon_service
from cash_recon.models import (
    Match,
    MatchCandidate,
    WireStatus,
    WireTransfer,
)
from cash_recon.ranker import DEFAULT_ARTIFACT_DIR, TrainedRanker, load, train
from cash_recon.service import threshold_for_trust
from shared.db import init_schema, make_engine, session_scope
from shared.trust_engine import TrustEngine


class IngestWireRequest(BaseModel):
    wire_id: str | None = None
    amount: float
    currency: str = "USD"
    received_on: date
    memo: str = ""
    sender_name: str = ""
    bank_ref: str | None = None


class IngestWireResponse(BaseModel):
    wire_id: str
    final_status: str
    resolved_payer_id: str | None
    best_candidate_id: int | None
    best_calibrated_prob: float
    threshold_used: float
    candidate_count: int
    notes: str


class WireDetailView(BaseModel):
    wire_id: str
    amount: float
    received_on: date
    memo: str
    sender_name: str
    status: str
    resolved_payer_id: str | None
    candidates: list[dict[str, Any]]
    matches: list[dict[str, Any]]


class ConfirmRequest(BaseModel):
    candidate_id: int
    reviewer: str
    note: str = ""


class OverrideRequest(BaseModel):
    invoice_ids: list[str]
    reviewer: str
    note: str = ""


class RejectRequest(BaseModel):
    reviewer: str
    note: str = ""


def _load_or_train(artifact_dir: Path) -> TrainedRanker:
    if (artifact_dir / "ranker.json").exists():
        return load(artifact_dir)
    r = train(seed=42)
    r.save(artifact_dir)
    return r


def create_app(*, ranker: TrainedRanker | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine()
        init_schema(engine)
        app.state.engine = engine
        app.state.ranker = ranker if ranker is not None else _load_or_train(DEFAULT_ARTIFACT_DIR)
        yield

    app = FastAPI(lifespan=lifespan, title="Iridium cash reconciliation")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/wires", response_model=IngestWireResponse)
    def ingest(req: IngestWireRequest) -> IngestWireResponse:
        wire = WireTransfer(
            wire_id=req.wire_id or "",
            amount=req.amount,
            currency=req.currency,
            received_on=req.received_on,
            memo=req.memo,
            sender_name=req.sender_name,
            bank_ref=req.bank_ref,
        )
        with session_scope(app.state.engine) as session:
            trust_engine = TrustEngine(session)
            result = recon_service.ingest_wire(
                session, ranker=app.state.ranker, wire=wire,
                trust_engine=trust_engine,
            )
        return IngestWireResponse(
            wire_id=result.wire_id,
            final_status=result.final_status.value,
            resolved_payer_id=result.resolved_payer_id,
            best_candidate_id=result.best_candidate_id,
            best_calibrated_prob=result.best_calibrated_prob,
            threshold_used=result.threshold_used,
            candidate_count=result.candidate_count,
            notes=result.notes,
        )

    @app.get("/wires/{wire_id}", response_model=WireDetailView)
    def get_wire(wire_id: str) -> WireDetailView:
        with session_scope(app.state.engine) as session:
            wire = session.get(WireTransfer, wire_id)
            if wire is None:
                raise HTTPException(404, "wire not found")
            cands = list(
                session.exec(
                    select(MatchCandidate)
                    .where(MatchCandidate.wire_id == wire_id)
                    .order_by(MatchCandidate.calibrated_prob.desc())
                )
            )
            matches = list(
                session.exec(
                    select(Match).where(Match.wire_id == wire_id).order_by(Match.decided_at.desc())
                )
            )
            return WireDetailView(
                wire_id=wire.wire_id,
                amount=wire.amount,
                received_on=wire.received_on,
                memo=wire.memo,
                sender_name=wire.sender_name,
                status=wire.status.value,
                resolved_payer_id=wire.resolved_payer_id,
                candidates=[
                    {
                        "id": c.id,
                        "invoice_ids": c.invoice_ids,
                        "is_bundle": c.is_bundle,
                        "raw_score": c.raw_score,
                        "calibrated_prob": c.calibrated_prob,
                        "features": json.loads(c.features_json),
                    }
                    for c in cands
                ],
                matches=[
                    {
                        "id": m.id,
                        "invoice_ids": m.invoice_ids,
                        "outcome": m.outcome.value,
                        "confidence": m.confidence,
                        "reviewer": m.reviewer,
                    }
                    for m in matches
                ],
            )

    @app.get("/reviews")
    def list_reviews() -> dict[str, Any]:
        with session_scope(app.state.engine) as session:
            wires = list(
                session.exec(
                    select(WireTransfer).where(WireTransfer.status == WireStatus.UNDER_REVIEW)
                )
            )
            return {
                "count": len(wires),
                "wires": [
                    {
                        "wire_id": w.wire_id,
                        "amount": w.amount,
                        "received_on": str(w.received_on),
                        "memo": w.memo,
                        "sender_name": w.sender_name,
                        "resolved_payer_id": w.resolved_payer_id,
                    }
                    for w in wires
                ],
            }

    @app.post("/reviews/{wire_id}/confirm")
    def confirm(wire_id: str, req: ConfirmRequest) -> dict[str, Any]:
        with session_scope(app.state.engine) as session:
            trust_engine = TrustEngine(session)
            try:
                m = recon_service.confirm_match(
                    session, wire_id=wire_id, candidate_id=req.candidate_id,
                    reviewer=req.reviewer, trust_engine=trust_engine, note=req.note,
                )
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        return {"match_id": m.id, "outcome": m.outcome.value}

    @app.post("/reviews/{wire_id}/override")
    def override(wire_id: str, req: OverrideRequest) -> dict[str, Any]:
        with session_scope(app.state.engine) as session:
            trust_engine = TrustEngine(session)
            try:
                m = recon_service.override_match(
                    session, wire_id=wire_id, new_invoice_ids=req.invoice_ids,
                    reviewer=req.reviewer, trust_engine=trust_engine, note=req.note,
                )
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        return {"match_id": m.id, "outcome": m.outcome.value}

    @app.post("/reviews/{wire_id}/reject")
    def reject(wire_id: str, req: RejectRequest) -> dict[str, Any]:
        with session_scope(app.state.engine) as session:
            try:
                w = recon_service.reject_wire(
                    session, wire_id=wire_id, reviewer=req.reviewer, note=req.note,
                )
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        return {"wire_id": w.wire_id, "status": w.status.value}

    @app.get("/payers/{payer_id}/threshold")
    def payer_threshold(payer_id: str) -> dict[str, Any]:
        with session_scope(app.state.engine) as session:
            trust = TrustEngine(session)
            score = trust.get_trust(payer_id)
        return {
            "payer_id": payer_id,
            "trust_score": round(score, 3),
            "auto_match_threshold": round(threshold_for_trust(score), 3),
        }

    return app


# Module-level app for `uvicorn cash_recon.server:app`.
app = create_app()
