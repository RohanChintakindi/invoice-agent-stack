"""HTTP-level tests for the FastAPI server.

Forces FakeLLMClient via the VOICE_AGENT_FAKE_LLM env var so the tests
are deterministic and offline.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from shared.models import Payer


@pytest.fixture(scope="module", autouse=True)
def _force_fake_llm():
    os.environ["VOICE_AGENT_FAKE_LLM"] = "1"
    yield
    os.environ.pop("VOICE_AGENT_FAKE_LLM", None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{db_path}")

    from voice_agent.server import app

    with TestClient(app) as c:
        yield c


def _seed_payer(client):
    """Insert a payer row into the test DB via the app's engine."""
    from shared.db import session_scope

    engine = client.app.state.engine
    with session_scope(engine) as db:
        existing = db.get(Payer, "acme")
        if existing is None:
            db.add(Payer(payer_id="acme", name="Acme Corp"))


def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_chat_completions_returns_assistant_message(client):
    _seed_payer(client)
    payload = {
        "model": "iridium-collections-agent",
        "messages": [
            {"role": "system", "content": "[payer_id=acme]"},
            {"role": "user", "content": "Hi, what's this about?"},
        ],
        "invoice_facts": "INV-1023, $12,000.",
    }
    r = client.post("/v1/chat/completions", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"]
    assert body["debug"]["phase"]
    assert body["debug"]["tone"]


def test_chat_completions_persists_session_across_turns(client):
    _seed_payer(client)

    base = {
        "model": "iridium-collections-agent",
        "messages": [{"role": "system", "content": "[payer_id=acme]"}],
        "invoice_facts": "INV-1023.",
        "call": {"id": "call-xyz"},
    }

    turn1 = dict(base)
    turn1["messages"] = base["messages"] + [{"role": "user", "content": "Hello"}]
    r1 = client.post("/v1/chat/completions", json=turn1)
    assert r1.status_code == 200

    # Fetch the session to make sure it was created and stored.
    s = client.get("/sessions/call-xyz")
    assert s.status_code == 200
    assert s.json()["payer_id"] == "acme"

    turn2 = dict(base)
    turn2["messages"] = base["messages"] + [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "I'll pay tomorrow"},
    ]
    r2 = client.post("/v1/chat/completions", json=turn2)
    assert r2.status_code == 200
    # Payment commit moves to paused phase.
    assert r2.json()["debug"]["phase"] == "paused"


def test_chat_completions_rejects_missing_payer(client):
    payload = {
        "model": "iridium-collections-agent",
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = client.post("/v1/chat/completions", json=payload)
    assert r.status_code == 400


def test_webhook_end_of_call_drops_session(client):
    _seed_payer(client)
    client.post(
        "/v1/chat/completions",
        json={
            "model": "iridium-collections-agent",
            "messages": [
                {"role": "system", "content": "[payer_id=acme]"},
                {"role": "user", "content": "hi"},
            ],
            "call": {"id": "call-end"},
        },
    )
    assert client.get("/sessions/call-end").status_code == 200

    client.post(
        "/webhooks/vapi",
        json={"type": "end-of-call-report", "call": {"id": "call-end"}},
    )
    assert client.get("/sessions/call-end").status_code == 404
