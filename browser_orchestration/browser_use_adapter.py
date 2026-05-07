"""BrowserUseAdapter: real browser automation via the browser-use library.

Drop-in replacement for ``MockHarness`` in the worker. Each ``execute()``
call spins up a headless Chromium session through ``browser-use`` and lets
an LLM (Claude Haiku by default) drive page navigation. The adapter
translates a structured ``HarnessRequest`` into a natural-language task,
runs it, and parses the agent's final output back into a ``HarnessResult``
that the existing validator already knows how to score.

The adapter is intentionally heavy — every call launches a real browser —
so tests should keep using ``MockHarness``. Use this for live demos and
production runs against real customer portals.

Install (the real-browser deps are in an optional extras group so they
don't bloat the base image):

    uv sync --extra real-browser
    uv run playwright install chromium

Wire it into the worker by passing it where ``MockHarness`` would go::

    from browser_orchestration.browser_use_adapter import BrowserUseAdapter
    harness = BrowserUseAdapter()
    process_one_job(engine=engine, harness=harness, vault=vault, llm=llm)
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from browser_orchestration.harness_adapter import (
    HarnessRequest,
    HarnessResult,
)
from browser_orchestration.models import JobAction


_DEFAULT_INVOICE_TASK = (
    "Open {url}. If a login form appears, sign in using username '{user}' "
    "and password '{secret}'. Once authenticated, navigate to the section "
    "that lists invoices or billing statements. Identify every invoice row "
    "you can see and return them as a JSON array. Each element MUST have "
    "the keys `invoice_id` (string), `amount` (number, USD), `due_date` "
    "(YYYY-MM-DD), and `status` (one of 'open', 'paid', 'overdue'). "
    "If a value is missing on the page, set it to null. Return ONLY the "
    "JSON array — no prose, no code fences."
)

_DEFAULT_STATEMENT_TASK = (
    "Open {url}. If a login form appears, sign in using username '{user}' "
    "and password '{secret}'. Find the most recent account statement and "
    "download it. After the download completes, return JSON of the form "
    "`{{\"downloaded\": true, \"filename\": \"...\", \"period\": \"YYYY-MM\"}}`. "
    "If the page shows no statements, return `{{\"downloaded\": false, "
    "\"reason\": \"...\"}}`."
)


_TASK_TEMPLATES: dict[JobAction, str] = {
    JobAction.EXTRACT_INVOICES: _DEFAULT_INVOICE_TASK,
    JobAction.DOWNLOAD_STATEMENT: _DEFAULT_STATEMENT_TASK,
}


class BrowserUseAdapter:
    """HarnessAdapter implementation backed by ``browser-use`` + Playwright.

    Sync ``execute()`` so it satisfies the ``HarnessAdapter`` Protocol the
    rest of the worker is built around. Internally it bridges to the async
    ``browser-use`` Agent via ``asyncio.run`` — safe because the worker
    runs outside a running event loop.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        fallback_model: str | None = None,
        headless: bool = True,
        max_steps: int = 25,
    ) -> None:
        # Cost-optimized two-model setup: Haiku drives most steps, but
        # browser-use auto-escalates to the fallback when the primary
        # model produces output that doesn't satisfy its strict per-step
        # Pydantic schema. This is the LLM-layer counterpart to the
        # validator's page-layer self-healing.
        self._model = model or os.getenv(
            "ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"
        )
        self._fallback_model = fallback_model or os.getenv(
            "ANTHROPIC_FALLBACK_MODEL", "claude-sonnet-4-6"
        )
        self._headless = headless
        self._max_steps = max_steps

    def execute(self, request: HarnessRequest) -> HarnessResult:
        try:
            return asyncio.run(self._execute_async(request))
        except Exception as exc:  # surface as a hard failure for the validator
            return HarnessResult(
                ok=False,
                error=f"BrowserUseAdapter raised: {type(exc).__name__}: {exc}",
            )

    async def _execute_async(self, request: HarnessRequest) -> HarnessResult:
        # Cheap precondition first — bail before importing the heavy deps
        # if there's no key to drive the LLM with.
        if not os.getenv("ANTHROPIC_API_KEY"):
            return HarnessResult(
                ok=False,
                error="ANTHROPIC_API_KEY not set; cannot drive browser-use Agent.",
            )

        # Lazy imports keep the optional deps optional. Anyone importing
        # this module without the [real-browser] extras would otherwise
        # fail at import time on a base-image worker. We use browser-use's
        # own ChatAnthropic wrapper (not langchain's) — browser-use needs
        # a `.provider` attribute that the langchain class doesn't expose.
        from browser_use import Agent  # type: ignore[import-not-found]
        from browser_use.llm.anthropic.chat import (  # type: ignore[import-not-found]
            ChatAnthropic,
        )

        api_key = os.environ["ANTHROPIC_API_KEY"]
        primary = ChatAnthropic(model=self._model, api_key=api_key, temperature=0.2)
        fallback = ChatAnthropic(
            model=self._fallback_model, api_key=api_key, temperature=0.2
        )
        task = self._build_task(request)
        # fallback_llm: browser-use auto-escalates to this model when the
        # primary repeatedly fails Pydantic validation on the per-step
        # action schema. Self-healing without exhausting the step budget.
        agent = Agent(task=task, llm=primary, fallback_llm=fallback)
        history = await agent.run(max_steps=self._max_steps)
        final = history.final_result() if hasattr(history, "final_result") else history

        return HarnessResult(
            ok=True,
            output=self._parse_final(final, request.action),
            raw_html_excerpt=_truncate(str(final), 1500) if final else None,
        )

    def _build_task(self, request: HarnessRequest) -> str:
        template = _TASK_TEMPLATES.get(request.action)
        if template is None:
            return (
                f"Open {request.base_url} and report back any invoice or "
                f"billing information you can see. Sign in as '{request.username}' "
                f"with password '{request.secret}' if prompted."
            )
        return template.format(
            url=request.base_url,
            user=request.username,
            secret=request.secret,
        )

    def _parse_final(self, final: Any, action: JobAction) -> dict[str, Any]:
        """Coerce the agent's free-form final answer into the structured
        shape the validator expects. Best-effort: if parsing fails we still
        return ``ok=true`` so the validator can decide whether the result
        is a silent failure (this is exactly the contract HarnessResult
        documents — the harness reports what it observed; the validator
        scores correctness)."""
        if final is None:
            return {"action": action.value, "invoices": []}
        if isinstance(final, dict):
            return {"action": action.value, **final}
        if isinstance(final, list):
            return {"action": action.value, "invoices": final}

        text = str(final).strip()
        # Strip markdown code fences the LLM may add despite our instructions.
        if text.startswith("```"):
            _, _, body = text.partition("\n")
            text = body.rsplit("```", 1)[0].strip()

        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return {"action": action.value, "raw": _truncate(text, 800)}

        if isinstance(parsed, list):
            return {"action": action.value, "invoices": parsed}
        if isinstance(parsed, dict):
            return {"action": action.value, **parsed}
        return {"action": action.value, "raw": _truncate(text, 800)}


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
