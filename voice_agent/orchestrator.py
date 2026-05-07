"""LangGraph orchestrator for the voice agent (L2).

Per-turn workflow:

    user utterance arrives
            │
            ▼
    classify_signals    (hard regex + LLM sentiment in one node)
            │
            ▼
    apply_transitions   (run state-machine intra-call transitions)
            │
            ▼
    route_branch        (conditional: respond | dispute | callback | handoff)
            │
        ┌───┴────────────┬──────────────┬──────────────┐
        ▼                ▼              ▼              ▼
     respond         dispute         callback        handoff
        │                │              │              │
        └───────┬────────┴──────────────┴──────────────┘
                ▼
         compliance_check
                │
        ┌───────┴───────┐
        ▼               ▼
    finalize        respond  (retry once with stricter directive)
        │
        ▼
       END

The graph state is a flat TypedDict so it's easy to inspect mid-call
(useful for the debug panel in the demo).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from voice_agent.compliance import (
    CONSISTENCY_PROMPT,
    Verdict,
    combine,
    parse_consistency_response,
    regex_screen,
)
from voice_agent.llm import LLMClient
from voice_agent.prompts import render_system_prompt
from voice_agent.signals import (
    SENTIMENT_PROMPT,
    classify_hard_signal,
    parse_sentiment_response,
    sentiment_to_signal,
)
from voice_agent.state_machine import (
    IntraCallSignal,
    PayerContext,
    State,
    initial_state,
    transition_intra_call,
)


# ---- Workflow state ---------------------------------------------------------


class WorkflowState(TypedDict, total=False):
    # Inputs (set before invoking the graph)
    payer_id: str
    invoice_facts: str
    memory_summary: str
    payer_context: PayerContext
    history: list[dict]  # OpenAI-format chat history
    user_message: str

    # Per-turn working state
    call_state: State
    detected_signals: list[IntraCallSignal]
    branch_action: str  # respond | dispute | callback | handoff
    draft_response: str
    compliance_verdict: str
    compliance_rationale: str
    final_response: str
    retry_count: int

    # Debug / inspection
    transition_log: list[str]


BRANCH_ACTIONS = ("respond", "dispute", "callback", "handoff")
MAX_RETRIES = 1


# ---- Node implementations ---------------------------------------------------


def make_classify_signals(llm: LLMClient):
    def node(state: WorkflowState) -> dict:
        text = state["user_message"]
        signals: list[IntraCallSignal] = []

        hard = classify_hard_signal(text)
        if hard is not None:
            signals.append(hard)

        # Soft sentiment via LLM. Skip if a hard signal already commits us.
        if hard not in (
            IntraCallSignal.BANKRUPTCY_MENTIONED,
            IntraCallSignal.DISPUTE_MENTIONED,
            IntraCallSignal.MANAGER_REQUESTED,
            IntraCallSignal.PAYMENT_COMMITTED,
        ):
            raw = llm.complete(
                system="You classify caller sentiment for a collections agent.",
                user=SENTIMENT_PROMPT.format(text=text),
                max_tokens=100,
            )
            sentiment = parse_sentiment_response(raw)
            soft = sentiment_to_signal(sentiment)
            if soft is not None and soft != hard:
                signals.append(soft)

        return {"detected_signals": signals}

    return node


def apply_transitions_node(state: WorkflowState) -> dict:
    current = state.get("call_state") or initial_state(state["payer_context"])
    log = list(state.get("transition_log", []))

    for signal in state.get("detected_signals", []):
        nxt = transition_intra_call(current, signal)
        log.append(
            f"{signal.value}: ({current.phase.value}, {current.tone.value}) → "
            f"({nxt.phase.value}, {nxt.tone.value}) — {nxt.reason}"
        )
        current = nxt

    return {"call_state": current, "transition_log": log}


def route_branch_node(state: WorkflowState) -> dict:
    signals = state.get("detected_signals", [])
    branch = "respond"
    if IntraCallSignal.BANKRUPTCY_MENTIONED in signals:
        branch = "handoff"
    elif IntraCallSignal.DISPUTE_MENTIONED in signals:
        branch = "dispute"
    elif IntraCallSignal.MANAGER_REQUESTED in signals:
        branch = "callback"
    return {"branch_action": branch}


def make_respond(llm: LLMClient):
    def node(state: WorkflowState) -> dict:
        extra = None
        if state.get("compliance_verdict") == Verdict.BLOCK.value:
            extra = (
                "Your previous draft was blocked by compliance. "
                f"Reason: {state.get('compliance_rationale', '')}. "
                "Rewrite the response without any threats, urgency claims, or "
                "third-party disclosure. Stay in the assigned tone."
            )

        system = render_system_prompt(
            state=state["call_state"],
            memory_summary=state["memory_summary"],
            invoice_facts=state["invoice_facts"],
            extra_directive=extra,
        )
        history = list(state.get("history", []))
        history.append({"role": "user", "content": state["user_message"]})
        text = llm.chat(system=system, messages=history, max_tokens=300)
        return {"draft_response": text}

    return node


def dispute_handler_node(state: WorkflowState) -> dict:
    text = (
        "Thank you for flagging that. I want to make sure we resolve any "
        "discrepancy before anything else. Could you tell me what's "
        "incorrect — the amount, the invoice itself, or whether it's "
        "already been paid? I'll loop in someone to investigate and email "
        "you a summary today."
    )
    return {"draft_response": text}


def callback_handler_node(state: WorkflowState) -> dict:
    text = (
        "Of course — what's a good time for me to follow up with them "
        "directly? I'll send you a quick email confirming what we discussed "
        "so they have it in writing before our next call."
    )
    return {"draft_response": text}


def handoff_handler_node(state: WorkflowState) -> dict:
    text = (
        "I appreciate you sharing that, and I don't want to add stress to a "
        "difficult situation. Let me have one of our specialists reach out "
        "directly to discuss next steps that work for you. Is email or "
        "phone better for that conversation?"
    )
    return {"draft_response": text}


def make_compliance_check(llm: LLMClient):
    def node(state: WorkflowState) -> dict:
        text = state["draft_response"]
        regex_result = regex_screen(text)

        # Skip the LLM check if the regex pass already says block — the regex
        # is precise enough on threats; LLM is for subtler issues.
        if regex_result.verdict == Verdict.BLOCK:
            return {
                "compliance_verdict": regex_result.verdict.value,
                "compliance_rationale": regex_result.rationale,
            }

        raw = llm.complete(
            system="You are a compliance reviewer for collections-call output.",
            user=CONSISTENCY_PROMPT.format(text=text),
            max_tokens=80,
        )
        llm_result = parse_consistency_response(raw)
        merged = combine(regex_result, llm_result)
        return {
            "compliance_verdict": merged.verdict.value,
            "compliance_rationale": merged.rationale,
        }

    return node


def finalize_node(state: WorkflowState) -> dict:
    verdict = state.get("compliance_verdict", Verdict.PASS.value)
    if verdict == Verdict.BLOCK.value:
        # Last-ditch safe canned response; orchestrator-level fallback.
        text = (
            "Thanks for taking the call. I'd like to put you in touch with one "
            "of our specialists who can walk through next steps. Could I get "
            "an email address to send a follow-up?"
        )
    else:
        text = state["draft_response"]
    return {"final_response": text}


# ---- Routing helpers --------------------------------------------------------


def _route_after_branch(state: WorkflowState) -> Literal["respond", "dispute", "callback", "handoff"]:
    return state.get("branch_action", "respond")  # type: ignore[return-value]


def _route_after_compliance(state: WorkflowState) -> Literal["finalize", "respond"]:
    if (
        state.get("compliance_verdict") == Verdict.BLOCK.value
        and state.get("retry_count", 0) < MAX_RETRIES
    ):
        return "respond"
    return "finalize"


def _bump_retry(state: WorkflowState) -> dict:
    return {"retry_count": state.get("retry_count", 0) + 1}


# ---- Graph factory ----------------------------------------------------------


def build_graph(llm: LLMClient):
    graph = StateGraph(WorkflowState)

    graph.add_node("classify_signals", make_classify_signals(llm))
    graph.add_node("apply_transitions", apply_transitions_node)
    graph.add_node("route_branch", route_branch_node)
    graph.add_node("respond", make_respond(llm))
    graph.add_node("dispute", dispute_handler_node)
    graph.add_node("callback", callback_handler_node)
    graph.add_node("handoff", handoff_handler_node)
    graph.add_node("compliance_check", make_compliance_check(llm))
    graph.add_node("bump_retry", _bump_retry)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "classify_signals")
    graph.add_edge("classify_signals", "apply_transitions")
    graph.add_edge("apply_transitions", "route_branch")
    graph.add_conditional_edges(
        "route_branch",
        _route_after_branch,
        {
            "respond": "respond",
            "dispute": "dispute",
            "callback": "callback",
            "handoff": "handoff",
        },
    )
    for branch in ("respond", "dispute", "callback", "handoff"):
        graph.add_edge(branch, "compliance_check")
    graph.add_conditional_edges(
        "compliance_check",
        _route_after_compliance,
        {"respond": "bump_retry", "finalize": "finalize"},
    )
    graph.add_edge("bump_retry", "respond")
    graph.add_edge("finalize", END)

    return graph.compile()


# ---- Convenience ------------------------------------------------------------


def run_turn(graph, *, payer_context: PayerContext, **inputs) -> dict:
    """Run one turn through the compiled graph; returns the final state dict."""
    state: WorkflowState = {
        "payer_context": payer_context,
        "history": inputs.get("history", []),
        "user_message": inputs["user_message"],
        "invoice_facts": inputs.get("invoice_facts", ""),
        "memory_summary": inputs.get("memory_summary", ""),
        "payer_id": inputs.get("payer_id", ""),
        "call_state": inputs.get("call_state") or initial_state(payer_context),
        "detected_signals": [],
        "transition_log": inputs.get("transition_log", []),
        "retry_count": 0,
    }
    return graph.invoke(state)


def state_to_dict(state: State) -> dict:
    return asdict(state)
