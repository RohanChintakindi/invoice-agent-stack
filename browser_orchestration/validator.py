"""Silent-failure validator.

The harness reports whether *navigation* succeeded; the validator decides
whether the *result* did. This is the layer that catches "the harness
returned 200 but the portal actually showed 'session expired'" — the
single most common failure mode of vanilla browser automation.

Validation is two-stage:

  1. **Schema check** (cheap, deterministic): does the harness output match
     the pydantic schema we expect for this action? Empty lists where we
     require ≥1 row, missing required fields, etc. fail here.

  2. **Consistency check** (LLM, optional): given the harness output and
     the raw HTML excerpt / screenshot, does an LLM agree the action
     succeeded? This catches semantically empty results that pass the
     schema (e.g. "thank you" page returned where invoices were expected).

Stage 2 uses the same LLMClient abstraction as the voice agent so the
FakeLLMClient can drive it offline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from browser_orchestration.harness_adapter import HarnessResult
from browser_orchestration.models import JobAction
from voice_agent.llm import LLMClient


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ValidationResult:
    verdict: Verdict
    confidence: float  # 0.0–1.0
    rationale: str
    parsed_payload: dict[str, Any] | None = None

    @property
    def is_silent_fail(self) -> bool:
        """Harness said ok=true but validator says fail."""
        return self.verdict is Verdict.FAIL


# ---------------------------------------------------------------------------
# Pydantic schemas — one per JobAction.
# ---------------------------------------------------------------------------


class InvoiceRow(BaseModel):
    invoice_id: str
    amount: float
    due_date: str | None = None
    status: str | None = None


class ExtractInvoicesPayload(BaseModel):
    """The shape we expect for an extract_invoices result."""

    action: str = Field(default="extract_invoices")
    invoices: list[InvoiceRow] = Field(default_factory=list)


class GenericOkPayload(BaseModel):
    action: str
    # Catch-all for actions where any non-empty response is fine.

    model_config = {"extra": "allow"}


_SCHEMA_BY_ACTION: dict[JobAction, type[BaseModel]] = {
    JobAction.EXTRACT_INVOICES: ExtractInvoicesPayload,
    JobAction.LOGIN: GenericOkPayload,
    JobAction.DOWNLOAD_STATEMENT: GenericOkPayload,
    JobAction.CONFIRM_PAYMENT: GenericOkPayload,
}


# ---------------------------------------------------------------------------
# Stage 1: schema check.
# ---------------------------------------------------------------------------


def _schema_check(action: JobAction, output: dict[str, Any]) -> tuple[bool, str, BaseModel | None]:
    schema = _SCHEMA_BY_ACTION.get(action, GenericOkPayload)
    try:
        parsed = schema.model_validate(output)
    except ValidationError as exc:
        return False, f"schema mismatch: {exc.errors()[0]['msg']}", None

    # Action-specific emptiness rules.
    if action is JobAction.EXTRACT_INVOICES:
        invoices = getattr(parsed, "invoices", [])
        if not invoices:
            return False, "extract_invoices returned no rows", parsed

    return True, "schema ok", parsed


# ---------------------------------------------------------------------------
# Stage 2: LLM consistency check (best-effort).
# ---------------------------------------------------------------------------


CONSISTENCY_PROMPT = """You are validating whether a browser-automation
action actually succeeded. The harness reported it ran without crashing,
but harnesses can be fooled by error pages that load cleanly.

Action attempted: {action}
Expected outcome: {expected}

Harness output (JSON):
{output}

Raw HTML excerpt from the page (may be truncated):
{html}

Decide whether the action genuinely succeeded. Respond on three lines:
verdict: pass | fail | unknown
confidence: 0.0-1.0
why: one short sentence

