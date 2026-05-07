"""Seed two demo portals for the browser orchestration vertical.

  - acme_portal:    happy path, returns clean invoices
  - zenith_portal:  silent-fail path, harness ok but content empty

Both have stored credentials for the seeded "acme" payer (vertical 1's
seed). Run scripts/seed_demo.py first if "acme" doesn't exist.

Run:
    set VAULT_KEY=<output of vault.generate_key()>
    uv run python -m scripts.seed_portals
"""

from __future__ import annotations

import os

from browser_orchestration.models import Portal
from browser_orchestration.vault import CredentialVault, generate_key
from shared.db import init_schema, make_engine, session_scope
from shared.models import Payer


def seed() -> None:
    if not os.getenv("VAULT_KEY"):
        # Friendly default for the demo. In prod the key would come from
        # a real secrets manager.
        key = generate_key()
        print(f"[seed_portals] VAULT_KEY not set, generated one: {key}")
        os.environ["VAULT_KEY"] = key

    engine = make_engine()
    init_schema(engine)
    vault = CredentialVault.from_env()

    with session_scope(engine) as db:
        if db.get(Payer, "acme") is None:
            db.add(Payer(payer_id="acme", name="Acme Corp"))
            db.flush()

        if db.get(Portal, "acme_portal") is None:
            db.add(
                Portal(
                    portal_id="acme_portal",
                    name="Acme AP Portal",
                    base_url="https://ap.acme.example",
                    login_url="https://ap.acme.example/login",
                )
            )
        if db.get(Portal, "zenith_portal") is None:
            db.add(
                Portal(
                    portal_id="zenith_portal",
                    name="Zenith Vendor Portal",
                    base_url="https://vendors.zenith.example",
                    login_url="https://vendors.zenith.example/login",
                    notes="Known to silently log out idle sessions.",
                )
            )

        vault.store(
            db,
            portal_id="acme_portal",
            payer_id="acme",
            username="ap@acme.example",
            secret="hunter2-correct",
        )
        vault.store(
            db,
            portal_id="zenith_portal",
            payer_id="acme",
            username="vendor@acme.example",
            secret="hunter2-correct",
        )

    print("Seeded acme_portal + zenith_portal with credentials for payer=acme.")
    print(f"VAULT_KEY={os.environ['VAULT_KEY']}")


if __name__ == "__main__":
    seed()
