"""End-to-end demo for the browser orchestration vertical.

Enqueues two jobs (one to the happy portal, one to the silent-fail
portal), runs the worker tick for each, and prints a debug panel
mirroring what the dashboard would show.

Run:
    uv run python -m scripts.seed_demo       # creates payer "acme"
    uv run python -m scripts.seed_portals    # creates portals + creds
    uv run python -m scripts.browser_demo
"""

from __future__ import annotations

import os
import sys
import textwrap

from dotenv import load_dotenv

load_dotenv()

from browser_orchestration.harness_adapter import (
    MockHarness,
    script_clean_extraction,
    script_silent_fail,
)
from browser_orchestration.models import JobAction
from browser_orchestration.queue import SqliteJobQueue
from browser_orchestration.vault import CredentialVault, generate_key
from browser_orchestration.worker import process_one_job
from shared.db import init_schema, make_engine, session_scope
from shared.trust_engine import TrustEngine
from voice_agent.llm import FakeLLMClient


def _panel(title: str, lines: list[str]) -> str:
    bar = "-" * 78
    body = "\n".join(f"| {line}" for line in lines)
    return f"+{bar}\n| {title}\n+{bar}\n{body}\n+{bar}"


def _wrap(s: str, width: int = 76) -> str:
    return "\n".join(textwrap.wrap(s, width=width)) or s


def main() -> int:
    if not os.getenv("VAULT_KEY"):
        os.environ["VAULT_KEY"] = generate_key()
        print(f"[demo] generated ephemeral VAULT_KEY={os.environ['VAULT_KEY']}\n")

    engine = make_engine()
    init_schema(engine)
    vault = CredentialVault.from_env()

    # FakeLLM keeps the demo offline. Real demos would use AnthropicClient.
    llm = FakeLLMClient(
        complete_responses=[
            "verdict: pass\nconfidence: 0.9\nwhy: invoice table present\n",
            "verdict: fail\nconfidence: 0.85\nwhy: page shows session expired\n",
        ]
    )

    # Build a harness with both happy + silent-fail scripts.
    harness = MockHarness()
    harness.register(
        "acme_portal",
        script_clean_extraction(
            invoices=[
                {"invoice_id": "INV-1023", "amount": 12000.0,
                 "due_date": "2026-04-01", "status": "open"},
            ]
        ),
    )
    harness.register("zenith_portal", script_silent_fail())

    # Make sure both portals exist (idempotent re-seed).
    from scripts.seed_portals import seed as seed_portals  # local import

    seed_portals()

    # Enqueue both jobs.
    with session_scope(engine) as session:
        queue = SqliteJobQueue(session)
        happy_id = queue.enqueue(
            portal_id="acme_portal",
            payer_id="acme",
            action=JobAction.EXTRACT_INVOICES,
        )
        silent_id = queue.enqueue(
            portal_id="zenith_portal",
            payer_id="acme",
            action=JobAction.EXTRACT_INVOICES,
        )

    print(_panel("queue", [f"happy job   id={happy_id}", f"silent fail id={silent_id}"]))
    print()

    # Process them one at a time. The worker picks ready jobs in FIFO order.
    for _ in range(2):
        outcome = process_one_job(engine=engine, harness=harness, vault=vault, llm=llm)
        if outcome is None:
            print("[demo] no more jobs ready.")
            break

        v = outcome.validation
        lines = [
            f"job_id        : {outcome.job_id}",
            f"final status  : {outcome.status.value}",
            f"verdict       : {v.verdict.value if v else 'n/a'}",
            f"confidence    : {f'{v.confidence:.2f}' if v else 'n/a'}",
            f"rationale     : {_wrap(v.rationale if v else '', width=60)}",
            f"trust event   : {outcome.trust_event.value if outcome.trust_event else '(none)'}",
        ]
        print(_panel(f"job {outcome.job_id} outcome", lines))
        print()

    # Show the post-run trust score. Materialize event tuples inside the
    # session so we don't hit DetachedInstanceError after it closes.
    with session_scope(engine) as session:
        trust = TrustEngine(session)
        score = trust.get_trust("acme")
        history = [
            (ev.event_type.value, ev.delta)
            for ev in trust.get_history("acme")[-5:]
        ]
    print(
        _panel(
            "acme post-run trust",
            [f"current trust : {score:.3f}"]
            + [f"  {evt}  delta={delta:+.3f}" for evt, delta in history],
        )
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
