"""Reconciliation service: ingest a wire and decide what happens to it.

End-to-end pipeline for one wire:

    1. Persist wire (status=pending).
    2. Entity resolution (memo, sender_name) -> payer_id (or None).
    3. Candidate generation: open invoices for that payer within
       amount/date windows. Cross-payer fallback if ER failed.
    4. Score each candidate with the trained ranker.
    5. If best calibrated_prob >= trust-aware threshold for that payer:
           AUTO_MATCH -> emit AUTO_MATCHED trust event.
       Else, run the bundler (subset-sum) on the same payer's invoices.
       If a bundle has |delta| < tolerance, score it the same way.
    6. Otherwise: status=under_review (or unmatched, if no candidates).

Trust integration:
    - Higher trust => lower auto-match threshold (we trust their wires
      to be clean, so we tolerate slightly weaker model evidence).
    - On confirm:    emit AUTO_MATCHED  (+0.01)
    - On override:   emit HUMAN_OVERRIDE (-0.02) — auto-match was wrong.
    - On reject:     no trust event (it wasn't theirs at all).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlmodel import Session, select

from cash_recon import entity_resolution as er
from cash_recon.bundler import find_bundles
from cash_recon.features import (
    InvoiceView,
    WireView,
    candidates_for_wire,
    featurize,
    featurize_to_dict,
)
from cash_recon.models import (
    Invoice,
    InvoiceStatus,
    Match,
    MatchCandidate,
    MatchOutcome,
    ReviewDecision,
    WireStatus,
    WireTransfer,
)
from cash_recon.ranker import TrainedRanker
from shared.models import TrustEventType
from shared.trust_engine import TrustEngine

# Trust-aware decision boundaries. Real systems would tune these on
# replayed history; reasonable defaults are fine for the demo.
BASE_AUTO_MATCH_THRESHOLD = 0.92
TRUST_BAND_LOW = 0.30   # below this trust score we require strong evidence
TRUST_BAND_HIGH = 0.75  # above this trust score we tolerate weaker evidence


def threshold_for_trust(trust_score: float) -> float:
    """Map trust [0,1] -> calibrated probability threshold for auto-match.

    Linear interpolation between two thresholds:
        trust=0.30 -> 0.97 (strict; we don't auto-post wires from low-trust payers)
        trust=0.75 -> 0.88 (lenient; clean history earns easier auto-match)
    """
    if trust_score <= TRUST_BAND_LOW:
        return 0.97
    if trust_score >= TRUST_BAND_HIGH:
        return 0.88
    span = TRUST_BAND_HIGH - TRUST_BAND_LOW
    pos = (trust_score - TRUST_BAND_LOW) / span
    return 0.97 - 0.09 * pos


@dataclass
class IngestResult:
    wire_id: str
    final_status: WireStatus
    resolved_payer_id: str | None
    best_candidate_id: int | None
    best_calibrated_prob: float
    threshold_used: float
    candidate_count: int
    notes: str


def _wire_view(w: WireTransfer) -> WireView:
    return WireView(
        wire_id=w.wire_id,
        amount=w.amount,
        received_on=w.received_on,
        memo=w.memo,
        sender_name=w.sender_name,
    )


def _inv_view(i: Invoice, payer_name: str) -> InvoiceView:
    return InvoiceView(
        invoice_id=i.invoice_id,
        payer_id=i.payer_id,
        payer_name=payer_name,
        amount=i.amount,
        due_date=i.due_date,
    )


def _ensure_wire_id(wire_id: str | None) -> str:
    return wire_id or "WIRE-" + uuid.uuid4().hex[:10].upper()


def _apply_payment_to_invoices(
    session: Session,
    *,
    invoice_ids: list[str],
    wire_amount: float,
    is_bundle: bool,
) -> None:
    """Mark each invoice paid (full) or partial. Distributes the wire
    amount across invoices: bundles credit each invoice's full amount;
    single-invoice matches credit the wire amount and clamp to invoice."""
    invs = list(
        session.exec(select(Invoice).where(Invoice.invoice_id.in_(invoice_ids)))
    )
    for inv in invs:
        credit = inv.amount if is_bundle else wire_amount
        new_paid = round(min(inv.amount_paid + credit, inv.amount), 2)
        inv.amount_paid = new_paid
        inv.status = (
            InvoiceStatus.PAID if abs(new_paid - inv.amount) < 0.01
            else InvoiceStatus.PARTIAL
        )
        session.add(inv)


def ingest_wire(
    session: Session,
    *,
    ranker: TrainedRanker,
    wire: WireTransfer,
    trust_engine: TrustEngine | None = None,
) -> IngestResult:
    """Process one wire end-to-end and return what happened.

    The wire is added to the session with a fresh wire_id if not set;
    candidates and the eventual Match are written transactionally.
    """
    wire.wire_id = _ensure_wire_id(wire.wire_id)
    if not session.get(WireTransfer, wire.wire_id):
        wire.status = WireStatus.PENDING
        session.add(wire)
        session.commit()
        session.refresh(wire)

    # 1. Entity resolution.
    res = er.resolve(session, memo=wire.memo, sender_name=wire.sender_name)
    wire.resolved_payer_id = res.payer_id
    session.add(wire)
    session.commit()

    # 2. Pull open invoices for this payer (or all open invoices as fallback).
    open_invoices = list(
        session.exec(
            select(Invoice).where(Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIAL]))
        )
    )

    # Lookup payer_name once. Falls back to payer_id when no Payer row.
    from shared.models import Payer  # local import avoids circulars in tests
    payer_names: dict[str, str] = {p.payer_id: p.name for p in session.exec(select(Payer))}

    inv_views = [_inv_view(i, payer_names.get(i.payer_id, i.payer_id)) for i in open_invoices]
    wv = _wire_view(wire)

    # 3. Candidate generation. If ER gave us a payer, scope to that payer.
    cand_invoices = candidates_for_wire(wv, inv_views, payer_id=res.payer_id)

    # 4. Score each candidate.
    written_candidates: list[tuple[int, float]] = []
    best_prob = 0.0
    best_id: int | None = None

    for inv in cand_invoices:
        feats = featurize(wv, inv)
        raw, cal = ranker.predict_calibrated(feats)
        mc = MatchCandidate(
            wire_id=wire.wire_id,
            invoice_ids=inv.invoice_id,
            is_bundle=False,
            raw_score=raw,
            calibrated_prob=cal,
            features_json=json.dumps(featurize_to_dict(wv, inv)),
        )
        session.add(mc)
        session.flush()
        written_candidates.append((mc.id, cal))
        if cal > best_prob:
            best_prob = cal
            best_id = mc.id

    # 5. Trust-aware threshold.
    trust = 0.5
    if trust_engine is not None and res.payer_id:
        trust = trust_engine.get_trust(res.payer_id)
    threshold = threshold_for_trust(trust)

    notes_parts: list[str] = [f"er={res.method}({res.score:.0f})"]

    final_status = WireStatus.UNMATCHED
    if best_prob >= threshold:
        # Single-invoice auto-match.
        cand = session.get(MatchCandidate, best_id)
        Match_ = Match(
            wire_id=wire.wire_id,
            invoice_ids=cand.invoice_ids,
            is_bundle=False,
            confidence=best_prob,
            outcome=MatchOutcome.AUTO_POSTED,
            reviewer="auto",
        )
        session.add(Match_)
        _apply_payment_to_invoices(
            session, invoice_ids=cand.invoice_ids.split(","),
            wire_amount=wire.amount, is_bundle=False,
        )
        wire.status = WireStatus.AUTO_MATCHED
        session.add(wire)
        session.commit()
        if trust_engine is not None and res.payer_id:
            trust_engine.update_trust(
                res.payer_id, TrustEventType.AUTO_MATCHED,
                source=f"recon.auto_match.{wire.wire_id}",
            )
        notes_parts.append(f"auto_match cal={best_prob:.3f} >= thr={threshold:.3f}")
        final_status = WireStatus.AUTO_MATCHED
    else:
        # 6. Try bundling.
        bundle_pool = [
            (i.invoice_id, i.amount) for i in open_invoices
            if (res.payer_id is None or i.payer_id == res.payer_id)
        ]
        bundles = find_bundles(wire.amount, bundle_pool)
        if bundles:
            # Score the best bundle by averaging features over its invoices.
            best_bundle = bundles[0]
            bundle_invs = [i for i in inv_views if i.invoice_id in best_bundle.invoice_ids]
            if bundle_invs:
                # Use the worst-of-N feature row to be conservative.
                per_inv_probs: list[tuple[InvoiceView, float, float]] = []
                for biv in bundle_invs:
                    feats = featurize(wv, biv)
                    raw, cal = ranker.predict_calibrated(feats)
                    per_inv_probs.append((biv, raw, cal))
                # Confidence for a bundle is the min calibrated prob across its
                # members — every invoice has to fit, not just one.
                bundle_prob = min(p[2] for p in per_inv_probs)
                bundle_features = {
                    "bundle_size": len(bundle_invs),
                    "bundle_delta": best_bundle.delta,
                    "bundle_min_prob": bundle_prob,
                }
                mc = MatchCandidate(
                    wire_id=wire.wire_id,
                    invoice_ids=",".join(best_bundle.invoice_ids),
                    is_bundle=True,
                    raw_score=bundle_prob,
                    calibrated_prob=bundle_prob,
                    features_json=json.dumps(bundle_features),
                )
                session.add(mc)
                session.flush()
                if bundle_prob >= threshold:
                    Match_ = Match(
                        wire_id=wire.wire_id,
                        invoice_ids=mc.invoice_ids,
                        is_bundle=True,
                        confidence=bundle_prob,
                        outcome=MatchOutcome.AUTO_POSTED,
                        reviewer="auto",
                    )
                    session.add(Match_)
                    _apply_payment_to_invoices(
                        session, invoice_ids=best_bundle.invoice_ids,
                        wire_amount=wire.amount, is_bundle=True,
                    )
                    wire.status = WireStatus.AUTO_MATCHED
                    session.add(wire)
                    session.commit()
                    if trust_engine is not None and res.payer_id:
                        trust_engine.update_trust(
                            res.payer_id, TrustEventType.AUTO_MATCHED,
                            source=f"recon.auto_bundle.{wire.wire_id}",
                        )
                    notes_parts.append(
                        f"auto_bundle size={len(bundle_invs)} cal={bundle_prob:.3f}"
                    )
                    final_status = WireStatus.AUTO_MATCHED
                    best_prob = bundle_prob
                    best_id = mc.id
                else:
                    # Bundle is plausible but not above auto threshold.
                    wire.status = WireStatus.UNDER_REVIEW
                    session.add(wire)
                    session.commit()
                    notes_parts.append(
                        f"review (bundle cal={bundle_prob:.3f} < thr={threshold:.3f})"
                    )
                    final_status = WireStatus.UNDER_REVIEW
                    best_prob = bundle_prob
                    best_id = mc.id

        if final_status not in (WireStatus.AUTO_MATCHED, WireStatus.UNDER_REVIEW):
            if written_candidates:
                wire.status = WireStatus.UNDER_REVIEW
                session.add(wire)
                session.commit()
                final_status = WireStatus.UNDER_REVIEW
                notes_parts.append(
                    f"review (best cal={best_prob:.3f} < thr={threshold:.3f})"
                )
            else:
                wire.status = WireStatus.UNMATCHED
                session.add(wire)
                session.commit()
                final_status = WireStatus.UNMATCHED
                notes_parts.append("no_candidates")

    return IngestResult(
        wire_id=wire.wire_id,
        final_status=final_status,
        resolved_payer_id=res.payer_id,
        best_candidate_id=best_id,
        best_calibrated_prob=best_prob,
        threshold_used=threshold,
        candidate_count=len(written_candidates),
        notes=" | ".join(notes_parts),
    )


def confirm_match(
    session: Session,
    *,
    wire_id: str,
    candidate_id: int,
    reviewer: str,
    trust_engine: TrustEngine | None = None,
    note: str = "",
) -> Match:
    """Human accepts a candidate. Used to confirm both auto-posts and
    review-queue items. Marks invoices PAID/PARTIAL and emits trust event."""
    wire = session.get(WireTransfer, wire_id)
    if wire is None:
        raise ValueError(f"unknown wire_id {wire_id}")
    cand = session.get(MatchCandidate, candidate_id)
    if cand is None or cand.wire_id != wire_id:
        raise ValueError(f"candidate {candidate_id} not for wire {wire_id}")

    # If wire was previously auto-matched, this is a confirm-of-auto;
    # otherwise it's a fresh human-confirmed match.
    was_auto = wire.status == WireStatus.AUTO_MATCHED
    outcome = MatchOutcome.HUMAN_CONFIRMED if not was_auto else MatchOutcome.AUTO_POSTED

    match = Match(
        wire_id=wire_id,
        invoice_ids=cand.invoice_ids,
        is_bundle=cand.is_bundle,
        confidence=cand.calibrated_prob,
        outcome=outcome,
        reviewer=reviewer,
    )
    session.add(match)

    invoice_ids = cand.invoice_ids.split(",")
    if not was_auto:
        # Auto-matched wires already credited at ingest time.
        _apply_payment_to_invoices(
            session, invoice_ids=invoice_ids,
            wire_amount=wire.amount, is_bundle=cand.is_bundle,
        )

    wire.status = WireStatus.MATCHED
    session.add(wire)

    session.add(
        ReviewDecision(
            wire_id=wire_id, candidate_id=candidate_id,
            action="confirm", note=note, reviewer=reviewer,
        )
    )
    session.commit()
    session.refresh(match)

    if trust_engine is not None and wire.resolved_payer_id and not was_auto:
        # Only emit on fresh human confirmations; auto-matches already
        # emitted at ingest time.
        trust_engine.update_trust(
            wire.resolved_payer_id,
            TrustEventType.AUTO_MATCHED,
            source=f"recon.human_confirm.{wire_id}",
        )
    return match


def override_match(
    session: Session,
    *,
    wire_id: str,
    new_invoice_ids: list[str],
    reviewer: str,
    trust_engine: TrustEngine | None = None,
    note: str = "",
) -> Match:
    """Human picks a different (set of) invoice(s) than what the model
    suggested. Penalises trust because the model was wrong, even if the
    human had to clean up the result."""
    wire = session.get(WireTransfer, wire_id)
    if wire is None:
        raise ValueError(f"unknown wire_id {wire_id}")

    is_bundle = len(new_invoice_ids) > 1
    match = Match(
        wire_id=wire_id,
        invoice_ids=",".join(new_invoice_ids),
        is_bundle=is_bundle,
        confidence=0.0,
        outcome=MatchOutcome.HUMAN_OVERRIDE,
        reviewer=reviewer,
    )
    session.add(match)
    wire.status = WireStatus.MATCHED
    session.add(wire)
    session.add(
        ReviewDecision(
            wire_id=wire_id, candidate_id=None,
            action="override", note=note, reviewer=reviewer,
        )
    )
    session.commit()
    session.refresh(match)
    if trust_engine is not None and wire.resolved_payer_id:
        trust_engine.update_trust(
            wire.resolved_payer_id,
            TrustEventType.HUMAN_OVERRIDE,
            source=f"recon.override.{wire_id}",
        )
    return match


def reject_wire(
    session: Session,
    *,
    wire_id: str,
    reviewer: str,
    note: str = "",
) -> WireTransfer:
    """Reviewer says this wire isn't ours. No trust event."""
    wire = session.get(WireTransfer, wire_id)
    if wire is None:
        raise ValueError(f"unknown wire_id {wire_id}")
    wire.status = WireStatus.REJECTED
    session.add(wire)
    session.add(
        ReviewDecision(
            wire_id=wire_id, candidate_id=None,
            action="reject", note=note, reviewer=reviewer,
        )
    )
    session.commit()
    session.refresh(wire)
    return wire
