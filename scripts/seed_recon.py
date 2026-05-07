"""Seed cash_recon demo state: open invoices + payer aliases.

Idempotent. Run before scripts/cash_recon_demo.py.
"""

from __future__ import annotations

from datetime import date

from sqlmodel import select

from cash_recon.models import Invoice, InvoiceStatus, PayerAlias
from shared.db import init_schema, make_engine, session_scope
from shared.models import Payer

DEMO_INVOICES = [
    # (invoice_id, payer_id, amount, days_overdue)
    ("INV-2001", "acme", 12000.0, 6),
    ("INV-2002", "acme", 4500.0, 1),
    ("INV-2003", "acme", 7500.0, -2),
    ("INV-2004", "zenith", 9000.0, 12),
    ("INV-2005", "zenith", 3200.0, 0),
    ("INV-2006", "globex", 1800.0, 3),
]

DEMO_ALIASES = [
    ("ACME CORP", "acme"),
    ("ACME CORPORATION", "acme"),
    ("ACME CRP", "acme"),
    ("ZENITH INDUSTRIES", "zenith"),
    ("ZENITHIND", "zenith"),
    ("GLOBEX LLC", "globex"),
]


def seed() -> None:
    engine = make_engine()
    init_schema(engine)

    today = date.today()
    with session_scope(engine) as s:
        for payer_id, name in (
            ("acme", "Acme Corp"),
            ("zenith", "Zenith Industries"),
            ("globex", "Globex LLC"),
        ):
            if s.get(Payer, payer_id) is None:
                s.add(Payer(payer_id=payer_id, name=name))

        for inv_id, payer_id, amount, days_overdue in DEMO_INVOICES:
            if s.get(Invoice, inv_id) is None:
                from datetime import timedelta
                due = today - timedelta(days=days_overdue)
                issued = due - timedelta(days=30)
                s.add(
                    Invoice(
                        invoice_id=inv_id, payer_id=payer_id, amount=amount,
                        issued_on=issued, due_date=due, status=InvoiceStatus.OPEN,
                    )
                )

        existing_aliases = {
            a.alias for a in s.exec(select(PayerAlias))
        }
        for alias, payer_id in DEMO_ALIASES:
            if alias not in existing_aliases:
                s.add(PayerAlias(alias=alias, payer_id=payer_id))

    print("Seeded cash_recon demo state.")


if __name__ == "__main__":
    seed()
