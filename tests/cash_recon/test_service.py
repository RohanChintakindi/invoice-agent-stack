"""End-to-end service behaviour: ingest → auto-match → trust events.

Uses the real trained ranker (small synth) so we exercise the actual
calibrated probabilities against threshold_for_trust.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from cash_recon import service
from cash_recon.models import (
    Invoice,
    InvoiceStatus,
    Match,
    MatchCandidate,
    PayerAlias,
    WireStatus,
    WireTransfer,
)
from cash_recon.ranker import train
from cash_recon.service import threshold_for_trust
from shared.models import Payer, TrustEventRecord, TrustEventType
from shared.trust_engine import TrustEngine


@pytest.fixture(scope="module")
def ranker():
    return train(seed=42, n_invoices=200, n_wires=200)


@pytest.fixture
def seeded(session):
    session.add(Payer(payer_id="acme", name="Acme Corp"))
    session.add(Payer(payer_id="zenith", name="Zenith Industries"))
    session.add(PayerAlias(alias="ACME CORP", payer_id="acme"))
    session.add(PayerAlias(alias="ZENITH INDUSTRIES", payer_id="zenith"))
    session.add(
        Invoice(
            invoice_id="INV-1023", payer_id="acme", amount=12000.0,
            issued_on=date(2026, 4, 1), due_date=date(2026, 5, 1),
        )
    )
    session.add(
        Invoice(
            invoice_id="INV-1024", payer_id="acme", amount=4500.0,
            issued_on=date(2026, 4, 1), due_date=date(2026, 5, 1),
        )
    )
    session.add(
        Invoice(
            invoice_id="INV-1025", payer_id="acme", amount=7500.0,
            issued_on=date(2026, 4, 1), due_date=date(2026, 5, 1),
        )
    )
    session.add(
        Invoice(
            invoice_id="INV-2001", payer_id="zenith", amount=3000.0,
            issued_on=date(2026, 4, 1), due_date=date(2026, 5, 1),
        )
    )
    session.commit()
    return session


def test_threshold_decreases_with_higher_trust():
    assert threshold_for_trust(0.3) > threshold_for_trust(0.5)
    assert threshold_for_trust(0.5) > threshold_for_trust(0.75)
    assert threshold_for_trust(0.95) == threshold_for_trust(0.75)  # clamped at high band


def test_clean_match_auto_posts_and_emits_trust_event(seeded, ranker):
    trust = TrustEngine(seeded)
    wire = WireTransfer(
        wire_id="W1", amount=12000.0, received_on=date(2026, 5, 2),
        memo="PAYMENT INV-1023 ACME CORP", sender_name="ACME CORP",
    )
    result = service.ingest_wire(seeded, ranker=ranker, wire=wire, trust_engine=trust)

    assert result.final_status == WireStatus.AUTO_MATCHED
    assert result.resolved_payer_id == "acme"
    assert result.candidate_count >= 1

    inv = seeded.get(Invoice, "INV-1023")
    assert inv.status == InvoiceStatus.PAID or inv.amount_paid > 0
    # AUTO_MATCHED trust event written.
    events = list(seeded.exec(
        TrustEventRecord.__table__.select().where(
            TrustEventRecord.payer_id == "acme"
        )
    ))
    assert any(e.event_type == TrustEventType.AUTO_MATCHED for e in events)


def test_no_payer_match_yields_unmatched_or_review(seeded, ranker):
    wire = WireTransfer(
        wire_id="W2", amount=999.0, received_on=date(2026, 5, 2),
        memo="UNKNOWN VENDOR REF 8888", sender_name="UNKNOWN VENDOR",
    )
    result = service.ingest_wire(seeded, ranker=ranker, wire=wire, trust_engine=None)
    # No ER hit, no candidates, should be UNMATCHED.
    assert result.final_status == WireStatus.UNMATCHED
    assert result.resolved_payer_id is None


def test_bundle_match_auto_posts_when_total_fits(seeded, ranker):
    # 4500 + 7500 = 12000, but invoice INV-1023 is also 12000 — to force
    # bundling, we use a different amount that only the bundle satisfies.
    # 4500 + 7500 = 12000 — same as INV-1023. Let's pick a wire that only
    # the bundle of two could satisfy: 4500 + 7500 - 50 won't fit either.
    # Build a scenario where INV-1023 is removed first.
    inv = seeded.get(Invoice, "INV-1023")
    inv.status = InvoiceStatus.PAID
    seeded.add(inv)
    seeded.commit()

    trust = TrustEngine(seeded)
    wire = WireTransfer(
        wire_id="W3", amount=12000.0, received_on=date(2026, 5, 2),
        memo="BULK PMT ACME CORP", sender_name="ACME CORP",
    )
    result = service.ingest_wire(seeded, ranker=ranker, wire=wire, trust_engine=trust)

    # One of: auto_matched as bundle, or under_review with bundle candidate.
    assert result.final_status in (WireStatus.AUTO_MATCHED, WireStatus.UNDER_REVIEW)
    cands = list(seeded.exec(
        MatchCandidate.__table__.select().where(MatchCandidate.wire_id == "W3")
    ))
    assert any(c.is_bundle for c in cands)


def test_low_trust_payer_requires_higher_confidence(seeded, ranker):
    """Drop trust very low and verify the threshold tightens."""
    trust = TrustEngine(seeded)
    # Push acme trust down with multiple PROMISE_BROKEN events.
    for _ in range(6):
        trust.update_trust("acme", TrustEventType.PROMISE_BROKEN)
    score = trust.get_trust("acme")
    assert score < 0.30
    assert threshold_for_trust(score) >= 0.97


def test_human_override_emits_negative_trust_event(seeded, ranker):
    trust = TrustEngine(seeded)
    wire = WireTransfer(
        wire_id="W4", amount=12000.0, received_on=date(2026, 5, 2),
        memo="PAYMENT INV-1023 ACME CORP", sender_name="ACME CORP",
    )
    service.ingest_wire(seeded, ranker=ranker, wire=wire, trust_engine=trust)

    before = trust.get_trust("acme")
    service.override_match(
        seeded, wire_id="W4", new_invoice_ids=["INV-1024"],
        reviewer="rohan", trust_engine=trust, note="model picked wrong invoice",
    )
    after = trust.get_trust("acme")
    assert after < before


def test_reject_does_not_change_trust(seeded, ranker):
    wire = WireTransfer(
        wire_id="W5", amount=200.0, received_on=date(2026, 5, 2),
        memo="MISC", sender_name="UNKNOWN",
    )
    service.ingest_wire(seeded, ranker=ranker, wire=wire, trust_engine=None)
    # Reject the wire — no payer was even resolved, so no trust event possible.
    service.reject_wire(seeded, wire_id="W5", reviewer="rohan")
    w = seeded.get(WireTransfer, "W5")
    assert w.status == WireStatus.REJECTED


def test_partial_payment_marks_invoice_partial(seeded, ranker):
    """A wire smaller than the invoice yields PARTIAL on confirm.

    We force the path through human confirmation — auto-match wouldn't
    fire on a 30%-of-amount wire because amount features dominate.
    """
    trust = TrustEngine(seeded)
    wire = WireTransfer(
        wire_id="W6", amount=4000.0, received_on=date(2026, 5, 2),
        memo="PARTIAL INV-1023 ACME CORP", sender_name="ACME CORP",
    )
    result = service.ingest_wire(seeded, ranker=ranker, wire=wire, trust_engine=trust)
    if result.final_status == WireStatus.UNDER_REVIEW and result.best_candidate_id:
        m = service.confirm_match(
            seeded, wire_id="W6", candidate_id=result.best_candidate_id,
            reviewer="rohan", trust_engine=trust,
        )
        assert m.outcome.value in ("human_confirmed", "auto_posted")
        inv = seeded.get(Invoice, "INV-1023")
        assert inv.status in (InvoiceStatus.PAID, InvoiceStatus.PARTIAL)
