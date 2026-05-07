"""Wipe all event-shaped rows from the DB while keeping structural data.

Use this when the seed-events pattern from the old seed_unified_demo
left fake activity in the DB and you want a clean timeline before a
demo without nuking the SQLite file (the Fly volume).

Kept (structural):
  - Payer
  - PayerAlias
  - Invoice (status reset to OPEN)
  - Portal
  - Credential (vault entries)
  - PayerTrust (raw_score reset to 0.5)

Wiped (events / activity):
  - TrustEventRecord
  - Job, JobAttempt, ExtractionRecord, PortalHealthEvent
  - WireTransfer, MatchCandidate, Match, ReviewDecision
  - CallSession-derived rows (CallRecord, Promise, Objection, Contact)

Run:
    uv run python -m scripts.clear_demo_events
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from sqlmodel import select, delete

from cash_recon.models import (
    Invoice,
    InvoiceStatus,
    Match,
    MatchCandidate,
    ReviewDecision,
    WireTransfer,
)
from shared.db import init_schema, make_engine, session_scope
from shared.models import PayerTrust, TrustEventRecord


def main() -> int:
    engine = make_engine()
    init_schema(engine)

    counts: dict[str, int] = {}

    # --- Cash recon events ---
    with session_scope(engine) as s:
        for table in (Match, MatchCandidate, ReviewDecision, WireTransfer):
            rows = list(s.exec(select(table)))
            counts[table.__name__] = len(rows)
            for r in rows:
                s.delete(r)
        # Reset paid invoices back to OPEN.
        invs = list(s.exec(select(Invoice)))
        reset = 0
        for inv in invs:
            if inv.status != InvoiceStatus.OPEN:
                inv.status = InvoiceStatus.OPEN
                inv.paid_at = None
                s.add(inv)
                reset += 1
        counts["Invoice (reset to OPEN)"] = reset

    # --- Browser orchestration events ---
    with session_scope(engine) as s:
        # Lazy-import these because the table list is long and many models
        # may not exist on every install. Fail soft on missing tables.
        try:
            from browser_orchestration.models import (
                ExtractionRecord,
                Job,
                JobAttempt,
                PortalHealthEvent,
            )
            for table in (JobAttempt, ExtractionRecord, PortalHealthEvent, Job):
                try:
                    rows = list(s.exec(select(table)))
                    counts[table.__name__] = len(rows)
                    for r in rows:
                        s.delete(r)
                except Exception as exc:
                    counts[f"{table.__name__} (skipped)"] = 0
                    print(f"  skip {table.__name__}: {exc}", file=sys.stderr)
        except ImportError:
            pass

    # --- Voice agent memory events ---
    with session_scope(engine) as s:
        try:
            from voice_agent.memory_models import (
                CallRecord,
                Contact,
                Objection,
                Promise,
            )
            # Keep Contact rows — they're reference data the voice agent
            # uses to know who Karen / Bob / Phil are. Not events.
            for table in (CallRecord, Promise, Objection):
                try:
                    rows = list(s.exec(select(table)))
                    counts[table.__name__] = len(rows)
                    for r in rows:
                        s.delete(r)
                except Exception as exc:
                    counts[f"{table.__name__} (skipped)"] = 0
                    print(f"  skip {table.__name__}: {exc}", file=sys.stderr)
        except ImportError:
            pass

    # --- Trust engine events + cached scores ---
    with session_scope(engine) as s:
        events = list(s.exec(select(TrustEventRecord)))
        counts["TrustEventRecord"] = len(events)
        for ev in events:
            s.delete(ev)

        # Reset any cached PayerTrust rows to neutral 0.5 so the dashboard
        # starts everyone at the same baseline.
        cached = list(s.exec(select(PayerTrust)))
        for c in cached:
            c.raw_score = 0.5
            c.last_event_at = None
            s.add(c)
        counts["PayerTrust (reset to 0.5)"] = len(cached)

    print("[clear_demo_events] cleared:")
    for name, n in counts.items():
        print(f"  {name:30s} {n}")
    print()
    print("Structural data (Payer, Alias, Invoice, Portal, Credential, Contact)")
    print("preserved. Timeline is now empty; drive new activity with the")
    print("dashboard buttons or sync_plaid / run_browser_full_loop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
