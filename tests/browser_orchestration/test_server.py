"""HTTP-level tests for the browser orchestration FastAPI server.

Forces FakeLLM via env, uses tmp_path-based DB for isolation.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from browser_orchestration.harness_adapter import (
    MockHarness,
    script_clean_extraction,
    script_silent_fail,
)
from browser_orchestration.models import Portal
from browser_orchestration.vault import CredentialVault, generate_key
from shared.models import Payer


@pytest.fixture(scope="module", autouse=True)
def _force_fake_llm():
    os.environ["BROWSER_FAKE_LLM"] = "1"
    os.environ["VAULT_KEY"] = generate_key()
    yield
    os.environ.pop("BROWSER_FAKE_LLM", None)
    os.environ.pop("VAULT_KEY", None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{db_path}")

    from browser_orchestration.server import create_app

    harness = MockHarness({
        "acme_portal": script_clean_extraction(
            invoices=[{"invoice_id": "INV-1", "amount": 100.0,
                       "due_date": "2026-01-01", "status": "open"}]
        ),
        "zenith_portal": script_silent_fail(),
    })
    app = create_app(harness=harness)
    with TestClient(app) as c:
        yield c


def _seed(client):
    from shared.db import session_scope
    engine = client.app.state.engine
    with session_scope(engine) as s:
        if s.get(Payer, "acme") is None:
            s.add(Payer(payer_id="acme", name="Acme Corp"))
        for pid in ("acme_portal", "zenith_portal"):
            if s.get(Portal, pid) is None:
                s.add(Portal(portal_id=pid, name=pid, base_url="https://x"))
        s.commit()
    vault: CredentialVault = client.app.state.vault
    with session_scope(engine) as s:
        vault.store(s, portal_id="acme_portal", payer_id="acme",
                    username="u", secret="p")
        vault.store(s, portal_id="zenith_portal", payer_id="acme",
                    username="u", secret="p")


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_enqueue_unknown_portal_404s(client):
    r = client.post("/jobs", json={
        "portal_id": "nope", "payer_id": "acme", "action": "extract_invoices",
    })
    assert r.status_code == 404


def test_enqueue_and_run_happy_path(client):
    _seed(client)
    r = client.post("/jobs", json={
        "portal_id": "acme_portal", "payer_id": "acme",
        "action": "extract_invoices",
    })
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    run = client.post(f"/jobs/{job_id}/run")
    body = run.json()
    assert body["ran"] is True
    assert body["verdict"] == "pass"
    assert body["status"] == "succeeded"

    job_view = client.get(f"/jobs/{job_id}").json()
    assert job_view["status"] == "succeeded"
    assert job_view["attempts_log"][0]["verdict"] == "PASS"


def test_silent_fail_path_writes_trust_event(client):
    _seed(client)
    # FakeLLM responds with first canned response — set up explicit fail.
    # Override the app's llm so we can predict the outcome.
    from voice_agent.llm import FakeLLMClient
    client.app.state.llm = FakeLLMClient(
        complete_responses=["verdict: fail\nconfidence: 0.9\nwhy: expired\n"]
    )

    r = client.post("/jobs", json={
        "portal_id": "zenith_portal", "payer_id": "acme",
        "action": "extract_invoices",
    })
    job_id = r.json()["job_id"]
    run = client.post(f"/jobs/{job_id}/run").json()
    assert run["status"] == "silent_fail"
    assert run["trust_event"] == "browser.silent_fail_caught"


def test_portal_health_endpoint(client):
    _seed(client)
    r = client.get("/portals/acme_portal/health")
    assert r.status_code == 200
    body = r.json()
    assert body["portal_id"] == "acme_portal"
    assert "recommended_interval_for_trust_0_5" in body


def test_payer_scrape_interval_reflects_trust(client):
    _seed(client)
    r = client.get("/payers/acme/scrape-interval")
    body = r.json()
    assert body["payer_id"] == "acme"
    assert body["trust_score"] == 0.5
    # Default trust 0.5 → 24h.
    assert body["interval_seconds"] == 24 * 3600
