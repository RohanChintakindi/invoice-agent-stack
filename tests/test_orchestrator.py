"""End-to-end tests for the LangGraph orchestrator with a fake LLM."""

from __future__ import annotations

from voice_agent.llm import FakeLLMClient
from voice_agent.orchestrator import build_graph, run_turn
from voice_agent.state_machine import IntraCallSignal, PayerContext, Phase, Tone


def _ctx(trust_score=0.5, days_overdue=10, broken_promises=0, has_active_promise=False):
    return PayerContext(
        trust_score=trust_score,
        days_overdue=days_overdue,
        broken_promises=broken_promises,
        has_active_promise=has_active_promise,
    )


# ---- Happy path: normal respond flow ----------------------------------------


def test_normal_turn_produces_compliant_response():
    llm = FakeLLMClient(
        complete_responses=[
            "sentiment: neutral\nconfidence: 0.7\n",  # sentiment classification
            "verdict: pass\nwhy: ok\n",  # compliance LLM check
        ],
        chat_responses=[
            "Could we agree on a payment date by Friday?",
        ],
    )
    graph = build_graph(llm)

    result = run_turn(
        graph,
        payer_context=_ctx(),
        user_message="Hey, what's this about?",
        invoice_facts="INV-1023, $12,000, 14 days overdue.",
        memory_summary="No prior contact recorded.",
    )

    assert result["branch_action"] == "respond"
    assert "Friday" in result["final_response"]
    assert result["compliance_verdict"] == "pass"


# ---- Branching: dispute, callback, handoff ----------------------------------


def test_dispute_phrase_routes_to_dispute_handler():
    llm = FakeLLMClient(complete_responses=["verdict: pass\n"])
    graph = build_graph(llm)

    result = run_turn(
        graph,
        payer_context=_ctx(),
        user_message="We never received that invoice.",
        invoice_facts="INV-1023.",
        memory_summary="",
    )

    assert result["branch_action"] == "dispute"
    assert "discrepancy" in result["final_response"].lower()


def test_manager_request_routes_to_callback_handler():
    llm = FakeLLMClient(complete_responses=["verdict: pass\n"])
    graph = build_graph(llm)

    result = run_turn(
        graph,
        payer_context=_ctx(),
        user_message="Let me ask my manager about that.",
        invoice_facts="INV-1023.",
        memory_summary="",
    )

    assert result["branch_action"] == "callback"
    assert "follow up" in result["final_response"].lower()


def test_bankruptcy_routes_to_handoff_and_moves_to_pre_legal():
    llm = FakeLLMClient(complete_responses=["verdict: pass\n"])
    graph = build_graph(llm)

    result = run_turn(
        graph,
        payer_context=_ctx(trust_score=0.7, days_overdue=20),
        user_message="We may have to file for bankruptcy.",
        invoice_facts="INV-1023.",
        memory_summary="",
    )

    assert result["branch_action"] == "handoff"
    assert result["call_state"].phase == Phase.PRE_LEGAL


# ---- Intra-call state transitions -------------------------------------------


def test_payment_commitment_moves_state_to_paused():
    llm = FakeLLMClient(complete_responses=["verdict: pass\n"])
    graph = build_graph(llm)

    result = run_turn(
        graph,
        payer_context=_ctx(),
        user_message="I'll pay it tomorrow.",
        invoice_facts="INV-1023.",
        memory_summary="",
    )

    assert result["call_state"].phase == Phase.PAUSED
    assert IntraCallSignal.PAYMENT_COMMITTED in result["detected_signals"]


def test_hostile_sentiment_softens_tone():
    llm = FakeLLMClient(
        complete_responses=[
            "sentiment: hostile\nconfidence: 0.9\nwhy: angry tone\n",
            "verdict: pass\n",
        ],
        chat_responses=["Understood — let's find a path that works."],
    )
    graph = build_graph(llm)

    result = run_turn(
        graph,
        payer_context=_ctx(trust_score=0.3),  # firm starting tone
        user_message="this is ridiculous, stop calling me",
        invoice_facts="INV-1023.",
        memory_summary="",
    )

    # Starting tone for trust=0.3 in firm_followup is FIRM; hostile softens to PROFESSIONAL
    assert result["call_state"].tone == Tone.PROFESSIONAL
    assert IntraCallSignal.HOSTILE_SENTIMENT in result["detected_signals"]


# ---- Compliance retry loop --------------------------------------------------


def test_blocked_response_triggers_retry_and_then_falls_back():
    # First respond returns a threat → regex blocks → retry.
    # Second respond also returns a threat → blocks again → finalize uses fallback.
    llm = FakeLLMClient(
        complete_responses=["sentiment: neutral\nconfidence: 0.7\n"],
        chat_responses=[
            "If you don't pay we'll sue you.",  # blocked
            "We'll take you to court if needed.",  # blocked again
        ],
    )
    graph = build_graph(llm)

    result = run_turn(
        graph,
        payer_context=_ctx(),
        user_message="What's this call about?",
        invoice_facts="INV-1023.",
        memory_summary="",
    )

    # Final response should be the fallback canned message.
    assert "specialist" in result["final_response"].lower()
    assert result["compliance_verdict"] == "block"
    assert result["retry_count"] == 1


def test_blocked_then_clean_response_succeeds():
    llm = FakeLLMClient(
        complete_responses=["sentiment: neutral\nconfidence: 0.7\n"],
        chat_responses=[
            "We'll sue you if you don't pay.",  # blocked first
            "Could we agree on a date this week?",  # clean retry
        ],
    )
    graph = build_graph(llm)

    result = run_turn(
        graph,
        payer_context=_ctx(),
        user_message="What's this call about?",
        invoice_facts="INV-1023.",
        memory_summary="",
    )

    assert "agree on a date" in result["final_response"].lower()
    assert result["compliance_verdict"] == "pass"
    assert result["retry_count"] == 1


# ---- Transition log -------------------------------------------------------


def test_transition_log_records_intra_call_changes():
    llm = FakeLLMClient(complete_responses=["verdict: pass\n"])
    graph = build_graph(llm)

    result = run_turn(
        graph,
        payer_context=_ctx(),
        user_message="I'll pay it tomorrow.",
        invoice_facts="INV-1023.",
        memory_summary="",
    )

    assert len(result["transition_log"]) >= 1
    assert any("payment_committed" in entry for entry in result["transition_log"])
