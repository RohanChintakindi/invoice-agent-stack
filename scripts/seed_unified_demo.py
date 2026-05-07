"""Seed structural data only — payers, aliases, invoices, portals, vault,
and the trained recon ranker. No synthetic events, calls, jobs, or wires.

The ops dashboard timeline starts empty; live demo runs (scrape button,
talk button, sync_plaid, etc.) populate it organically. This is the
"truth-only" mode for interview demos where every event a viewer sees
came from a real backend run, not seed_unified_demo.

Idempotent: running twice is safe (per-row get-or-skip).

Run:
    uv run python -m scripts.seed_unified_demo
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from sqlmodel import select

from browser_orchestration.models import Portal
from browser_orchestration.vault import CredentialVault, generate_key
from cash_recon.models import Invoice, InvoiceStatus, PayerAlias
from cash_recon.ranker import DEFAULT_ARTIFACT_DIR, load, train
from shared.db import init_schema, make_engine, session_scope
from shared.models import Payer


PAYERS = [
    ("acme", "Acme Corp"),
    ("zenith", "Zenith Industries"),
    ("globex", "Globex LLC"),
]

import os

# Default to the live demo_portal URLs on Fly so the browser-use adapter
# (when run locally against this DB) hits a real, scrapeable target.
# Override with PORTAL_BASE if you point at a different host.
_PORTAL_BASE = os.getenv(
    "PORTAL_BASE", "https://invoice-agent-stack-rohan.fly.dev/portal"
)

PORTALS = [
    ("acme_portal",   "Acme AP Portal",       f"{_PORTAL_BASE}/acme_portal/",   "acme"),
    ("zenith_portal", "Zenith Vendor Portal", f"{_PORTAL_BASE}/zenith_portal/", "zenith"),
    ("globex_portal", "Globex Billing Portal", f"{_PORTAL_BASE}/globex_portal/", "globex"),
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


# Voice / browser / recon event seeding intentionally removed. The demo now
# starts with an empty timeline and populates organically when buttons are
# clicked or scripts (sync_plaid, run_browser_full_loop) run. See git
# history for the previous seed-events code if you ever want it back.


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


def _seed_portals(engine) -> None:
    """Register the portal records and store demo credentials in the vault.
    Idempotent — safe to re-run. No jobs enqueued; the timeline stays empty
    until a real demo run (button click / scripts/run_browser_full_loop)
    fires."""
    if not os.getenv("VAULT_KEY"):
        os.environ["VAULT_KEY"] = generate_key()
    vault = CredentialVault.from_env()

    with session_scope(engine) as s:
        for portal_id, name, url, payer_id in PORTALS:
            existing = s.get(Portal, portal_id)
            if existing is None:
                s.add(Portal(portal_id=portal_id, name=name, base_url=url))
                s.flush()
            elif existing.base_url != url:
                # Pick up URL drift on re-seed (e.g. moving to live demo_portal).
                existing.base_url = url
                s.add(existing)
                s.flush()

            try:
                vault.store(
                    s,
                    portal_id=portal_id,
                    payer_id=payer_id,
                    username="ap@example",
                    secret="hunter2",
                )
            except Exception:
                # Already stored — vault is idempotent on (portal, payer).
                pass


def _seed_recon_ranker() -> None:
    """Make sure the trained ranker model is on disk; train it on synthetic
    data if not. No wires are ingested into the DB — the recon timeline
    starts empty and populates from real Plaid sync runs (or the dashboard
    Run-scrape button paired with a wire-ingest endpoint) at demo time."""
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR
    if (artifact_dir / "ranker.json").exists():
        return
    print("[seed] training cash_recon ranker on synthetic data...")
    ranker = train(seed=42, n_invoices=300, n_wires=300)
    ranker.save(artifact_dir)


def main() -> None:
    engine = make_engine()
    init_schema(engine)

    with session_scope(engine) as s:
        _seed_payers_and_aliases(s)

    _seed_invoices(engine)
    _seed_portals(engine)
    _seed_recon_ranker()

    print("[seed_unified_demo] OK — structural seed complete:")
    print(f"  payers   : {len(PAYERS)}")
    print(f"  aliases  : {len(ALIASES)}")
    print(f"  invoices : 18 across acme / zenith / globex")
    print(f"  portals  : {len(PORTALS)} pointing at live demo_portal URLs")
    print("  ranker   : trained model on disk")
    print()
    print("Timeline starts empty. Drive activity with the dashboard")
    print("buttons or scripts/sync_plaid + scripts/run_browser_full_loop.")


if __name__ == "__main__":
    main()
