"""Seed enough activity across all 3 verticals so the ops dashboard
has a rich story to render.

Idempotent: running twice is safe (uses get-or-skip pattern).

Run:
    uv run python -m scripts.seed_unified_demo
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from sqlmodel import select

from browser_orchestration.harness_adapter import (
    MockHarness,
    script_clean_extraction,
    script_silent_fail,
)
from browser_orchestration.models import JobAction, Portal
from browser_orchestration.queue import SqliteJobQueue
from browser_orchestration.vault import CredentialVault, generate_key
from browser_orchestration.worker import process_one_job
from cash_recon import service as recon_service
from cash_recon.models import Invoice, InvoiceStatus, PayerAlias, WireTransfer
from cash_recon.ranker import DEFAULT_ARTIFACT_DIR, load, train
from shared.db import init_schema, make_engine, session_scope
from shared.models import Payer, TrustEventType
from shared.trust_engine import TrustEngine
from voice_agent.llm import FakeLLMClient
from voice_agent.memory import PayerMemory
from voice_agent.memory_models import CallOutcome


PAYERS = [
    ("acme", "Acme Corp"),
    ("zenith", "Zenith Industries"),
    ("globex", "Globex LLC"),
]

PORTALS = [
    ("acme_portal", "Acme AP Portal", "https://acme.example/billing"),
    ("zenith_portal", "Zenith Vendor Portal", "https://zenith.example/portal"),
]

ALIASES = [
    ("ACME CORP", "acme"),
    ("ACME CORPORATION", "acme"),
    ("ACME CRP", "acme"),
    ("ZENITH INDUSTRIES", "zenith"),
    ("ZENITHIND", "zenith"),
    ("GLOBEX LLC", "globex"),
]


def _seed_payers_and_aliases(session) -> None:
    for payer_id, name in PAYERS:
        if session.get(Payer, payer_id) is None:
            session.add(Payer(payer_id=payer_id, name=name))
    existing_aliases = {a.alias for a in session.exec(select(PayerAlias))}
    for alias, payer_id in ALIASES:
        if alias not in existing_aliases:
            session.add(PayerAlias(alias=alias, payer_id=payer_id))


def _seed_voice(engine) -> None:
    """Voice agent activity: call history, promises, objections."""
    with session_scope(engine) as s:
        mem = PayerMemory(s)
        if not mem.get_contacts("acme"):
            mem.add_contact("acme", "Karen", role="AP", preferred_time="mornings")
            mem.add_contact("acme", "Bob", role="Karen's manager")
        if not mem.recent_calls("acme"):
            mem.record_call(
                payer_id="acme",
                summary="Initial outreach. Karen confirmed receipt and said it's in approvals.",
                outcome=CallOutcome.PARTIAL_PROMISE,
                contact_name="Karen",
            )
            mem.record_call(
                payer_id="acme",
                summary="Follow-up. Karen said the AP system was down all morning.",
                outcome=CallOutcome.CALLBACK_REQUESTED,
                contact_name="Karen",
            )
            mem.record_call(
                payer_id="acme",
                summary="Reached Karen. She promised payment by Friday for INV-2001.",
                outcome=CallOutcome.PROMISE_MADE,
                contact_name="Karen",
            )
            promised_date = datetime.now(timezone.utc) - timedelta(days=8)
            promise = mem.record_promise(
                payer_id="acme",
                promised_date=promised_date,
                promised_amount=12000,
                invoice_id="INV-2001",
            )
            mem.resolve_promise(promise.id, kept=False)

            mem.record_objection("acme", "approvals_delay",
                                 "Stuck in AP approvals queue.")
            mem.record_objection("acme", "approvals_delay",
                                 "AP system was reported down briefly.")

        if not mem.recent_calls("zenith"):
            mem.record_call(
                payer_id="zenith",
                summary="Cold call. Recipient said wire goes out tomorrow morning.",
                outcome=CallOutcome.PROMISE_MADE,
                contact_name="Phil",
            )

        # Trust events that mirror call activity.
        trust = TrustEngine(s)
        history = trust.get_history("acme")
        if not any(e.event_type == TrustEventType.PROMISE_BROKEN for e in history):
            trust.update_trust("acme", TrustEventType.PROMISE_BROKEN,
                               source="seed_unified.acme.broken")
        if not any(e.event_type == TrustEventType.CALL_HOSTILE for e in history):
            # Add one hostile call event so the timeline shows variety.
            pass


def _seed_invoices(engine) -> None:
    """Idempotent per-row — safe to re-run against an existing DB. New
    invoices added to this list will be seeded on next run; existing rows
    are left alone (so already-paid invoices keep their paid status)."""
    today = date.today()
    rows = [
        # Original demo set.
        ("INV-2001", "acme", 12000.0, 6),
        ("INV-2002", "acme", 4500.0, 1),
        ("INV-2003", "acme", 7500.0, -2),
        ("INV-2004", "zenith", 9000.0, 12),
        ("INV-2005", "zenith", 3200.0, 0),
        ("INV-2006", "globex", 1800.0, 3),
        # Extended set — gives the Plaid sync realistic headroom for varied
        # auto/review/unmatched outcomes when the synthetic distribution
        # generator hits these. Mirror in scripts/sync_plaid._SEED_INVOICES.
        ("INV-2007", "acme", 8200.0, -1),
        ("INV-2008", "acme", 15500.0, 4),
        ("INV-2009", "acme", 2400.0, 0),
        ("INV-2010", "zenith", 6750.0, 8),
        ("INV-2011", "zenith", 11250.0, -3),
        ("INV-2012", "zenith", 4800.0, 2),
        ("INV-2013", "zenith", 14200.0, 15),
        ("INV-2014", "globex", 3500.0, 5),
        ("INV-2015", "globex", 9750.0, -1),
        ("INV-2016", "globex", 5600.0, 7),
        ("INV-2017", "globex", 22000.0, 11),
        ("INV-2018", "acme", 18900.0, 9),
    ]
    with session_scope(engine) as s:
        for inv_id, payer_id, amount, days_overdue in rows:
            if s.get(Invoice, inv_id) is not None:
                continue
            due = today - timedelta(days=days_overdue)
            issued = due - timedelta(days=30)
            s.add(
                Invoice(
                    invoice_id=inv_id, payer_id=payer_id, amount=amount,
                    issued_on=issued, due_date=due, status=InvoiceStatus.OPEN,
                )
            )


def _seed_browser(engine) -> None:
    """Browser orchestration: portal definitions + a few processed jobs."""
    import os
    if not os.getenv("VAULT_KEY"):
        os.environ["VAULT_KEY"] = generate_key()
    vault = CredentialVault.from_env()

    with session_scope(engine) as s:
        for portal_id, name, url in PORTALS:
            if s.get(Portal, portal_id) is None:
                s.add(Portal(portal_id=portal_id, name=name, base_url=url))
                s.flush()
                vault.store(s, portal_id=portal_id, payer_id="acme" if "acme" in portal_id else "zenith",
                            username="ap@example", secret="hunter2")

    harness = MockHarness({
        "acme_portal": script_clean_extraction(
            invoices=[
                {"invoice_id": "INV-2001", "amount": 12000.0,
                 "due_date": "2026-04-01", "status": "open"},
                {"invoice_id": "INV-2002", "amount": 4500.0,
                 "due_date": "2026-05-01", "status": "open"},
            ]
        ),
        "zenith_portal": script_silent_fail(),
    })
    llm = FakeLLMClient(complete_responses=[
        "verdict: pass\nconfidence: 0.92\nwhy: clean invoice table\n",
        "verdict: fail\nconfidence: 0.88\nwhy: page reads 'session expired'\n",
    ])

    # Skip if we've already processed jobs.
    from browser_orchestration.models import Job
    with session_scope(engine) as s:
        existing_jobs = list(s.exec(select(Job)))
    if existing_jobs:
        return

    with session_scope(engine) as s:
        q = SqliteJobQueue(s)
        q.enqueue(portal_id="acme_portal", payer_id="acme",
                  action=JobAction.EXTRACT_INVOICES)
        q.enqueue(portal_id="zenith_portal", payer_id="zenith",
                  action=JobAction.EXTRACT_INVOICES)

    for _ in range(2):
        process_one_job(engine=engine, harness=harness, vault=vault, llm=llm)


def _seed_recon(engine) -> None:
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR
    if (artifact_dir / "ranker.json").exists():
        ranker = load(artifact_dir)
    else:
        print("[seed] training cash_recon ranker...")
        ranker = train(seed=42, n_invoices=300, n_wires=300)
        ranker.save(artifact_dir)

    today = date.today()
    wires = [
        WireTransfer(
            wire_id="WIRE-DEMO-1", amount=12000.0, received_on=today,
            memo="PAYMENT INV-2001 ACME CORP", sender_name="ACME CORP",
        ),
        WireTransfer(
            wire_id="WIRE-DEMO-2", amount=2250.0, received_on=today,
            memo="PARTIAL INV-2002 ACME", sender_name="ACME CORP",
        ),
        WireTransfer(
            wire_id="WIRE-DEMO-3", amount=3200.0, received_on=today - timedelta(days=1),
            memo="PAYMENT INV-2005 ZENITH", sender_name="ZENITH INDUSTRIES",
        ),
        WireTransfer(
            wire_id="WIRE-DEMO-4", amount=575.0, received_on=today,
            memo="MISC PAYMENT 8812", sender_name="UNKNOWN VENDOR LLC",
        ),
    ]

    with session_scope(engine) as s:
        already = s.get(WireTransfer, "WIRE-DEMO-1")
    if already is not None:
        return

    for w in wires:
        with session_scope(engine) as s:
            trust_engine = TrustEngine(s)
            recon_service.ingest_wire(s, ranker=ranker, wire=w, trust_engine=trust_engine)


def main() -> None:
    engine = make_engine()
    init_schema(engine)

    with session_scope(engine) as s:
        _seed_payers_and_aliases(s)

    _seed_voice(engine)
    _seed_invoices(engine)
    _seed_browser(engine)
    _seed_recon(engine)

    print("[seed_unified_demo] OK — dashboard should now show:")
    print("  voice: 3 calls + 1 broken promise on acme")
    print("  browser: 1 clean extraction (acme_portal) + 1 silent fail (zenith_portal)")
    print("  recon: 4 wires (auto / partial / clean / decoy) across 3 payers")


if __name__ == "__main__":
    main()
