"""Read-only JSON API powering the Next.js ops dashboard.

Reads the same SQLite DB that all three verticals write to. Exposes
per-payer aggregates that fuse signals across voice / browser / cash_recon.

Endpoints:
    GET /payers
    GET /payers/{payer_id}
    GET /payers/{payer_id}/timeline?since_days=30
    GET /payers/{payer_id}/trust-history?since_days=30
    GET /kpis
    GET /events/recent?limit=50
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import select

from browser_orchestration.models import ExtractionRecord, Job, JobStatus, PortalHealthEvent
from cash_recon.models import (
    Match,
    MatchOutcome,
    PayerAlias,
    WireStatus,
    WireTransfer,
)
from cash_recon.service import threshold_for_trust
from shared.db import init_schema, make_engine, session_scope
from shared.models import Payer, TrustEventRecord, TrustEventType
from shared.trust_engine import TrustEngine
from voice_agent.memory_models import Call, Objection, Promise


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# ---- App factory ----------------------------------------------------------


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine()
        init_schema(engine)
        app.state.engine = engine
        yield

    app = FastAPI(lifespan=lifespan, title="Iridium ops API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/payers")
    def list_payers() -> dict[str, Any]:
        with session_scope(app.state.engine) as s:
            payers = list(s.exec(select(Payer).order_by(Payer.payer_id)))
            trust = TrustEngine(s)
            rows = []
            for p in payers:
                score = trust.get_trust(p.payer_id)
                last_event = list(
                    s.exec(
                        select(TrustEventRecord)
                        .where(TrustEventRecord.payer_id == p.payer_id)
                        .order_by(TrustEventRecord.occurred_at.desc())
                        .limit(1)
                    )
                )
                rows.append(
                    {
                        "payer_id": p.payer_id,
                        "name": p.name,
                        "trust_score": round(score, 3),
                        "auto_match_threshold": round(threshold_for_trust(score), 3),
                        "last_event_at": _iso(
                            last_event[0].occurred_at if last_event else None
                        ),
                        "last_event_type": (
                            last_event[0].event_type.value if last_event else None
                        ),
                    }
                )
        rows.sort(key=lambda r: r["trust_score"])  # lowest trust first — needs attention
        return {"count": len(rows), "payers": rows}

    @app.get("/payers/{payer_id}")
    def get_payer(payer_id: str) -> dict[str, Any]:
        with session_scope(app.state.engine) as s:
            p = s.get(Payer, payer_id)
            if p is None:
                raise HTTPException(404, "payer not found")
            trust = TrustEngine(s)
            score = trust.get_trust(payer_id)

            calls = list(
                s.exec(
                    select(Call)
                    .where(Call.payer_id == payer_id)
                    .order_by(Call.occurred_at.desc())
                )
            )
            promises = list(
                s.exec(
                    select(Promise)
                    .where(Promise.payer_id == payer_id)
                    .order_by(Promise.created_at.desc())
                )
            )
            objections = list(
                s.exec(
                    select(Objection)
                    .where(Objection.payer_id == payer_id)
                    .order_by(Objection.occurred_at.desc())
                    .limit(20)
                )
            )

            jobs = list(
                s.exec(
                    select(Job)
                    .where(Job.payer_id == payer_id)
                    .order_by(Job.enqueued_at.desc())
                    .limit(40)
                )
            )
            extractions = list(
                s.exec(
                    select(ExtractionRecord)
                    .where(ExtractionRecord.payer_id == payer_id)
                    .order_by(ExtractionRecord.extracted_at.desc())
                    .limit(20)
                )
            )

            aliases = list(
                s.exec(select(PayerAlias).where(PayerAlias.payer_id == payer_id))
            )
            alias_strs = [a.alias for a in aliases]

            wires = list(
                s.exec(
                    select(WireTransfer)
                    .where(WireTransfer.resolved_payer_id == payer_id)
                    .order_by(WireTransfer.created_at.desc())
                    .limit(40)
                )
            )
            matches = list(
                s.exec(
                    select(Match).order_by(Match.decided_at.desc())
                )
            )
            match_by_wire: dict[str, Match] = {}
            for m in matches:
                match_by_wire.setdefault(m.wire_id, m)

            # Aggregates.
            promises_kept = sum(1 for pr in promises if pr.kept is True)
            promises_broken = sum(1 for pr in promises if pr.kept is False)
            silent_fails = sum(1 for j in jobs if j.status == JobStatus.SILENT_FAIL)
            auto_matches = sum(
                1 for w in wires if w.status == WireStatus.AUTO_MATCHED
            )
            under_review = sum(
                1 for w in wires if w.status == WireStatus.UNDER_REVIEW
            )

            return {
                "payer_id": p.payer_id,
                "name": p.name,
                "trust_score": round(score, 3),
                "auto_match_threshold": round(threshold_for_trust(score), 3),
                "aliases": alias_strs,
                "kpis": {
                    "calls": len(calls),
                    "promises_kept": promises_kept,
                    "promises_broken": promises_broken,
                    "browser_jobs": len(jobs),
                    "silent_fails": silent_fails,
                    "wires_auto_matched": auto_matches,
                    "wires_under_review": under_review,
                },
                "calls": [
                    {
                        "id": c.id,
                        "occurred_at": _iso(c.occurred_at),
                        "summary": c.summary,
                        "outcome": c.outcome.value,
                        "duration_sec": c.duration_sec,
                        "contact_name": c.contact_name,
                        "invoice_id": c.invoice_id,
                        "final_phase": c.final_phase,
                        "final_tone": c.final_tone,
                    }
                    for c in calls[:20]
                ],
                "promises": [
                    {
                        "id": pr.id,
                        "promised_date": _iso(pr.promised_date),
                        "promised_amount": pr.promised_amount,
                        "invoice_id": pr.invoice_id,
                        "kept": pr.kept,
                    }
                    for pr in promises[:20]
                ],
                "objections": [
                    {
                        "kind": o.kind, "text": o.text, "occurred_at": _iso(o.occurred_at)
                    }
                    for o in objections
                ],
                "jobs": [
                    {
                        "id": j.id,
                        "portal_id": j.portal_id,
                        "action": j.action.value,
                        "status": j.status.value,
                        "attempts": j.attempts,
                        "enqueued_at": _iso(j.enqueued_at),
                        "finished_at": _iso(j.finished_at),
                        "last_error": j.last_error,
                    }
                    for j in jobs
                ],
                "extractions": [
                    {
                        "id": e.id,
                        "portal_id": e.portal_id,
                        "invoice_count": e.invoice_count,
                        "extracted_at": _iso(e.extracted_at),
                    }
                    for e in extractions
                ],
                "wires": [
                    {
                        "wire_id": w.wire_id,
                        "amount": w.amount,
                        "received_on": str(w.received_on),
                        "memo": w.memo,
                        "sender_name": w.sender_name,
                        "status": w.status.value,
                        "match": (
                            {
                                "invoice_ids": match_by_wire[w.wire_id].invoice_ids,
                                "outcome": match_by_wire[w.wire_id].outcome.value,
                                "confidence": match_by_wire[w.wire_id].confidence,
                            }
                            if w.wire_id in match_by_wire
                            else None
                        ),
                    }
                    for w in wires
                ],
            }

    @app.get("/payers/{payer_id}/timeline")
    def timeline(payer_id: str, since_days: int = 30) -> dict[str, Any]:
        """Unified event stream across all three verticals.

        Each event has: ts, vertical, kind, summary, delta (optional).
        Events are sorted reverse-chronologically.
        """
        cutoff = _utcnow() - timedelta(days=since_days)
        events: list[dict[str, Any]] = []
        with session_scope(app.state.engine) as s:
            if s.get(Payer, payer_id) is None:
                raise HTTPException(404, "payer not found")

            # Trust events (cross-vertical signal).
            trust_events = list(
                s.exec(
                    select(TrustEventRecord)
                    .where(TrustEventRecord.payer_id == payer_id)
                    .where(TrustEventRecord.occurred_at >= cutoff)
                )
            )
            for e in trust_events:
                events.append(
                    {
                        "ts": _iso(e.occurred_at),
                        "vertical": _vertical_for_event(e.event_type),
                        "kind": e.event_type.value,
                        "summary": _human_event(e.event_type, e.delta),
                        "delta": e.delta,
                        "source": e.source,
                    }
                )

            # Voice calls.
            calls = list(
                s.exec(
                    select(Call)
                    .where(Call.payer_id == payer_id)
                    .where(Call.occurred_at >= cutoff)
                )
            )
            for c in calls:
                events.append(
                    {
                        "ts": _iso(c.occurred_at),
                        "vertical": "voice",
                        "kind": f"call.{c.outcome.value}",
                        "summary": c.summary[:140],
                        "delta": None,
                        "source": f"call#{c.id}",
                    }
                )

            # Browser jobs (only finished ones).
            jobs = list(
                s.exec(
                    select(Job)
                    .where(Job.payer_id == payer_id)
                    .where(Job.enqueued_at >= cutoff)
                )
            )
            for j in jobs:
                events.append(
                    {
                        "ts": _iso(j.finished_at or j.enqueued_at),
                        "vertical": "browser",
                        "kind": f"job.{j.status.value}",
                        "summary": f"{j.action.value} on {j.portal_id}"
                        + (f" — {j.last_error}" if j.last_error else ""),
                        "delta": None,
                        "source": f"job#{j.id}",
                    }
                )

            # Wires & matches.
            wires = list(
                s.exec(
                    select(WireTransfer)
                    .where(WireTransfer.resolved_payer_id == payer_id)
                    .where(WireTransfer.created_at >= cutoff)
                )
            )
            for w in wires:
                events.append(
                    {
                        "ts": _iso(w.created_at),
                        "vertical": "recon",
                        "kind": f"wire.{w.status.value}",
                        "summary": f"${w.amount:.2f} from {w.sender_name} — {w.memo}",
                        "delta": None,
                        "source": f"wire#{w.wire_id}",
                    }
                )

        events.sort(key=lambda e: e["ts"] or "", reverse=True)
        return {"payer_id": payer_id, "since_days": since_days, "events": events}

    @app.get("/payers/{payer_id}/trust-history")
    def trust_history(payer_id: str, since_days: int = 60) -> dict[str, Any]:
        """Chronological trust events with running raw_score so the
        frontend can plot the trust line."""
        cutoff = _utcnow() - timedelta(days=since_days)
        with session_scope(app.state.engine) as s:
            if s.get(Payer, payer_id) is None:
                raise HTTPException(404, "payer not found")
            events = list(
                s.exec(
                    select(TrustEventRecord)
                    .where(TrustEventRecord.payer_id == payer_id)
                    .where(TrustEventRecord.occurred_at >= cutoff)
                    .order_by(TrustEventRecord.occurred_at.asc())
                )
            )
            running = 0.5
            points = [{"ts": _iso(cutoff), "score": 0.5, "kind": "baseline", "delta": 0.0}]
            for e in events:
                running = max(0.0, min(1.0, running + e.delta))
                points.append(
                    {
                        "ts": _iso(e.occurred_at),
                        "score": round(running, 3),
                        "kind": e.event_type.value,
                        "delta": e.delta,
                        "source": e.source,
                    }
                )
            # Final point at "now" so the line extends to today.
            points.append(
                {"ts": _iso(_utcnow()), "score": round(running, 3), "kind": "now", "delta": 0.0}
            )
        return {"payer_id": payer_id, "since_days": since_days, "points": points}

    @app.get("/kpis")
    def kpis() -> dict[str, Any]:
        with session_scope(app.state.engine) as s:
            payers = list(s.exec(select(Payer)))
            trust = TrustEngine(s)
            scores = [trust.get_trust(p.payer_id) for p in payers]
            avg_trust = sum(scores) / len(scores) if scores else 0.5

            calls_count = len(list(s.exec(select(Call))))
            jobs = list(s.exec(select(Job)))
            silent_fails = sum(1 for j in jobs if j.status == JobStatus.SILENT_FAIL)
            successes = sum(1 for j in jobs if j.status == JobStatus.SUCCEEDED)

            wires = list(s.exec(select(WireTransfer)))
            auto_matched = sum(
                1 for w in wires if w.status == WireStatus.AUTO_MATCHED
            )
            under_review = sum(
                1 for w in wires if w.status == WireStatus.UNDER_REVIEW
            )
            unmatched = sum(1 for w in wires if w.status == WireStatus.UNMATCHED)

            matches = list(s.exec(select(Match)))
            human_overrides = sum(
                1 for m in matches if m.outcome == MatchOutcome.HUMAN_OVERRIDE
            )

        auto_match_rate = (auto_matched / len(wires)) if wires else 0.0
        return {
            "fleet": {
                "payers": len(payers),
                "avg_trust_score": round(avg_trust, 3),
            },
            "voice": {"calls": calls_count},
            "browser": {
                "jobs_total": len(jobs),
                "succeeded": successes,
                "silent_fails": silent_fails,
                "silent_fail_rate": round(silent_fails / len(jobs), 3) if jobs else 0.0,
            },
            "recon": {
                "wires_total": len(wires),
                "auto_matched": auto_matched,
                "under_review": under_review,
                "unmatched": unmatched,
                "auto_match_rate": round(auto_match_rate, 3),
                "human_overrides": human_overrides,
            },
        }

    @app.get("/events/recent")
    def recent(limit: int = 50) -> dict[str, Any]:
        """Cross-payer firehose of trust events (descending)."""
        with session_scope(app.state.engine) as s:
            events = list(
                s.exec(
                    select(TrustEventRecord)
                    .order_by(TrustEventRecord.occurred_at.desc())
                    .limit(limit)
                )
            )
            return {
                "count": len(events),
                "events": [
                    {
                        "id": e.id,
                        "payer_id": e.payer_id,
                        "kind": e.event_type.value,
                        "delta": e.delta,
                        "ts": _iso(e.occurred_at),
                        "source": e.source,
                        "vertical": _vertical_for_event(e.event_type),
                    }
                    for e in events
                ],
            }

    return app


def _vertical_for_event(t: TrustEventType) -> str:
    if t in (
        TrustEventType.PROMISE_KEPT, TrustEventType.PROMISE_BROKEN,
        TrustEventType.PARTIAL_RECEIVED, TrustEventType.CALL_HOSTILE,
    ):
        return "voice"
    if t in (
        TrustEventType.SILENT_FAIL_CAUGHT, TrustEventType.CLEAN_EXTRACTION_STREAK,
    ):
        return "browser"
    if t in (TrustEventType.AUTO_MATCHED, TrustEventType.HUMAN_OVERRIDE):
        return "recon"
    return "shared"


_HUMAN: dict[TrustEventType, str] = {
    TrustEventType.PROMISE_KEPT: "Promise kept",
    TrustEventType.PROMISE_BROKEN: "Promise broken",
    TrustEventType.PARTIAL_RECEIVED: "Partial payment received",
    TrustEventType.AUTO_MATCHED: "Wire auto-matched",
    TrustEventType.HUMAN_OVERRIDE: "Reviewer overrode auto-match",
    TrustEventType.SILENT_FAIL_CAUGHT: "Silent failure caught",
    TrustEventType.CLEAN_EXTRACTION_STREAK: "Clean extraction streak",
    TrustEventType.CALL_HOSTILE: "Hostile call",
}


def _human_event(t: TrustEventType, delta: float) -> str:
    return f"{_HUMAN.get(t, t.value)} ({delta:+.2f})"


app = create_app()
