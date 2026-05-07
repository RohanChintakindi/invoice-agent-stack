"""FastAPI server exposing:

  POST /v1/chat/completions   OpenAI-compatible endpoint Vapi calls each
                              turn (Custom LLM mode).
  POST /webhooks/vapi         End-of-call hook: persist final summary,
                              update trust engine, mark promises kept,
                              etc.
  GET  /health                Liveness probe.
  GET  /sessions              List active call sessions (debug).
  GET  /sessions/{call_id}    Inspect one session: state, transitions,
                              compliance trail (powers the debug panel
                              during the demo).

Vapi Custom LLM integration: configure your Vapi assistant to use a
custom OpenAI-style LLM URL pointing at this server. Vapi sends the
caller transcript on each turn; we respond with the assistant's next
utterance. Vapi handles all audio.

The payer being called is identified per request via metadata. There
are three ways to set it (checked in order):

  1. JSON body field `payer_id` (demo / CLI)
  2. Vapi's `call.metadata.payer_id`
  3. A line `[payer_id=...]` inside the system message
"""

from __future__ import annotations

import os
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from shared.db import init_schema, make_engine, session_scope
from shared.models import Payer
from shared.trust_engine import TrustEngine
from voice_agent.llm import AnthropicClient, FakeLLMClient, LLMClient
from voice_agent.memory import PayerMemory
from voice_agent.orchestrator import build_graph, run_turn
from voice_agent.session import CallSession, SessionStore
from voice_agent.state_machine import PayerContext, State, initial_state


# ---- Request / response models ----------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "iridium-collections-agent"
    messages: list[ChatMessage]
    stream: bool = False
    payer_id: str | None = None
    invoice_id: str | None = None
    invoice_facts: str | None = None
    call: dict | None = None  # Vapi packs call_id + metadata here


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: dict = Field(default_factory=lambda: {})
    # Non-OpenAI fields for the debug panel.
    debug: dict | None = None


class WebhookEvent(BaseModel):
    type: str
    call: dict | None = None
    message: dict | None = None
    transcript: str | None = None


# ---- App state --------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    fake_mode = os.getenv("VOICE_AGENT_FAKE_LLM", "0") == "1"
    if fake_mode:
        app.state.llm = FakeLLMClient()
    else:
        app.state.llm = AnthropicClient()

    app.state.graph = build_graph(app.state.llm)
    app.state.sessions = SessionStore()
    app.state.engine = make_engine()
    init_schema(app.state.engine)
    yield


app = FastAPI(title="Iridium Voice Agent", lifespan=lifespan)


# ---- Helpers ----------------------------------------------------------------


_PAYER_TAG = re.compile(r"\[payer_id\s*=\s*([A-Za-z0-9_-]+)\]")


def extract_payer_id(req: ChatCompletionRequest) -> str | None:
    if req.payer_id:
        return req.payer_id
    if req.call and isinstance(req.call, dict):
        meta = req.call.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("payer_id"):
            return meta["payer_id"]
    for msg in req.messages:
        if msg.role == "system":
            m = _PAYER_TAG.search(msg.content)
            if m:
                return m.group(1)
    return None


def extract_call_id(req: ChatCompletionRequest) -> str:
    if req.call and isinstance(req.call, dict) and req.call.get("id"):
        return req.call["id"]
    return f"local-{uuid.uuid4()}"


def load_context(session: Session, payer_id: str, days_overdue: int = 0) -> tuple[
    PayerContext, str, list[str]
]:
    """Load PayerContext + memory summary from DB."""
    trust = TrustEngine(session)
    mem = PayerMemory(session)

    score = trust.get_trust(payer_id)
    broken = mem.broken_promise_count(payer_id)
    has_promise = mem.has_active_promise(payer_id)
    summary = mem.summarize_for_prompt(payer_id)

    ctx = PayerContext(
        trust_score=score,
        days_overdue=days_overdue,
        broken_promises=broken,
        has_active_promise=has_promise,
    )
    return ctx, summary


