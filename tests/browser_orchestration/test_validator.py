"""Silent-failure validator: schema check + LLM consistency check."""

from __future__ import annotations

from browser_orchestration.harness_adapter import HarnessResult
from browser_orchestration.models import JobAction
from browser_orchestration.validator import (
    Verdict,
    parse_consistency_response,
    validate,
)
from voice_agent.llm import FakeLLMClient


def _ok_invoices() -> dict:
    return {
        "action": "extract_invoices",
        "invoices": [
            {"invoice_id": "INV-1", "amount": 100.0, "due_date": "2026-01-01", "status": "open"},
        ],
    }


def test_harness_failure_short_circuits_to_fail():
    result = validate(
        action=JobAction.EXTRACT_INVOICES,
        harness=HarnessResult(ok=False, error="timeout"),
    )
    assert result.verdict is Verdict.FAIL
    assert "timeout" in result.rationale


def test_schema_pass_no_llm_returns_pass():
    h = HarnessResult(ok=True, output=_ok_invoices())
    result = validate(action=JobAction.EXTRACT_INVOICES, harness=h, llm=None)
    assert result.verdict is Verdict.PASS
    assert result.parsed_payload is not None


def test_extract_invoices_empty_list_fails_schema():
    h = HarnessResult(
        ok=True,
        output={"action": "extract_invoices", "invoices": []},
        raw_html_excerpt="<table></table>",
    )
    result = validate(action=JobAction.EXTRACT_INVOICES, harness=h)
    assert result.verdict is Verdict.FAIL
    assert "no rows" in result.rationale


def test_silent_fail_caught_via_llm():
    h = HarnessResult(
        ok=True,
        output=_ok_invoices(),
        raw_html_excerpt="<div>Your session has expired.</div>",
    )
    llm = FakeLLMClient(
        complete_responses=[
            "verdict: fail\nconfidence: 0.9\nwhy: page shows session expired\n"
        ]
    )
    result = validate(action=JobAction.EXTRACT_INVOICES, harness=h, llm=llm)
    assert result.verdict is Verdict.FAIL
    assert result.is_silent_fail
    assert "silent failure" in result.rationale.lower()


def test_llm_unknown_keeps_pass_at_lower_confidence():
    h = HarnessResult(
        ok=True,
        output=_ok_invoices(),
        raw_html_excerpt="<table>...</table>",
    )
    llm = FakeLLMClient(
        complete_responses=["verdict: unknown\nconfidence: 0.2\nwhy: snippet too short\n"]
    )
    result = validate(action=JobAction.EXTRACT_INVOICES, harness=h, llm=llm)
    assert result.verdict is Verdict.PASS
    assert result.confidence <= 0.7


def test_llm_pass_returns_pass():
    h = HarnessResult(
        ok=True,
        output=_ok_invoices(),
        raw_html_excerpt="<table>...</table>",
    )
    llm = FakeLLMClient(
        complete_responses=["verdict: pass\nconfidence: 0.95\nwhy: invoice table present\n"]
    )
    result = validate(action=JobAction.EXTRACT_INVOICES, harness=h, llm=llm)
    assert result.verdict is Verdict.PASS
    assert result.confidence >= 0.7


def test_parse_consistency_response_clamps():
    v, c, _ = parse_consistency_response("verdict: pass\nconfidence: 1.4\nwhy: ok\n")
    assert v is Verdict.PASS
    assert c == 1.0


def test_parse_consistency_handles_missing_fields():
    v, c, w = parse_consistency_response("nonsense")
    assert v is Verdict.UNKNOWN
    assert c == 0.0
    assert w == ""
