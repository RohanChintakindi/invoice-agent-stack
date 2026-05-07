"""Pull transactions from Plaid Sandbox and feed them into cash_recon.

Sandbox flow (no real bank, synthetic data):
    1. Plaid sandbox issues a fake public_token for a fake institution.
    2. Exchange that for an access_token (cached in .plaid_sandbox.json).
    3. Call /transactions/sync to get all transactions on the fake account.
    4. Filter to incoming credits (positive cash inflows -> "wire received").
    5. POST each one through `cash_recon.service.ingest_wire` so it goes
       through entity resolution -> ranker -> threshold check, exactly
       like a wire from a real bank would.

Run:
    uv run python -m scripts.sync_plaid

Env vars (already in .env from `make_demo_real`):
    PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV (sandbox|development|production)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import plaid
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.products import Products
from plaid.model.sandbox_public_token_create_request import (
    SandboxPublicTokenCreateRequest,
)
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from cash_recon import service as recon_service
from cash_recon.models import WireTransfer
from cash_recon.ranker import DEFAULT_ARTIFACT_DIR, load, train
from shared.db import init_schema, make_engine, session_scope
from shared.trust_engine import TrustEngine

# Plaid's "First Platypus Bank" — a sandbox institution that always works.
SANDBOX_INSTITUTION_ID = "ins_109508"

# Persist the access_token so re-running doesn't churn fake Items.
TOKEN_CACHE = Path(__file__).resolve().parent.parent / ".plaid_sandbox.json"


def _client() -> plaid_api.PlaidApi:
    env_name = os.getenv("PLAID_ENV", "sandbox").lower()
    host_map = {
        "sandbox": plaid.Environment.Sandbox,
        "development": plaid.Environment.Production,  # Plaid renamed dev->prod recently
        "production": plaid.Environment.Production,
    }
    cfg = plaid.Configuration(
        host=host_map.get(env_name, plaid.Environment.Sandbox),
        api_key={
            "clientId": os.environ["PLAID_CLIENT_ID"],
            "secret": os.environ["PLAID_SECRET"],
        },
    )
    return plaid_api.PlaidApi(plaid.ApiClient(cfg))


def _get_access_token(client: plaid_api.PlaidApi) -> str:
    if TOKEN_CACHE.exists():
        cached = json.loads(TOKEN_CACHE.read_text())
        return cached["access_token"]

    pub_req = SandboxPublicTokenCreateRequest(
        institution_id=SANDBOX_INSTITUTION_ID,
        initial_products=[Products("transactions")],
    )
    pub = client.sandbox_public_token_create(pub_req)
    public_token = pub["public_token"]

    exch_req = ItemPublicTokenExchangeRequest(public_token=public_token)
    exch = client.item_public_token_exchange(exch_req)
    access_token = exch["access_token"]

    TOKEN_CACHE.write_text(json.dumps({"access_token": access_token}, indent=2))
    print(f"[plaid] cached new sandbox access_token to {TOKEN_CACHE.name}")
    return access_token


def _pull_transactions(client: plaid_api.PlaidApi, access_token: str) -> list[dict]:
    cursor = ""
    added: list[dict] = []
    while True:
        req = TransactionsSyncRequest(
            access_token=access_token,
            cursor=cursor,
            count=500,
        )
        resp = client.transactions_sync(req)
        added.extend(t.to_dict() for t in resp["added"])
        if not resp["has_more"]:
            break
        cursor = resp["next_cursor"]
    return added


def _to_wire(tx: dict) -> WireTransfer:
    """Plaid transaction -> our WireTransfer model.

    Plaid amounts are positive for outflows (debits). For our purposes we
    treat positive *inflows* (deposits / credits) as wires received from
    payers. Plaid encodes those as negative amounts.
    """
    amount = -float(tx["amount"])  # flip sign
    txid = tx["transaction_id"]
    name = tx.get("name", "") or ""
    merchant = tx.get("merchant_name") or ""
    sender = merchant or name
    return WireTransfer(
        wire_id=f"PLAID-{txid[:18].upper()}",
        amount=round(amount, 2),
        currency=tx.get("iso_currency_code") or "USD",
        received_on=date.fromisoformat(str(tx["date"])),
        memo=name,
        sender_name=sender,
        bank_ref=txid,
    )


def main() -> int:
    if not os.getenv("PLAID_CLIENT_ID") or not os.getenv("PLAID_SECRET"):
        print("[plaid] PLAID_CLIENT_ID / PLAID_SECRET not set in .env", file=sys.stderr)
        return 2

    client = _client()
    access_token = _get_access_token(client)
    transactions = _pull_transactions(client, access_token)
    print(f"[plaid] pulled {len(transactions)} transactions from sandbox")

    incoming = [tx for tx in transactions if float(tx["amount"]) < 0]
    print(f"[plaid] {len(incoming)} are incoming credits (cash inflows)")

    engine = make_engine()
    init_schema(engine)

    artifact_dir = DEFAULT_ARTIFACT_DIR
    if (artifact_dir / "ranker.json").exists():
        ranker = load(artifact_dir)
    else:
        print("[plaid] training ranker on synth dataset (first run only)...")
        ranker = train(seed=42, n_invoices=300, n_wires=300)
        ranker.save(artifact_dir)

    counts = {"auto_matched": 0, "under_review": 0, "unmatched": 0, "skipped": 0}
    for tx in incoming[:25]:  # cap to keep the demo readable
        wire = _to_wire(tx)
        if wire.amount < 1.0:
            counts["skipped"] += 1
            continue
        with session_scope(engine) as s:
            existing = s.get(WireTransfer, wire.wire_id)
            if existing is not None:
                counts["skipped"] += 1
                continue
            trust = TrustEngine(s)
            result = recon_service.ingest_wire(
                s, ranker=ranker, wire=wire, trust_engine=trust,
            )
            counts[result.final_status.value] = counts.get(
                result.final_status.value, 0
            ) + 1
            print(
                f"  {wire.wire_id} ${wire.amount:>10.2f}"
                f"  sender={wire.sender_name[:28]:<28}"
                f"  -> {result.final_status.value:<14}"
                f"  cal={result.best_calibrated_prob:.3f}"
                f"  thr={result.threshold_used:.3f}"
            )

    print("\n[plaid] outcomes:")
    for k, v in counts.items():
        print(f"  {k:<14} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