def find_user_message(messages: list[ChatMessage]) -> str:
    for msg in reversed(messages):
        if msg.role == "user":
            return msg.content
    return ""


def messages_to_history(messages: list[ChatMessage]) -> list[dict]:
    """Strip system messages; keep only user/assistant pairs prior to the
    final user message (which the orchestrator adds back itself)."""
    history: list[dict] = []
    for msg in messages:
        if msg.role in ("user", "assistant"):
            history.append({"role": msg.role, "content": msg.content})
    if history and history[-1]["role"] == "user":
        history.pop()
    return history


# ---- Routes -----------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(req: ChatCompletionRequest) -> ChatCompletionResponse:
    payer_id = extract_payer_id(req)
    if not payer_id:
        raise HTTPException(
            status_code=400,
            detail="payer_id required (body, call.metadata, or [payer_id=...] system tag).",
        )

    call_id = extract_call_id(req)
    user_message = find_user_message(req.messages)
    if not user_message:
        raise HTTPException(status_code=400, detail="no user message found")

    sessions: SessionStore = app.state.sessions
    session = sessions.get(call_id)

    if session is None:
        # First turn for this call — bootstrap from DB.
        with session_scope(app.state.engine) as db_session:
            ctx, summary = load_context(db_session, payer_id)
        invoice_facts = req.invoice_facts or "(invoice facts not provided)"
        call_state = initial_state(ctx)
        session = CallSession(
            call_id=call_id,
            payer_id=payer_id,
            invoice_id=req.invoice_id,
            invoice_facts=invoice_facts,
            memory_summary=summary,
            payer_context=ctx,
            call_state=call_state,
            history=messages_to_history(req.messages),
        )
        sessions.put(session)

    graph = app.state.graph
    result = run_turn(
        graph,
        payer_context=session.payer_context,
        payer_id=session.payer_id,
        user_message=user_message,
        invoice_facts=session.invoice_facts,
        memory_summary=session.memory_summary,
        history=session.history,
        call_state=session.call_state,
        transition_log=session.transition_log,
    )

    # Update session for next turn.
    session.call_state = result["call_state"]
    session.transition_log = result["transition_log"]
    session.history = list(session.history)
    session.history.append({"role": "user", "content": user_message})
    session.history.append({"role": "assistant", "content": result["final_response"]})
    sessions.put(session)

    debug = {
        "phase": result["call_state"].phase.value,
        "tone": result["call_state"].tone.value,
        "branch_action": result["branch_action"],
        "compliance_verdict": result["compliance_verdict"],
        "compliance_rationale": result.get("compliance_rationale", ""),
        "detected_signals": [s.value for s in result["detected_signals"]],
        "transition_log": result["transition_log"],
        "retry_count": result.get("retry_count", 0),
    }

    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4()}",
        created=int(time.time()),
        model=req.model,
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=result["final_response"])
            )
        ],
        debug=debug,
    )


@app.post("/webhooks/vapi")
async def vapi_webhook(event: WebhookEvent) -> dict:
    """End-of-call hook. For now: drops the session. Will later persist
    the final call summary, mark promises, and update the trust engine."""
    if event.type == "end-of-call-report" and event.call and event.call.get("id"):
        app.state.sessions.remove(event.call["id"])
    return {"received": True}


@app.get("/sessions")
async def list_sessions() -> dict:
    return {"call_ids": app.state.sessions.all_call_ids()}


@app.get("/sessions/{call_id}")
async def get_session(call_id: str) -> dict:
    session = app.state.sessions.get(call_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "call_id": session.call_id,
        "payer_id": session.payer_id,
        "invoice_id": session.invoice_id,
        "phase": session.call_state.phase.value,
        "tone": session.call_state.tone.value,
        "transition_log": session.transition_log,
        "history": session.history,
    }
