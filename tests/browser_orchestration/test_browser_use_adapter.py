"""Unit tests for BrowserUseAdapter.

These tests exercise the synchronous parts of the adapter — task building
and final-result parsing — without spinning up a real browser. End-to-end
testing requires `uv sync --extra real-browser` plus a Chromium install
and is intentionally not part of the default test suite.
"""

from __future__ import annotations

from browser_orchestration.browser_use_adapter import BrowserUseAdapter
from browser_orchestration.harness_adapter import HarnessRequest
from browser_orchestration.models import JobAction


def _request(action: JobAction = JobAction.EXTRACT_INVOICES) -> HarnessRequest:
    return HarnessRequest(
        portal_id="test_portal",
        base_url="https://billing.example/login",
        action=action,
        payload={},
        username="ap@example.com",
        secret="hunter2",
    )


class TestBuildTask:
    def test_extract_invoices_includes_credentials_and_url(self):
        adapter = BrowserUseAdapter()
        task = adapter._build_task(_request(JobAction.EXTRACT_INVOICES))

        assert "https://billing.example/login" in task
        assert "ap@example.com" in task
        assert "hunter2" in task
        assert "JSON array" in task
        assert "invoice_id" in task and "amount" in task

    def test_download_statement_uses_statement_template(self):
        adapter = BrowserUseAdapter()
        task = adapter._build_task(_request(JobAction.DOWNLOAD_STATEMENT))

        assert "statement" in task.lower()
        assert "downloaded" in task

    def test_unknown_action_falls_back_to_generic_task(self):
        adapter = BrowserUseAdapter()
        # Bypass the enum constraint to simulate a future / unknown action.
        request = _request()
        object.__setattr__(request, "action", "synthetic_unknown")
        task = adapter._build_task(request)

        assert "https://billing.example/login" in task
        assert "ap@example.com" in task


class TestParseFinal:
    def test_json_array_becomes_invoices_field(self):
        adapter = BrowserUseAdapter()
        out = adapter._parse_final(
            '[{"invoice_id": "INV-1", "amount": 100, '
            '"due_date": "2026-05-01", "status": "open"}]',
            JobAction.EXTRACT_INVOICES,
        )

        assert out["action"] == "extract_invoices"
        assert out["invoices"][0]["invoice_id"] == "INV-1"

    def test_fenced_json_strips_code_fences(self):
        adapter = BrowserUseAdapter()
        out = adapter._parse_final(
            '```json\n[{"invoice_id": "INV-2", "amount": 50}]\n```',
            JobAction.EXTRACT_INVOICES,
        )

        assert out["invoices"] == [{"invoice_id": "INV-2", "amount": 50}]

    def test_dict_response_is_spread_into_output(self):
        adapter = BrowserUseAdapter()
        out = adapter._parse_final(
            '{"downloaded": true, "filename": "stmt-2026-04.pdf"}',
            JobAction.DOWNLOAD_STATEMENT,
        )

        assert out["action"] == "download_statement"
        assert out["downloaded"] is True
        assert out["filename"] == "stmt-2026-04.pdf"

    def test_prose_falls_back_to_raw_field(self):
        adapter = BrowserUseAdapter()
        out = adapter._parse_final(
            "I could not find an invoices section on the page.",
            JobAction.EXTRACT_INVOICES,
        )

        assert out["action"] == "extract_invoices"
        assert "raw" in out
        assert "could not find" in out["raw"]
        # Must not falsely claim invoices were extracted — the validator
        # treats this as a silent failure and the absence of `invoices` is
        # the signal it relies on.
        assert "invoices" not in out

    def test_long_raw_response_is_truncated(self):
        adapter = BrowserUseAdapter()
        long_text = "x" * 2000
        out = adapter._parse_final(long_text, JobAction.EXTRACT_INVOICES)

        assert "raw" in out
        assert len(out["raw"]) <= 800

    def test_none_returns_empty_invoices(self):
        adapter = BrowserUseAdapter()
        out = adapter._parse_final(None, JobAction.EXTRACT_INVOICES)

        assert out == {"action": "extract_invoices", "invoices": []}


class TestExecuteSurfaceErrorsAsHardFailure:
    def test_missing_anthropic_key_returns_failure_not_exception(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = BrowserUseAdapter()
        result = adapter.execute(_request())

        assert result.ok is False
        assert result.error and "ANTHROPIC_API_KEY" in result.error
