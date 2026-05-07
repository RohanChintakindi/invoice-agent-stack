"""Adapter Protocol that wraps the actual browser execution engine.

In production this delegates to ``browser-use/browser-harness`` running
Playwright sessions over a structured action API. The adapter pattern
keeps the worker decoupled from the harness implementation:

  - Tests use ``MockHarness`` to simulate portal behavior deterministically.
  - The demo can run end-to-end without spinning up Chromium.
  - Swapping in the real harness is a single import change.

The harness purposely does not validate its own work — it reports what
it observed. A separate validator (see ``validator.py``) decides whether
the action actually succeeded, which is how silent failures get caught.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from browser_orchestration.models import JobAction


@dataclass(frozen=True)
class HarnessRequest:
    portal_id: str
    base_url: str
    action: JobAction
    payload: dict[str, Any]
    username: str
    secret: str  # plaintext — keep in scope only for the call


@dataclass(frozen=True)
class HarnessResult:
    """Raw report from the harness. NOT a verdict on success.

    ``ok`` means the harness ran without crashing — the page loaded, the
    action completed without exception. The validator decides whether the
    *content* of the result matches expectations.
    """

    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    screenshot_path: str | None = None
    error: str | None = None
    raw_html_excerpt: str | None = None


class HarnessAdapter(Protocol):
    def execute(self, request: HarnessRequest) -> HarnessResult: ...


# ---------------------------------------------------------------------------
# Mock implementation for tests + offline demo.
# ---------------------------------------------------------------------------


PortalScript = Callable[[HarnessRequest], HarnessResult]


class MockHarness:
    """Deterministic stand-in for the real harness.

    Configure per-portal scripts (one callable per portal_id). Each script
    receives the request and returns a HarnessResult. If no script is
    registered for a portal_id, a generic success is returned so trivial
    smoke tests don't need to wire up scripts.
    """

    def __init__(self, scripts: dict[str, PortalScript] | None = None) -> None:
        self._scripts: dict[str, PortalScript] = dict(scripts or {})
        self.calls: list[HarnessRequest] = []

    def register(self, portal_id: str, script: PortalScript) -> None:
        self._scripts[portal_id] = script

    def execute(self, request: HarnessRequest) -> HarnessResult:
        self.calls.append(request)
        script = self._scripts.get(request.portal_id)
        if script is not None:
            return script(request)
        return HarnessResult(
            ok=True,
            output={"action": request.action.value, "invoices": []},
        )


# ---------------------------------------------------------------------------
# Pre-canned scripts for the demo. Real portals would come from the harness.
# ---------------------------------------------------------------------------


def script_clean_extraction(invoices: list[dict[str, Any]]) -> PortalScript:
    """Portal that returns a clean invoice list every time."""

    def _script(request: HarnessRequest) -> HarnessResult:
        return HarnessResult(
            ok=True,
            output={"action": request.action.value, "invoices": invoices},
            raw_html_excerpt="<table id='invoices'>...</table>",
        )

    return _script


def script_silent_fail() -> PortalScript:
    """Portal that says ok=true but returns an empty result.

    This is the canonical failure mode the validator must catch — the
    harness loaded the page, didn't crash, but the page actually showed
    a 'session expired' banner instead of the invoice table.
    """

    def _script(request: HarnessRequest) -> HarnessResult:
        return HarnessResult(
            ok=True,
            output={"action": request.action.value, "invoices": []},
            raw_html_excerpt="<div class='alert'>Your session has expired.</div>",
        )

    return _script


def script_hard_failure(error: str = "navigation timeout after 30s") -> PortalScript:
    def _script(request: HarnessRequest) -> HarnessResult:
        return HarnessResult(ok=False, error=error)

    return _script
