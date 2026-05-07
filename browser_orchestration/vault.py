"""Fernet-encrypted credential vault.

Secrets are encrypted at rest with a symmetric key loaded from the
``VAULT_KEY`` environment variable. The DB only ever sees the ciphertext.

In production, the key would come from a secrets manager (AWS KMS, GCP
Secret Manager, etc.) — the env-var indirection here is the demo-friendly
seam to swap in.

Usage:
    vault = CredentialVault.from_env()
    vault.store(session, portal_id="acme_portal", payer_id="acme",
                username="ap@acme.com", secret="hunter2")
    secret = vault.reveal(session, credential_id=3)
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import Session, select

from browser_orchestration.models import Credential


class VaultError(Exception):
    """Raised when the vault cannot encrypt/decrypt a secret."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def generate_key() -> str:
    """Generate a fresh Fernet key (base64-encoded). For seeding only."""
    return Fernet.generate_key().decode("ascii")


class CredentialVault:
    """Encrypts/decrypts credential secrets with a Fernet key."""

    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise VaultError(f"invalid Fernet key: {exc}") from exc

    @classmethod
    def from_env(cls, env_var: str = "VAULT_KEY") -> "CredentialVault":
        key = os.getenv(env_var)
        if not key:
            raise VaultError(
                f"{env_var} is not set. Generate one with "
                "`python -c \"from browser_orchestration.vault import generate_key; "
                "print(generate_key())\"`."
            )
        return cls(key)

    def encrypt(self, plaintext: str) -> str:
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return base64.b64encode(token).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            token = base64.b64decode(ciphertext.encode("ascii"))
            return self._fernet.decrypt(token).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise VaultError(f"failed to decrypt secret: {exc}") from exc

    # ---- DB-backed helpers ----------------------------------------------

    def store(
        self,
        session: Session,
        *,
        portal_id: str,
        payer_id: str,
        username: str,
        secret: str,
    ) -> Credential:
        """Insert or update a credential for (portal, payer)."""
        existing = session.exec(
            select(Credential).where(
                Credential.portal_id == portal_id,
                Credential.payer_id == payer_id,
            )
        ).first()

        ciphertext = self.encrypt(secret)
        if existing is None:
            cred = Credential(
                portal_id=portal_id,
                payer_id=payer_id,
                username=username,
                secret_ciphertext=ciphertext,
            )
            session.add(cred)
            session.flush()
            return cred

        existing.username = username
        existing.secret_ciphertext = ciphertext
        existing.rotated_at = _utcnow()
        session.add(existing)
        session.flush()
        return existing

    def reveal(self, session: Session, *, credential_id: int) -> tuple[str, str]:
        """Return (username, plaintext_secret) for a credential row.

        The plaintext stays in process memory only — never logged, never
        written back. Callers should pass it directly to the harness and
        let it fall out of scope.
        """
        cred = session.get(Credential, credential_id)
        if cred is None:
            raise VaultError(f"credential id={credential_id} not found")
        return cred.username, self.decrypt(cred.secret_ciphertext)

    def reveal_for(
        self, session: Session, *, portal_id: str, payer_id: str
    ) -> tuple[str, str]:
        cred = session.exec(
            select(Credential).where(
                Credential.portal_id == portal_id,
                Credential.payer_id == payer_id,
            )
        ).first()
        if cred is None:
            raise VaultError(
                f"no credential for portal={portal_id} payer={payer_id}"
            )
        return cred.username, self.decrypt(cred.secret_ciphertext)
