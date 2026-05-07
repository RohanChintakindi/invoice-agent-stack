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

from dataclasses import asdict, dataclass
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from voice_agent.compliance import (
    CONSISTENCY_PROMPT,
    ComplianceResult,
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


# ---- Canned branch responses ------------------------------------------------
# Used by the dispute / callback / handoff branches when we don't want the LLM
# to free-form. Streaming path emits these as a single SSE chunk.

CANNED_DISPUTE = (
    "Thank you for flagging that. I want to make sure we resolve any "
    "discrepancy before anything else. Could you tell me what's "
    "incorrect — the amount, the invoice itself, or whether it's "
    "already been paid? I'll loop in someone to investigate and email "
    "you a summary today."
)

CANNED_CALLBACK = (
    "Of course — what's a good time for me to follow up with them "
    "directly? I'll send you a quick email confirming what we discussed "
    "so they have it in writing before our next call."
)

CANNED_HANDOFF = (
    "I appreciate you sharing that, and I don't want to add stress to a "
    "difficult situation. Let me have one of our specialists reach out "
    "directly to discuss next steps that work for you. Is email or "
    "phone better for that conversation?"
)

COMPLIANCE_FALLBACK = (
    "Thanks for taking the call. I'd like to put you in touch with one "
    "of our specialists who can walk through next steps. Could I get "
    "an email address to send a follow-up?"
)

CANNED_BY_BRANCH = {
    "dispute": CANNED_DISPUTE,
    "callback": CANNED_CALLBACK,
    "handoff": CANNED_HANDOFF,
}


# ---- Pure stages (callable from both graph and streaming path) --------------


# Hard signals deterministically resolved by regex — fast (microseconds) and
# safe to run on the latency-critical path. Soft sentiment requires an LLM
# call and is the slowest part of pre-response (~300-500ms with Haiku); the
# streaming path defers it to a background task.

_HARD_TERMINAL = (
    IntraCallSignal.BANKRUPTCY_MENTIONED,
    IntraCallSignal.DISPUTE_MENTIONED,
    IntraCallSignal.MANAGER_REQUESTED,
    IntraCallSignal.PAYMENT_COMMITTED,
)


def classify_hard_signals(user_message: str) -> list[IntraCallSignal]:
    """Sync, regex-only signal extraction. ~1ms. Used by both paths."""
    hard = classify_hard_signal(user_message)
    return [hard] if hard is not None else []


def classify_soft_signal(
    llm: LLMClient, user_message: str, hard_signals: list[IntraCallSignal]
) -> IntraCallSignal | None:
    """LLM sentiment classifier. Skipped when a hard signal already commits
    the call to a specific branch (those branches don't need soft tone)."""
    if any(h in _HARD_TERMINAL for h in hard_signals):
        return None
    raw = llm.complete(
        system="You classify caller sentiment for a collections agent.",
        user=SENTIMENT_PROMPT.format(text=user_message),
        max_tokens=100,
    )
    sentiment = parse_sentiment_response(raw)
    soft = sentiment_to_signal(sentiment)
    if soft is not None and soft not in hard_signals:
        return soft
    return None


def classify_signals(llm: LLMClient, user_message: str) -> list[IntraCallSignal]:
    """Combined hard + soft classification — used by the non-streaming graph."""
    hard = classify_hard_signals(user_message)
    soft = classify_soft_signal(llm, user_message, hard)
    return hard + ([soft] if soft is not None else [])


def apply_signals(
    call_state: State,
    signals: list[IntraCallSignal],
    transition_log: list[str] | None = None,
) -> tuple[State, list[str]]:
    current = call_state
    log = list(transition_log or [])
    for signal in signals:
        nxt = transition_intra_call(current, signal)
        log.append(
            f"{signal.value}: ({current.phase.value}, {current.tone.value}) → "
            f"({nxt.phase.value}, {nxt.tone.value}) — {nxt.reason}"
        )
        current = nxt
    return current, log


def decide_branch(signals: list[IntraCallSignal]) -> str:
    if IntraCallSignal.BANKRUPTCY_MENTIONED in signals:
        return "handoff"
    if IntraCallSignal.DISPUTE_MENTIONED in signals:
        return "dispute"
    if IntraCallSignal.MANAGER_REQUESTED in signals:
        return "callback"
    return "respond"


def check_compliance(llm: LLMClient, text: str) -> ComplianceResult:
    """Two-pass compliance: regex screen, then LLM consistency check.

    Skips the LLM call when regex already says BLOCK (regex is precise on
    threats; LLM is for subtler issues that regex misses).
    """
    regex_result = regex_screen(text)
    if regex_result.verdict == Verdict.BLOCK:
        return regex_result
    raw = llm.complete(
        system="You are a compliance reviewer for collections-call output.",
        user=CONSISTENCY_PROMPT.format(text=text),
        max_tokens=80,
    )
    llm_result = parse_consistency_response(raw)
    return combine(regex_result, llm_result)


@dataclass
class PreResponseResult:
    detected_signals: list[IntraCallSignal]
    call_state: State
    transition_log: list[str]
    branch_action: str


def run_pre_response(
    llm: LLMClient,
    *,
    payer_context: PayerContext,
    call_state: State | None,
    user_message: str,
    transition_log: list[str] | None = None,
) -> PreResponseResult:
    """Stage 1: classify signals, apply transitions, decide branch.

    Synchronous and small (one LLM call for sentiment classification when
    no hard signal triggers). Used by the graph and the streaming server
    path so signal handling stays consistent across both."""
    starting_state = call_state or initial_state(payer_context)
    signals = classify_signals(llm, user_message)
    new_state, log = apply_signals(starting_state, signals, transition_log)
    branch = decide_branch(signals)
    return PreResponseResult(
        detected_signals=signals,
        call_state=new_state,
        transition_log=log,
        branch_action=branch,
    )


def run_pre_response_fast(
    *,
    payer_context: PayerContext,
    call_state: State | None,
    user_message: str,
    transition_log: list[str] | None = None,
) -> PreResponseResult:
    """Latency-critical version of run_pre_response.

    Skips the soft-sentiment LLM call (~300-500ms) — hard signals alone
    decide the branch, since soft sentiment only ever affects state
    transitions, not branch routing. The caller is expected to schedule
    `classify_soft_signal` as a background task whose result mutates
    state for the *next* turn. Cuts pre-response work from ~500ms to
    ~1ms on the hot path."""
    starting_state = call_state or initial_state(payer_context)
    signals = classify_hard_signals(user_message)
    new_state, log = apply_signals(starting_state, signals, transition_log)
    branch = decide_branch(signals)
    return PreResponseResult(
        detected_signals=signals,
        call_state=new_state,
        transition_log=log,
        branch_action=branch,
    )


def run_post_response(llm: LLMClient, text: str) -> ComplianceResult:
    """Stage 3: compliance review of an outgoing utterance.

    In the non-streaming path this can BLOCK and trigger a retry. In the
    streaming path tokens have already shipped, so the verdict is
    observed-and-logged only — used to flag turns in the debug panel and
    to condition the next turn's system prompt.
    """
    return check_compliance(llm, text)


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
        return {"detected_signals": classify_signals(llm, state["user_message"])}

    return node


def apply_transitions_node(state: WorkflowState) -> dict:
    starting = state.get("call_state") or initial_state(state["payer_context"])
    new_state, log = apply_signals(
        starting, state.get("detected_signals", []), state.get("transition_log", [])
    )
    return {"call_state": new_state, "transition_log": log}


def route_branch_node(state: WorkflowState) -> dict:
    return {"branch_action": decide_branch(state.get("detected_signals", []))}


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
    return {"draft_response": CANNED_DISPUTE}


def callback_handler_node(state: WorkflowState) -> dict:
    return {"draft_response": CANNED_CALLBACK}


def handoff_handler_node(state: WorkflowState) -> dict:
    return {"draft_response": CANNED_HANDOFF}


def make_compliance_check(llm: LLMClient):
    def node(state: WorkflowState) -> dict:
        result = check_compliance(llm, state["draft_response"])
        return {
            "compliance_verdict": result.verdict.value,
            "compliance_rationale": result.rationale,
        }

    return node


def finalize_node(state: WorkflowState) -> dict:
    verdict = state.get("compliance_verdict", Verdict.PASS.value)
    text = COMPLIANCE_FALLBACK if verdict == Verdict.BLOCK.value else state["draft_response"]
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
