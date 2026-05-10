"""Trigger a random AR collections call via Vapi.

Picks a random payer with at least one open invoice in the local DB,
selects the most overdue invoice for that payer, and asks Vapi to dial
the configured outbound number with rich context (payer_id, invoice_id,
amount, days overdue) baked into the assistant variables.

The voice agent on Cloud Run reads `payer_id` from the assistant prompt's
[payer_id=...] tag (or call.metadata) and pulls the rest from its DB,
so the LLM has the full picture by turn 1.

Run once for a single call:
    uv run python -m scripts.random_outbound_call

Or in a loop for "calls throughout the day" — wrap it in your own
scheduler. A cheap 'random cadence' shell pattern:
    while True; do uv run python -m scripts.random_outbound_call; \
        sleep $((1800 + RANDOM % 1800)); done
(every 30-60 min, randomized).

Required env (.env):
    VAPI_API_KEY            — auth
    VAPI_ASSISTANT_ID       — the AR collections assistant
    VAPI_PHONE_NUMBER_ID    — outbound number leased from Vapi
    OUTBOUND_TARGET_NUMBER  — E.164 phone to dial (e.g. +12404381333)
    OUTBOUND_TARGET_NAME    — optional, shown in Vapi logs
"""

from __future__ import annotations

import os
import random
import sys
from datetime import date

import httpx
from dotenv import load_dotenv
from sqlmodel import select

load_dotenv()

from cash_recon.models import Invoice, InvoiceStatus
from shared.db import make_engine, session_scope
from shared.models import Payer


VAPI_BASE = "https://api.vapi.ai"


def _pick_call_target(engine) -> dict | None:
    """Pick a random payer with at least one open invoice; return the most
    overdue invoice as the call subject. Returns None if no suitable
    candidates exist (DB empty or all invoices closed)."""
    today = date.today()
    with session_scope(engine) as s:
        payers = list(s.exec(select(Payer)))
        random.shuffle(payers)
        for payer in payers:
            invs = list(
                s.exec(
                    select(Invoice)
                    .where(Invoice.payer_id == payer.payer_id)
                    .where(Invoice.status == InvoiceStatus.OPEN)
                )
            )
            if not invs:
                continue
            # Most overdue first — biggest demo signal.
            invs.sort(key=lambda i: i.due_date)
            inv = invs[0]
            days_overdue = max(0, (today - inv.due_date).days)
            return {
                "payer_id": payer.payer_id,
                "payer_name": payer.name,
                "invoice_id": inv.invoice_id,
                "amount": inv.amount,
                "due_date": inv.due_date.isoformat(),
                "days_overdue": days_overdue,
            }
    return None


def _build_first_message(target: dict, customer_name: str) -> str:
    """Custom opening line so the call feels specific instead of generic."""
    if target["days_overdue"] > 0:
        urgency = f"about {target['days_overdue']} days overdue"
    else:
        urgency = "due"
    return (
        f"Hi, this is Iridium calling about invoice {target['invoice_id']} "
        f"for ${target['amount']:,.0f} from {target['payer_name']} — it's "
        f"{urgency}. Is this {customer_name}, do you have a moment?"
    )


def _trigger_call(*, target: dict, customer_number: str, customer_name: str) -> dict:
    api_key = os.environ["VAPI_API_KEY"]
    assistant_id = os.environ["VAPI_ASSISTANT_ID"]
    phone_id = os.environ["VAPI_PHONE_NUMBER_ID"]

    body = {
        "assistantId": assistant_id,
        "phoneNumberId": phone_id,
        "customer": {"number": customer_number, "name": customer_name},
        "assistantOverrides": {
            "firstMessage": _build_first_message(target, customer_name),
            "variableValues": {
                "payer_id": target["payer_id"],
                "payer_name": target["payer_name"],
                "invoice_id": target["invoice_id"],
                "invoice_amount": f"{target['amount']:.2f}",
                "days_overdue": target["days_overdue"],
            },
            # Vapi forwards metadata to our /voice/v1/chat/completions
            # endpoint via call.metadata. The voice agent's extract_payer_id
            # picks it up there.
            "metadata": {
                "payer_id": target["payer_id"],
                "invoice_id": target["invoice_id"],
            },
        },
    }
    resp = httpx.post(
        f"{VAPI_BASE}/call",
        json=body,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    for var in ("VAPI_API_KEY", "VAPI_ASSISTANT_ID", "VAPI_PHONE_NUMBER_ID"):
        if not os.getenv(var):
            print(f"{var} not set in .env", file=sys.stderr)
            return 2

    customer_number = os.getenv("OUTBOUND_TARGET_NUMBER", "+12404381333")
    customer_name = os.getenv("OUTBOUND_TARGET_NAME", "Karen")

    engine = make_engine()
    target = _pick_call_target(engine)
    if target is None:
        print(
            "No open invoices in the DB to call about. "
            "Run scripts/seed_unified_demo.py first.",
            file=sys.stderr,
        )
        return 3

    print(f"[call] payer       : {target['payer_id']} ({target['payer_name']})")
    print(f"[call] invoice     : {target['invoice_id']}  ${target['amount']:,.2f}")
    print(f"[call] days overdue: {target['days_overdue']}")
    print(f"[call] dialing     : {customer_number} ({customer_name})")

    result = _trigger_call(
        target=target,
        customer_number=customer_number,
        customer_name=customer_name,
    )
    print(f"[call] queued      : {result.get('id')}  status={result.get('status')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
