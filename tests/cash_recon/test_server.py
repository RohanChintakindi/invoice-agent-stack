"""HTTP-level smoke tests for the cash_recon FastAPI server."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from cash_recon.models import Invoice, PayerAlias
from cash_recon.ranker import train
from shared.db import session_scope
from shared.models import Payer


@pytest.fixture(scope="module")
def ranker():
    return train(seed=42, n_invoices=160, n_wires=160)


@pytest.fixture
def client(tmp_path, monkeypatch, ranker):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{db_path}")

    from cash_recon.server import create_app

    app = create_app(ranker=ranker)
    with TestClient(app) as c:
        yield c


def _seed(client):
    engine = client.app.state.engine
    with session_scope(engine) as s:
        if s.get(Payer, "acme") is None:
            s.add(Payer(payer_id="acme", name="Acme Corp"))
        s.add(PayerAlias(alias="ACME CORP", payer_id="acme"))
        s.add(
            Invoice(
                invoice_id="INV-9001", payer_id="acme", amount=8000.0,
                issued_on=date(2026, 4, 1), due_date=date(2026, 5, 1),
            )
        )
        s.commit()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_ingest_clean_wire_auto_matches(client):
    _seed(client)
    r = client.post("/wires", json={
        "wire_id": "WIRE-TEST-1",
        "amount": 8000.0,
        "received_on": "2026-05-02",
        "memo": "PAYMENT INV-9001 ACME CORP",
        "sender_name": "ACME CORP",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved_payer_id"] == "acme"
    assert body["final_status"] in ("auto_matched", "under_review")


def test_get_wire_returns_candidates(client):
    _seed(client)
    client.post("/wires", json={
        "wire_id": "WIRE-TEST-2",
        "amount": 8000.0,
        "received_on": "2026-05-02",
        "memo": "PAYMENT INV-9001 ACME CORP",
        "sender_name": "ACME CORP",
    })
    detail = client.get("/wires/WIRE-TEST-2").json()
    assert detail["wire_id"] == "WIRE-TEST-2"
    assert len(detail["candidates"]) >= 1


def test_unknown_wire_404s(client):
    assert client.get("/wires/does-not-exist").status_code == 404


def test_review_queue_endpoint(client):
    _seed(client)
    r = client.get("/reviews")
    assert r.status_code == 200
    assert isinstance(r.json()["wires"], list)


def test_payer_threshold_endpoint(client):
    _seed(client)
    r = client.get("/payers/acme/threshold").json()
    assert r["payer_id"] == "acme"
    assert 0.85 <= r["auto_match_threshold"] <= 0.97
