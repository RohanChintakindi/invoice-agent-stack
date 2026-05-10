"""End-to-end demo of BrowserUseAdapter against the live demo_portal.

Runs two scrapes back-to-back to exercise both validator code paths:

  1. acme_portal   — happy path. Login + invoice table extraction.
                     Validator should score this OK with 3 invoices.
  2. zenith_portal — silent failure. Login looks like it works but the
                     dashboard renders "session expired" instead of the
                     table. Validator should mark this as silent_fail.

Requires the [real-browser] extras (browser-use, playwright) and a
Chromium install. Run:

    uv sync --extra real-browser
    uv run playwright install chromium
    uv run python -m scripts.run_browser_use_demo

Anthropic API key must be in the environment (loaded from .env).
"""

from __future__ import annotations

import json
import os
import sys
from pprint import pprint

from dotenv import load_dotenv

load_dotenv()

from browser_orchestration.browser_use_adapter import BrowserUseAdapter
from browser_orchestration.harness_adapter import HarnessRequest, HarnessResult
from browser_orchestration.models import JobAction
from browser_orchestration.validator import validate


SERVICE_BASE = os.getenv(
    "SERVICE_BASE",
    "https://invoice-agent-stack-169815310866.us-central1.run.app",
)
USERNAME = "ap@example"
PASSWORD = "hunter2"

SCENARIOS = [
    ("acme_portal",   "happy path — clean invoice table"),
    ("zenith_portal", "silent failure — session expired banner"),
]


def _run_one(adapter: BrowserUseAdapter, portal_id: str, label: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {portal_id}  —  {label}")
    print(f"{'=' * 70}\n")

    req = HarnessRequest(
        portal_id=portal_id,
        base_url=f"{SERVICE_BASE}/portal/{portal_id}/",
        action=JobAction.EXTRACT_INVOICES,
        payload={},
        username=USERNAME,
        secret=PASSWORD,
    )

    result: HarnessResult = adapter.execute(req)
    verdict = validate(action=JobAction.EXTRACT_INVOICES, harness=result)

    print("HarnessResult:")
    print(f"  ok           : {result.ok}")
    print(f"  error        : {result.error}")
    print(f"  output       :")
    pprint(result.output, indent=4)
    print()
    print("Validator verdict:")
    print(f"  verdict      : {verdict.verdict.value}")
    print(f"  rationale    : {verdict.rationale}")


def main() -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — aborting.", file=sys.stderr)
        return 2

    headless_str = os.getenv("BROWSER_HEADLESS", "false").lower()
    headless = headless_str in ("1", "true", "yes")

    # Default: Haiku as primary, Sonnet auto-escalation via fallback_llm.
    # browser-use's max_failures=5 + loop_detection + fallback_llm together
    # form the LLM-layer self-healing — analogous to our own validator
    # catching page-layer silent failures. If Haiku stumbles on the strict
    # per-step Pydantic schema, the harness should switch to Sonnet without
    # us hand-tuning the model up front. Override with BROWSER_USE_MODEL.
    primary_model = os.getenv("BROWSER_USE_MODEL")  # None → adapter default

    print(f"SERVICE_BASE       : {SERVICE_BASE}")
    print(f"headless       : {headless}")
    print(f"primary model  : {primary_model or '(adapter default — Haiku)'}")
    print(f"fallback model : claude-sonnet-4-6 (auto-escalation)")
    print(f"scenarios      : {[p for p, _ in SCENARIOS]}")

    adapter = BrowserUseAdapter(model=primary_model, headless=headless, max_steps=30)
    for portal_id, label in SCENARIOS:
        _run_one(adapter, portal_id, label)

    return 0


if __name__ == "__main__":
    sys.exit(main())
