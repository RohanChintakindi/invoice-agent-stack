"""Vault encrypts/decrypts and persists rotations correctly."""

from __future__ import annotations

import pytest

from browser_orchestration.models import Portal
from browser_orchestration.vault import CredentialVault, VaultError, generate_key


@pytest.fixture
def vault() -> CredentialVault:
    return CredentialVault(generate_key())


@pytest.fixture
def portal(session) -> str:
    p = Portal(portal_id="acme_portal", name="Acme", base_url="https://x")
    session.add(p)
    session.commit()
    return p.portal_id


def test_round_trip_encrypts_and_decrypts(vault):
    token = vault.encrypt("hunter2")
    assert token != "hunter2"
    assert vault.decrypt(token) == "hunter2"


def test_invalid_key_raises():
    with pytest.raises(VaultError):
        CredentialVault("not-a-real-fernet-key")


def test_decrypt_garbage_raises(vault):
    with pytest.raises(VaultError):
        vault.decrypt("not-valid-ciphertext")


def test_store_persists_encrypted_blob(vault, session, portal, payer):
    cred = vault.store(
        session, portal_id=portal, payer_id=payer,
        username="ap@acme.example", secret="hunter2",
    )
    session.commit()
    assert cred.id is not None
    assert "hunter2" not in cred.secret_ciphertext


def test_reveal_for_returns_plaintext(vault, session, portal, payer):
    vault.store(
        session, portal_id=portal, payer_id=payer,
        username="ap@acme.example", secret="hunter2",
    )
    session.commit()
    user, secret = vault.reveal_for(session, portal_id=portal, payer_id=payer)
    assert (user, secret) == ("ap@acme.example", "hunter2")


def test_store_overwrites_on_rotation(vault, session, portal, payer):
    cred1 = vault.store(
        session, portal_id=portal, payer_id=payer,
        username="ap@acme.example", secret="old",
    )
    session.commit()
    cred2 = vault.store(
        session, portal_id=portal, payer_id=payer,
        username="ap@acme.example", secret="new",
    )
    session.commit()
    # Same row id — rotated in place.
    assert cred1.id == cred2.id
    _, secret = vault.reveal_for(session, portal_id=portal, payer_id=payer)
    assert secret == "new"


def test_reveal_for_unknown_payer_raises(vault, session, portal):
    with pytest.raises(VaultError):
        vault.reveal_for(session, portal_id=portal, payer_id="ghost")


def test_separate_vaults_cannot_decrypt_each_other():
    v1 = CredentialVault(generate_key())
    v2 = CredentialVault(generate_key())
    token = v1.encrypt("secret")
    with pytest.raises(VaultError):
        v2.decrypt(token)
