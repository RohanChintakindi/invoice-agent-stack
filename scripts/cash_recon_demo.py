"""End-to-end demo for the cash reconciliation vertical.

Trains the ranker, seeds demo state, ingests four representative wires
through service.ingest_wire, and prints what happened to each + the
resulting trust scores.

Run:
    uv run python -m scripts.seed_demo      # creates payer "acme"
    uv run python -m scripts.seed_recon     # invoices + aliases
    uv run python -m scripts.cash_recon_demo
"""

from __future__ import annotations

import sys
import textwrap
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

from cash_recon import service
from cash_recon.models import (
    Invoice,
    InvoiceStatus,
    Match,
    MatchCandidate,
    PayerAlias,
    WireTransfer,
)
from cash_recon.ranker import DEFAULT_ARTIFACT_DIR, load, train
from shared.db import init_schema, make_engine, session_scope
from shared.models import Payer
from shared.trust_engine import TrustEngine


def _panel(title: str, lines: list[str]) -> str:
    bar = "-" * 78
    body = "\n".join(f"| {line}" for line in lines)
    return f"+{bar}\n| {title}\n+{bar}\n{body}\n+{bar}"


def _wrap(s: str, width: int = 70) -> str:
    return "\n".join(textwrap.wrap(s, width=width)) or s


def _train_or_load_ranker():
    artifact_dir = DEFAULT_ARTIFACT_DIR
    if (artifact_dir / "ranker.json").exists():
        print("[demo] loaded trained ranker from", artifact_dir)
        return load(artifact_dir)
    print("[demo] training ranker on synthetic dataset...")
    r = train(seed=42, n_invoices=300, n_wires=300)
    r.save(artifact_dir)
    print("[demo] ranker AUC =", round(r.metrics["auc"], 3),
          "Brier =", round(r.metrics["brier"], 4))
    return r


def main() -> int:
    engine = make_engine()
    init_schema(engine)
    ranker = _train_or_load_ranker()

    # Auto-seed if necessary so the demo runs from a cold checkout.
    with session_scope(engine) as s:
        if s.get(Payer, "acme") is None:
            from scripts.seed_demo import seed_acme
            seed_acme()
        if s.get(Invoice, "INV-2001") is None:
            from scripts.seed_recon import seed
            seed()

    today = date.today()
    wires = [
        # 1. Clean single match: amount + invoice_id + alias all line up.
        WireTransfer(
            wire_id="WIRE-CLEAN", amount=12000.0,
            received_on=today,
            memo="PAYMENT INV-2001 ACME CORP", sender_name="ACME CORP",
        ),
        # 2. Partial: 50% of INV-2002, will likely route to review.
        WireTransfer(
            wire_id="WIRE-PARTIAL", amount=2250.0,
            received_on=today,
            memo="PARTIAL INV-2002 ACME", sender_name="ACME CORP",
        ),
        # 3. Bundle: 4500 + 7500 = 12000 from acme — but INV-2001 (12000)
        # will already be paid, so the bundler should fire.
        WireTransfer(
            wire_id="WIRE-BUNDLE", amount=12000.0,
            received_on=today + timedelta(days=1),
            memo="BULK PMT ACME CORP", sender_name="ACME CORPORATION",
        ),
        # 4. Decoy: not in our system at all.
        WireTransfer(
            wire_id="WIRE-DECOY", amount=575.0,
            received_on=today,
            memo="MISC PAYMENT 8812", sender_name="UNKNOWN VENDOR LLC",
        ),
    ]

    summaries: list[str] = []

    for w in wires:
        # Snapshot scalar fields up front so we can format them after
        # the session closes (sqlalchemy expires the attached instance).
        snap = {
            "wire_id": w.wire_id, "amount": w.amount,
            "sender_name": w.sender_name, "memo": w.memo,
        }
        with session_scope(engine) as s:
            trust = TrustEngine(s)
            result = service.ingest_wire(s, ranker=ranker, wire=w, trust_engine=trust)

            cands = list(
                s.exec(
                    MatchCandidate.__table__.select().where(
                        MatchCandidate.wire_id == snap["wire_id"]
                    )
                )
            )
            cand_lines = [
                f"  cand {c.id} cal={c.calibrated_prob:.3f} bundle={c.is_bundle}"
                f" -> {c.invoice_ids}"
                for c in sorted(cands, key=lambda x: x.calibrated_prob, reverse=True)[:3]
            ]

        lines = [
            f"wire_id    : {snap['wire_id']}",
            f"amount     : ${snap['amount']:.2f}",
            f"sender     : {snap['sender_name']}",
            f"memo       : {_wrap(snap['memo'], 60)}",
            f"resolved   : {result.resolved_payer_id or '(none)'}",
            f"final      : {result.final_status.value}",
            f"best cal   : {result.best_calibrated_prob:.3f}",
            f"threshold  : {result.threshold_used:.3f}",
            f"notes      : {_wrap(result.notes, 60)}",
        ]
        if cand_lines:
            lines.append("top candidates:")
            lines.extend(cand_lines)
        print(_panel(f"ingest {snap['wire_id']}", lines))
        print()
        summaries.append(f"{snap['wire_id']}: {result.final_status.value}")

    # Demonstrate human override on the partial (if it landed in review).
    with session_scope(engine) as s:
        from cash_recon.models import WireStatus
        partial = s.get(WireTransfer, "WIRE-PARTIAL")
        if partial and partial.status == WireStatus.UNDER_REVIEW:
            cands = list(
                s.exec(
                    MatchCandidate.__table__.select().where(
                        MatchCandidate.wire_id == "WIRE-PARTIAL"
                    )
                )
            )
            if cands:
                trust = TrustEngine(s)
                # Confirm the top candidate from the review queue.
                top = max(cands, key=lambda c: c.calibrated_prob)
                m = service.confirm_match(
                    s, wire_id="WIRE-PARTIAL", candidate_id=top.id,
                    reviewer="rohan", trust_engine=trust,
                    note="confirmed manually after review",
                )
                summaries.append(f"WIRE-PARTIAL confirmed: {m.outcome.value}")

    # Final trust panel.
    with session_scope(engine) as s:
        trust = TrustEngine(s)
        rows: list[str] = []
        for payer_id in ("acme", "zenith", "globex"):
            score = trust.get_trust(payer_id)
            history = [
                (e.event_type.value, e.delta)
                for e in trust.get_history(payer_id)[-3:]
            ]
            rows.append(
                f"{payer_id:8s}  trust={score:.3f}  last={history if history else '(none)'}"
            )
    print(_panel("post-run trust", rows))
    print()
    print(_panel("summary", summaries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
