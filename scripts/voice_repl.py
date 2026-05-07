"""Text-mode REPL that drives the voice-agent orchestrator without Vapi.

Use this to iterate on the agent, demo it without phone audio, and as a
fallback if Vapi has issues during the live demo.

Each turn prints a small debug panel showing the (phase, tone) state,
detected signals, branch action, and compliance verdict — the exact
panel we'll mirror in the dashboard.

Run with:
    uv run python -m scripts.voice_repl --payer acme --invoice INV-1023 \
        --days-overdue 28 --invoice-facts "INV-1023, $12,000"
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap

from shared.db import init_schema, make_engine, session_scope
from shared.models import Payer
from shared.trust_engine import TrustEngine
from voice_agent.llm import AnthropicClient, FakeLLMClient
from voice_agent.memory import PayerMemory
from voice_agent.orchestrator import build_graph, run_turn
from voice_agent.state_machine import PayerContext, initial_state


def _wrap(text: str, width: int = 80) -> str:
    return "\n".join(textwrap.wrap(text, width=width)) or text


def _panel(title: str, lines: list[str]) -> str:
    bar = "─" * 78
    body = "\n".join(f"│ {line}" for line in lines)
    return f"┌{bar}\n│ {title}\n├{bar}\n{body}\n└{bar}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payer", default="acme", help="payer_id")
    parser.add_argument("--invoice", default="INV-1023", help="invoice_id")
    parser.add_argument("--days-overdue", type=int, default=14)
    parser.add_argument(
        "--invoice-facts",
        default="INV-1023, $12,000, net-30 terms, 14 days overdue.",
    )
    parser.add_argument(
        "--fake-llm",
        action="store_true",
        help="use the FakeLLMClient (no Anthropic key needed; deterministic responses)",
    )
    args = parser.parse_args()

    fake = args.fake_llm or os.getenv("VOICE_AGENT_FAKE_LLM") == "1"
    if fake:
        llm = FakeLLMClient()
        print("[REPL] using FakeLLMClient — responses are canned.\n")
    else:
        if not os.getenv("ANTHROPIC_API_KEY"):
            print(
                "[REPL] ANTHROPIC_API_KEY not set. Use --fake-llm or set the key.",
                file=sys.stderr,
            )
            return 2
        llm = AnthropicClient()

    engine = make_engine()
    init_schema(engine)

    with session_scope(engine) as db:
        payer = db.get(Payer, args.payer)
        if payer is None:
            print(f"[REPL] payer '{args.payer}' not in DB. Run scripts/seed_demo.py first.")
            return 2
        trust = TrustEngine(db)
        mem = PayerMemory(db)
        score = trust.get_trust(args.payer)
        broken = mem.broken_promise_count(args.payer)
        has_promise = mem.has_active_promise(args.payer)
        memory_summary = mem.summarize_for_prompt(args.payer)

    payer_context = PayerContext(
        trust_score=score,
        days_overdue=args.days_overdue,
        broken_promises=broken,
        has_active_promise=has_promise,
    )

    graph = build_graph(llm)
    state = initial_state(payer_context)
    history: list[dict] = []
    transition_log: list[str] = []

    print(
        _panel(
            f"Call setup — {args.payer} ({args.invoice})",
            [
                f"trust_score      : {score:.2f}",
                f"days_overdue     : {args.days_overdue}",
                f"broken_promises  : {broken}",
                f"active_promise   : {has_promise}",
                f"initial state    : ({state.phase.value}, {state.tone.value})",
                f"reason           : {state.reason}",
            ],
        )
    )
    print()
    print("Type the payer's side of the conversation. Ctrl-D / 'exit' to quit.\n")

    while True:
        try:
            user = input(">> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user or user.lower() in {"exit", "quit"}:
            break

        result = run_turn(
            graph,
            payer_context=payer_context,
            payer_id=args.payer,
            user_message=user,
            invoice_facts=args.invoice_facts,
            memory_summary=memory_summary,
            history=history,
            call_state=state,
            transition_log=transition_log,
        )
        state = result["call_state"]
        transition_log = result["transition_log"]
        history.append({"role": "user", "content": user})
        history.append({"role": "assistant", "content": result["final_response"]})

        print()
        print(_wrap(f"agent: {result['final_response']}"))
        print()
        print(
            _panel(
                "debug",
                [
                    f"state          : ({state.phase.value}, {state.tone.value})",
                    f"signals        : {[s.value for s in result['detected_signals']]}",
                    f"branch         : {result['branch_action']}",
                    f"compliance     : {result['compliance_verdict']}"
                    + (
                        f" — {result.get('compliance_rationale', '')}"
                        if result.get("compliance_rationale")
                        else ""
                    ),
                    f"retries        : {result.get('retry_count', 0)}",
                ]
                + (
                    [f"transitions    : {transition_log[-1]}"] if transition_log else []
                ),
            )
        )
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