Examples of FAIL: page shows "session expired", "login required",
"no records found" when records were expected, generic error banners.
Examples of PASS: page shows the expected data table or confirmation
message. UNKNOWN if the excerpt is too short to tell.
"""


_EXPECTED_BY_ACTION: dict[JobAction, str] = {
    JobAction.EXTRACT_INVOICES: "a table or list of invoice rows with amounts and IDs",
    JobAction.LOGIN: "an authenticated landing page (dashboard, account, etc.)",
    JobAction.DOWNLOAD_STATEMENT: "a confirmation that a PDF/CSV was generated",
    JobAction.CONFIRM_PAYMENT: "a payment confirmation receipt",
}


def parse_consistency_response(raw: str) -> tuple[Verdict, float, str]:
    verdict = Verdict.UNKNOWN
    confidence = 0.0
    why = ""
    for line in raw.splitlines():
        line = line.strip()
        if line.lower().startswith("verdict:"):
            value = line.split(":", 1)[1].strip().lower()
            try:
                verdict = Verdict(value)
            except ValueError:
                verdict = Verdict.UNKNOWN
        elif line.lower().startswith("confidence:"):
            try:
                confidence = float(line.split(":", 1)[1].strip())
            except ValueError:
                confidence = 0.0
        elif line.lower().startswith("why:"):
            why = line.split(":", 1)[1].strip()
    return verdict, max(0.0, min(1.0, confidence)), why


def _consistency_check(
    llm: LLMClient,
    *,
    action: JobAction,
    harness: HarnessResult,
) -> tuple[Verdict, float, str]:
    if not harness.raw_html_excerpt:
        # Nothing to check — defer to schema verdict.
        return Verdict.UNKNOWN, 0.0, "no html excerpt provided"

    user = CONSISTENCY_PROMPT.format(
        action=action.value,
        expected=_EXPECTED_BY_ACTION.get(action, "a non-empty success response"),
        output=json.dumps(harness.output, indent=2)[:1000],
        html=harness.raw_html_excerpt[:1500],
    )
    try:
        raw = llm.complete(
            system="You are a strict validator. Be conservative — when in doubt say fail.",
            user=user,
            max_tokens=200,
        )
    except Exception as exc:  # noqa: BLE001 — LLM may raise anything
        return Verdict.UNKNOWN, 0.0, f"llm error: {exc}"
    return parse_consistency_response(raw)


# ---------------------------------------------------------------------------
# Top-level validate().
# ---------------------------------------------------------------------------


def validate(
    *,
    action: JobAction,
    harness: HarnessResult,
    llm: LLMClient | None = None,
) -> ValidationResult:
    """Run both validation stages and combine into a single verdict.

    Combine rules:
      - Hard harness failure → FAIL with high confidence (no LLM call).
      - Schema fail + consistency fail → FAIL.
      - Schema fail + consistency pass → FAIL (schema is authoritative on
        structure; LLM might be fooled by a 'thank you' page).
      - Schema pass + consistency fail → FAIL (silent failure caught).
      - Schema pass + consistency pass → PASS.
      - Schema pass + consistency unknown → PASS at lower confidence.
    """
    if not harness.ok:
        return ValidationResult(
            verdict=Verdict.FAIL,
            confidence=0.95,
            rationale=f"harness reported failure: {harness.error}",
        )

    schema_ok, schema_reason, parsed = _schema_check(action, harness.output)
    parsed_dict = parsed.model_dump() if parsed is not None else None

    if not schema_ok:
        # Don't burn an LLM call when schema already failed — silent fail
        # is already proven.
        return ValidationResult(
            verdict=Verdict.FAIL,
            confidence=0.9,
            rationale=schema_reason,
            parsed_payload=parsed_dict,
        )

    if llm is None:
        return ValidationResult(
            verdict=Verdict.PASS,
            confidence=0.7,
            rationale="schema ok; consistency check skipped (no llm)",
            parsed_payload=parsed_dict,
        )

    cons_verdict, cons_conf, cons_reason = _consistency_check(
        llm, action=action, harness=harness
    )

    if cons_verdict is Verdict.FAIL:
        return ValidationResult(
            verdict=Verdict.FAIL,
            confidence=max(cons_conf, 0.6),
            rationale=f"silent failure: {cons_reason}",
            parsed_payload=parsed_dict,
        )

    if cons_verdict is Verdict.PASS:
        return ValidationResult(
            verdict=Verdict.PASS,
            confidence=max(cons_conf, 0.7),
            rationale=f"schema ok; {cons_reason}".rstrip(),
            parsed_payload=parsed_dict,
        )

    # Unknown from LLM — fall back to the schema verdict at reduced confidence.
    return ValidationResult(
        verdict=Verdict.PASS,
        confidence=0.6,
        rationale=f"schema ok; consistency uncertain ({cons_reason})",
        parsed_payload=parsed_dict,
    )
