"""Per-payer memory store + history-summarization for the voice agent.

Two responsibilities:
  1. CRUD over calls / promises / contacts / objections.
  2. Render history into a concise system-prompt fragment that the
     orchestrator can splice into each turn.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from voice_agent.memory_models import Call, CallOutcome, Contact, Objection, Promise


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PayerMemory:
    """Read/write per-payer call history. Inject a SQLModel Session."""

    def __init__(self, session: Session, now_fn=_utcnow):
        self._session = session
        self._now = now_fn

    # ---- Contacts ------------------------------------------------------------

    def add_contact(
        self,
        payer_id: str,
        name: str,
        role: str | None = None,
        preferred_time: str | None = None,
        notes: str | None = None,
    ) -> Contact:
        contact = Contact(
            payer_id=payer_id,
            name=name,
            role=role,
            preferred_time=preferred_time,
            notes=notes,
        )
        self._session.add(contact)
        self._session.commit()
        self._session.refresh(contact)
        return contact

    def get_contacts(self, payer_id: str) -> list[Contact]:
        stmt = select(Contact).where(Contact.payer_id == payer_id)
        return list(self._session.exec(stmt))

    # ---- Calls ---------------------------------------------------------------

    def record_call(
        self,
        payer_id: str,
        summary: str,
        outcome: CallOutcome,
        invoice_id: str | None = None,
        contact_name: str | None = None,
        duration_sec: int | None = None,
        final_phase: str | None = None,
        final_tone: str | None = None,
    ) -> Call:
        call = Call(
            payer_id=payer_id,
            invoice_id=invoice_id,
            summary=summary,
            outcome=outcome,
            contact_name=contact_name,
            duration_sec=duration_sec,
            final_phase=final_phase,
            final_tone=final_tone,
            occurred_at=self._now(),
        )
        self._session.add(call)
        self._session.commit()
        self._session.refresh(call)
        return call

    def recent_calls(self, payer_id: str, limit: int = 10) -> list[Call]:
        stmt = (
            select(Call)
            .where(Call.payer_id == payer_id)
            .order_by(Call.occurred_at.desc())
            .limit(limit)
        )
        return list(self._session.exec(stmt))

    # ---- Promises ------------------------------------------------------------

    def record_promise(
        self,
        payer_id: str,
        promised_date: datetime,
        promised_amount: float | None = None,
        invoice_id: str | None = None,
        call_id: int | None = None,
    ) -> Promise:
        promise = Promise(
            payer_id=payer_id,
            promised_date=promised_date,
            promised_amount=promised_amount,
            invoice_id=invoice_id,
            call_id=call_id,
        )
        self._session.add(promise)
        self._session.commit()
        self._session.refresh(promise)
        return promise

    def resolve_promise(self, promise_id: int, kept: bool) -> Promise:
        promise = self._session.get(Promise, promise_id)
        if promise is None:
            raise ValueError(f"promise {promise_id} not found")
        promise.kept = kept
        promise.resolved_at = self._now()
        self._session.add(promise)
        self._session.commit()
        self._session.refresh(promise)
        return promise

    def open_promises(self, payer_id: str) -> list[Promise]:
        stmt = (
            select(Promise)
            .where(Promise.payer_id == payer_id)
            .where(Promise.kept.is_(None))
        )
        return list(self._session.exec(stmt))

    def broken_promise_count(self, payer_id: str) -> int:
        stmt = (
            select(Promise)
            .where(Promise.payer_id == payer_id)
            .where(Promise.kept.is_(False))
        )
        return len(list(self._session.exec(stmt)))

    def has_active_promise(self, payer_id: str) -> bool:
        return len(self.open_promises(payer_id)) > 0

    # ---- Objections ----------------------------------------------------------

    def record_objection(self, payer_id: str, kind: str, text: str) -> Objection:
        obj = Objection(payer_id=payer_id, kind=kind, text=text, occurred_at=self._now())
        self._session.add(obj)
        self._session.commit()
        self._session.refresh(obj)
        return obj

    def recurring_objections(self, payer_id: str) -> list[str]:
        """Return distinct objection kinds the payer has raised more than once."""
        stmt = select(Objection).where(Objection.payer_id == payer_id)
        rows = list(self._session.exec(stmt))
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.kind] = counts.get(row.kind, 0) + 1
        return [kind for kind, count in counts.items() if count >= 2]

    # ---- Summarization for system prompt -------------------------------------

    def summarize_for_prompt(self, payer_id: str, limit_calls: int = 5) -> str:
        contacts = self.get_contacts(payer_id)
        calls = self.recent_calls(payer_id, limit=limit_calls)
        objections = self.recurring_objections(payer_id)
        open_promises = self.open_promises(payer_id)
        broken = self.broken_promise_count(payer_id)

        parts: list[str] = []

        if contacts:
            contact_lines = [
                f"- {c.name}"
                + (f" ({c.role})" if c.role else "")
                + (f", prefers {c.preferred_time}" if c.preferred_time else "")
                for c in contacts
            ]
            parts.append("Known contacts:\n" + "\n".join(contact_lines))

        if calls:
            call_lines = [
                f"- {c.occurred_at.date().isoformat()}: {c.summary} ({c.outcome.value})"
                for c in calls
            ]
            parts.append(f"Recent {len(calls)} calls:\n" + "\n".join(call_lines))
        else:
            parts.append("No prior contact recorded.")

        if open_promises:
            promise_lines = [
                f"- promised {p.promised_amount or 'unspecified'} by"
                f" {p.promised_date.date().isoformat()}"
                for p in open_promises
            ]
            parts.append("Open payment promises:\n" + "\n".join(promise_lines))

        if broken > 0:
            parts.append(f"Broken promises to date: {broken}.")

        if objections:
            parts.append("Recurring objections: " + ", ".join(objections) + ".")

        return "\n\n".join(parts)
