"""Entity resolution: exact + fuzzy + payer name fallback."""

from __future__ import annotations

import pytest

from cash_recon import entity_resolution as er
from cash_recon.models import PayerAlias
from shared.models import Payer


@pytest.fixture
def seeded(session):
    session.add(Payer(payer_id="acme", name="Acme Corp"))
    session.add(Payer(payer_id="zenith", name="Zenith Industries"))
    session.add(PayerAlias(alias="ACME CORP", payer_id="acme"))
    session.add(PayerAlias(alias="ACME CORPORATION", payer_id="acme"))
    session.add(PayerAlias(alias="ZENITHIND", payer_id="zenith"))
    session.commit()
    return session


def test_exact_alias_hit(seeded):
    r = er.resolve(seeded, sender_name="ACME CORP")
    assert r.payer_id == "acme"
    assert r.method == "exact_alias"
    assert r.score == 100.0


def test_substring_hit(seeded):
    r = er.resolve(seeded, sender_name="WIRE FROM ACME CORP REF#123")
    assert r.payer_id == "acme"


def test_fuzzy_alias_hit(seeded):
    # Typo'd alias close to ACME CORPORATION.
    r = er.resolve(seeded, sender_name="ACME CORPORATIN")
    assert r.payer_id == "acme"
    assert r.method in ("fuzzy_alias", "fuzzy_payer_name", "exact_alias")


def test_fuzzy_payer_name_hit(seeded):
    # No alias matches but payer.name does.
    r = er.resolve(seeded, sender_name="ZENITH INDUSTRIES INC")
    assert r.payer_id == "zenith"


def test_no_match_returns_none(seeded):
    r = er.resolve(seeded, sender_name="UNKNOWN VENDOR LLC")
    assert r.payer_id is None
    assert r.method == "no_match"


def test_learn_alias_idempotent(seeded):
    a1 = er.learn_alias(seeded, alias="acme c.", payer_id="acme")
    a2 = er.learn_alias(seeded, alias="ACME C.", payer_id="acme")
    assert a1.id == a2.id
