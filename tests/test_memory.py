from datetime import datetime, timedelta, timezone

from voice_agent.memory import PayerMemory
from voice_agent.memory_models import CallOutcome


def test_record_and_retrieve_call(session, payer):
    mem = PayerMemory(session)
    call = mem.record_call(
        payer_id=payer,
        summary="Karen confirmed she'll route to AP this week.",
        outcome=CallOutcome.PROMISE_MADE,
        contact_name="Karen",
    )
    assert call.id is not None

    calls = mem.recent_calls(payer)
    assert len(calls) == 1
    assert calls[0].summary.startswith("Karen confirmed")


def test_open_promise_count_and_active_flag(session, payer):
    mem = PayerMemory(session)
    promised_date = datetime.now(timezone.utc) + timedelta(days=7)
    mem.record_promise(payer_id=payer, promised_date=promised_date, promised_amount=12000)
    assert mem.has_active_promise(payer) is True
    assert mem.broken_promise_count(payer) == 0


def test_broken_promise_increments_count(session, payer):
    mem = PayerMemory(session)
    promised_date = datetime.now(timezone.utc) + timedelta(days=7)
    p = mem.record_promise(payer_id=payer, promised_date=promised_date)
    mem.resolve_promise(p.id, kept=False)
    assert mem.broken_promise_count(payer) == 1
    assert mem.has_active_promise(payer) is False


def test_kept_promise_does_not_count_as_broken(session, payer):
    mem = PayerMemory(session)
    promised_date = datetime.now(timezone.utc) + timedelta(days=7)
    p = mem.record_promise(payer_id=payer, promised_date=promised_date)
    mem.resolve_promise(p.id, kept=True)
    assert mem.broken_promise_count(payer) == 0


def test_recurring_objections_requires_two_occurrences(session, payer):
    mem = PayerMemory(session)
    mem.record_objection(payer, "approvals_delay", "Stuck in AP approvals queue.")
    assert "approvals_delay" not in mem.recurring_objections(payer)
    mem.record_objection(payer, "approvals_delay", "AP says it's still being approved.")
    assert "approvals_delay" in mem.recurring_objections(payer)


def test_summarize_for_prompt_includes_all_signals(session, payer):
    mem = PayerMemory(session)
    mem.add_contact(payer, "Karen", role="AP", preferred_time="mornings")
    mem.record_call(
        payer_id=payer,
        summary="Karen blamed approvals process.",
        outcome=CallOutcome.PARTIAL_PROMISE,
        contact_name="Karen",
    )
    promised_date = datetime.now(timezone.utc) + timedelta(days=7)
    p = mem.record_promise(payer_id=payer, promised_date=promised_date, promised_amount=12000)
    mem.resolve_promise(p.id, kept=False)
    mem.record_objection(payer, "approvals_delay", "stuck in AP")
    mem.record_objection(payer, "approvals_delay", "AP queue")

    summary = mem.summarize_for_prompt(payer)
    assert "Karen" in summary
    assert "AP" in summary
    assert "Broken promises" in summary
    assert "approvals_delay" in summary


def test_summarize_for_prompt_handles_empty_payer(session, payer):
    mem = PayerMemory(session)
    summary = mem.summarize_for_prompt(payer)
    assert "No prior contact" in summary
