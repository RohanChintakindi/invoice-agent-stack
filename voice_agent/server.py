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

import asyncio
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session

from shared.db import init_schema, make_engine, session_scope
from shared.models import Payer
from shared.trust_engine import TrustEngine
from voice_agent.compliance import Verdict
from voice_agent.llm import AnthropicClient, FakeLLMClient, LLMClient
from voice_agent.memory import PayerMemory
from voice_agent.orchestrator import (
    CANNED_BY_BRANCH,
    COMPLIANCE_FALLBACK,
    apply_signals,
    build_graph,
    classify_soft_signal,
    run_post_response,
    run_pre_response,
    run_pre_response_fast,
    run_turn,
)
from voice_agent.prompts import render_system_prompt
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


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    # Debug: dump request shape so we can see what Vapi actually sends.
    # Strip message contents to avoid log spam; just role + length.
    msg_summary = [{"role": m.role, "len": len(m.content)} for m in req.messages]
    call_meta = (req.call or {}).get("metadata") if isinstance(req.call, dict) else None
    print(
        f"[voice] /v1/chat/completions stream={req.stream} payer_id={req.payer_id} "
        f"messages={msg_summary} call_meta={call_meta}",
        flush=True,
    )

    payer_id = extract_payer_id(req)
    if not payer_id:
        # Log first system message to see if {{payer_id}} got templated.
        first_sys = next((m.content for m in req.messages if m.role == "system"), "")
        print(f"[voice] payer_id extraction failed; first system msg: {first_sys[:200]!r}", flush=True)
        raise HTTPException(
            status_code=400,
            detail="payer_id required (body, call.metadata, or [payer_id=...] system tag).",
        )

    call_id = extract_call_id(req)
    user_message = find_user_message(req.messages)
    if not user_message:
        print(f"[voice] no user message; payer={payer_id} messages={msg_summary}", flush=True)
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

    if req.stream:
        return await _streaming_chat(req, sessions, session, user_message)

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


async def _streaming_chat(
    req: ChatCompletionRequest,
    sessions: SessionStore,
    session: CallSession,
    user_message: str,
) -> StreamingResponse:
    """SSE streaming path used when the caller (Vapi) sets stream=true.

    Same three-stage pipeline as the non-streaming path, but the response
    generation step streams Anthropic deltas directly into SSE chunks.
    Compliance runs post-stream as observation only (tokens already shipped
    to TTS — see run_post_response docstring).
    """
    llm: LLMClient = app.state.llm

    # Latency-critical path: hard-signal classification only (~1ms) so we can
    # start streaming Anthropic immediately. Soft sentiment runs as a
    # background task (see _async_soft_sentiment) — its state transitions
    # apply to the next turn instead of blocking this one.
    pre = run_pre_response_fast(
        payer_context=session.payer_context,
        call_state=session.call_state,
        user_message=user_message,
        transition_log=session.transition_log,
    )

    stream_system: str | None = None
    stream_history: list[dict] | None = None
    canned_text: str | None = None
    if pre.branch_action == "respond":
        stream_system = render_system_prompt(
            state=pre.call_state,
            memory_summary=session.memory_summary,
            invoice_facts=session.invoice_facts,
        )
        stream_history = list(session.history) + [
            {"role": "user", "content": user_message}
        ]
    else:
        canned_text = CANNED_BY_BRANCH[pre.branch_action]

    completion_id = f"chatcmpl-{uuid.uuid4()}"
    created = int(time.time())
    model_name = req.model

    def chunk(delta: dict, finish_reason: str | None = None) -> str:
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason}
            ],
        }
        return f"data: {json.dumps(payload)}\n\n"

    async def sse_gen():
        full_text = ""
        try:
            yield chunk({"role": "assistant"})
            if canned_text is not None:
                full_text = canned_text
                yield chunk({"content": canned_text})
            else:
                async for delta in llm.astream_chat(
                    system=stream_system, messages=stream_history, max_tokens=300
                ):
                    full_text += delta
                    yield chunk({"content": delta})
        finally:
            yield chunk({}, finish_reason="stop")
            yield "data: [DONE]\n\n"

            # Fast session updates first — the next turn may arrive immediately
            # and needs the new state machine + history.
            session.call_state = pre.call_state
            session.transition_log = pre.transition_log
            new_history = list(session.history)
            new_history.append({"role": "user", "content": user_message})
            new_history.append({"role": "assistant", "content": full_text})
            session.history = new_history
            session.last_compliance = {
                "verdict": "pending",
                "rationale": "",
                "branch": pre.branch_action,
                "text": full_text,
            }
            sessions.put(session)

            # Compliance as an async guardrail. Tokens have already shipped;
            # we don't block the stream on the verdict. The background task
            # updates session.last_compliance when it lands, so the next-turn
            # system prompt and the debug panel can both see it.
            if full_text:
                asyncio.create_task(
                    _async_compliance(llm, sessions, session, full_text, pre.branch_action)
                )

            # Soft sentiment also runs in the background, since we skipped it
            # on the hot path. Its state transitions apply to the *next* turn.
            asyncio.create_task(
                _async_soft_sentiment(
                    llm, sessions, session, user_message, pre.detected_signals
                )
            )

    return StreamingResponse(sse_gen(), media_type="text/event-stream")


async def _async_compliance(
    llm: LLMClient,
    sessions: SessionStore,
    session: CallSession,
    text: str,
    branch: str,
) -> None:
    """Background guardrail. Runs compliance off the event loop (the LLM
    consistency call is sync) and stores the verdict back on the session.
    Never raises — failures degrade to a logged error verdict."""
    try:
        result = await asyncio.to_thread(run_post_response, llm, text)
        verdict = result.verdict.value
        rationale = result.rationale
    except Exception as exc:
        verdict = "error"
        rationale = f"compliance check raised: {exc}"
    session.last_compliance = {
        "verdict": verdict,
        "rationale": rationale,
        "branch": branch,
        "text": text,
    }
    sessions.put(session)


async def _async_soft_sentiment(
    llm: LLMClient,
    sessions: SessionStore,
    session: CallSession,
    user_message: str,
    hard_signals: list,
) -> None:
    """Background sentiment classifier. The streaming path skipped the
    soft-sentiment LLM call to start speaking sooner; this picks it up
    afterward and folds the resulting state transition into the session
    so the *next* turn's system prompt sees the updated phase/tone.
    Never raises."""
    try:
        soft = await asyncio.to_thread(
            classify_soft_signal, llm, user_message, hard_signals
        )
    except Exception:
        return
    if soft is None:
        return
    new_state, new_log = apply_signals(session.call_state, [soft], session.transition_log)
    session.call_state = new_state
    session.transition_log = new_log
    sessions.put(session)


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
