"""Seed a synthetic Acme Corp payer with realistic call history.

Used by the CLI demo and by the FastAPI server when started in demo
mode. Idempotent — re-running won't duplicate rows.

Run with:
    uv run python -m scripts.seed_demo
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shared.db import init_schema, make_engine, session_scope
from shared.models import Payer, TrustEventType
from shared.trust_engine import TrustEngine
from voice_agent.memory import PayerMemory
from voice_agent.memory_models import CallOutcome


def seed_acme() -> None:
    engine = make_engine()
    init_schema(engine)

    with session_scope(engine) as db:
        existing = db.get(Payer, "acme")
        if existing is None:
            db.add(Payer(payer_id="acme", name="Acme Corp"))
            db.flush()

        mem = PayerMemory(db)
        if not mem.get_contacts("acme"):
            mem.add_contact("acme", "Karen", role="AP", preferred_time="mornings")
            mem.add_contact("acme", "Bob", role="Karen's manager")

        if not mem.recent_calls("acme"):
            mem.record_call(
                payer_id="acme",
                summary="First contact. Karen confirmed receipt and said it's in approvals.",
                outcome=CallOutcome.PARTIAL_PROMISE,
                contact_name="Karen",
            )
            promised_date = datetime.now(timezone.utc) - timedelta(days=10)
            promise = mem.record_promise(
                payer_id="acme",
                promised_date=promised_date,
                promised_amount=12000,
                invoice_id="INV-1023",
            )
            mem.resolve_promise(promise.id, kept=False)

            mem.record_objection("acme", "approvals_delay", "Stuck in AP approvals queue.")
            mem.record_objection("acme", "approvals_delay", "AP says it's still being approved.")

        # Seed trust events that mirror the call history.
        trust = TrustEngine(db)
        history = trust.get_history("acme")
        if not history:
            trust.update_trust("acme", TrustEventType.PROMISE_BROKEN, source="seed_demo")

    print("Seeded Acme Corp.")


if __name__ == "__main__":
    seed_acme()
