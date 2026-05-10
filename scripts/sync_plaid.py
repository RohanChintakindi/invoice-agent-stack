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
from datetime import date, timedelta
import time as _time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import plaid
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.custom_sandbox_transaction import CustomSandboxTransaction
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.products import Products
from plaid.model.sandbox_item_fire_webhook_request import (
    SandboxItemFireWebhookRequest,
)
from plaid.model.sandbox_public_token_create_request import (
    SandboxPublicTokenCreateRequest,
)
from plaid.model.sandbox_public_token_create_request_options import (
    SandboxPublicTokenCreateRequestOptions,
)
from plaid.model.sandbox_transactions_create_request import (
    SandboxTransactionsCreateRequest,
)
from plaid.model.transactions_refresh_request import TransactionsRefreshRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.webhook_type import WebhookType

from cash_recon import service as recon_service
from cash_recon.models import WireTransfer
from cash_recon.ranker import DEFAULT_ARTIFACT_DIR, load, train
from shared.db import init_schema, make_engine, session_scope
from shared.trust_engine import TrustEngine

# Plaid's "First Platypus Bank" — a sandbox institution that always works.
SANDBOX_INSTITUTION_ID = "ins_109508"

# Persist the access_token so re-running doesn't churn fake Items.
TOKEN_CACHE = Path(__file__).resolve().parent.parent / ".plaid_sandbox.json"

# Webhook URL Plaid will hit when DEFAULT_UPDATE fires. Required for
# sandbox_transactions_create injections to actually surface in
# transactions_sync — Plaid sandbox holds custom transactions until a
# webhook flow completes. The endpoint just needs to return 200.
PLAID_WEBHOOK_URL = os.getenv(
    "PLAID_WEBHOOK_URL",
    "https://invoice-agent-stack-169815310866.us-central1.run.app/recon/webhooks/plaid",
)


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


def _load_cache() -> dict:
    if TOKEN_CACHE.exists():
        return json.loads(TOKEN_CACHE.read_text())
    return {}


def _save_cache(cache: dict) -> None:
    TOKEN_CACHE.write_text(json.dumps(cache, indent=2))


def _build_user_custom_payload() -> str:
    """JSON payload Plaid's `user_custom` sandbox accepts. Defines a single
    checking account preloaded with payer-matched transactions. Unlike
    sandbox_transactions_create (which requires a webhook lifecycle to
    surface custom data), user_custom transactions are baked into the
    item at creation — they show up in transactions_sync immediately.
    """
    today = date.today()
    return json.dumps(
        {
            "override_accounts": [
                {
                    "type": "depository",
                    "subtype": "checking",
                    "starting_balance": 250000.00,
                    "meta": {
                        "name": "Iridium Operating Account",
                        "official_name": "Iridium AR Sweep — Checking",
                    },
                    "transactions": [
                        {
                            "amount": row["amount"],
                            "description": row["description"],
                            "date_transacted": (
                                today - timedelta(days=row["days_ago"])
                            ).isoformat(),
                            "date_posted": (
                                today - timedelta(days=row["days_ago"] - 1)
                            ).isoformat(),
                            "currency": "USD",
                        }
                        for row in DEMO_INJECTIONS
                    ],
                }
            ],
        }
    )


def _get_access_token(client: plaid_api.PlaidApi) -> str:
    cache = _load_cache()
    if cache.get("access_token"):
        return cache["access_token"]

    pub_req = SandboxPublicTokenCreateRequest(
        institution_id=SANDBOX_INSTITUTION_ID,
        initial_products=[Products("transactions")],
        options=SandboxPublicTokenCreateRequestOptions(
            webhook=PLAID_WEBHOOK_URL,
            override_username="user_custom",
            override_password=_build_user_custom_payload(),
        ),
    )
    pub = client.sandbox_public_token_create(pub_req)
    public_token = pub["public_token"]

    exch_req = ItemPublicTokenExchangeRequest(public_token=public_token)
    exch = client.item_public_token_exchange(exch_req)
    access_token = exch["access_token"]

    # user_custom items already contain our injected transactions, so flag
    # them as injected to skip the legacy sandbox_transactions_create path.
    _save_cache({"access_token": access_token, "injected": True})
    print(f"[plaid] cached new sandbox access_token to {TOKEN_CACHE.name}")
    return access_token


# --- Realistic wire distribution generator -----------------------------------
#
# Replaces the previous hand-rigged 5-injection list. Mirrors the 60/15/10/10/5
# distribution from cash_recon/synth.py so the Plaid sync produces a *real*
# mix of recon outcomes:
#
#     60%  single   — clean exact match against an open invoice
#     15%  bundle   — one wire pays 2-3 invoices for the same payer
#     10%  partial  — wire amount is ~70% of invoice (typical short-pay)
#     10%  noisy    — typo'd payer name, no invoice id in memo
#      5%  decoy    — money from a sender we don't have invoices for
#
# Result: some auto_match, some under_review, some unmatched. The ranker has
# to actually discriminate, the trust-aware threshold has to actually fire,
# and the demo tells an honest "this is what production looks like" story.
#
# The invoice list mirrors scripts/seed_unified_demo.py (kept in sync manually
# — both files cite each other in comments). If you add invoices to the seed,
# add them here too.

_SEED_INVOICES: list[tuple[str, str, float]] = [
    # (invoice_id, payer_id, amount)
    ("INV-2001", "acme",   12000.0),
    ("INV-2002", "acme",    4500.0),
    ("INV-2003", "acme",    7500.0),
    ("INV-2004", "zenith",  9000.0),
    ("INV-2005", "zenith",  3200.0),
    ("INV-2006", "globex",  1800.0),
    ("INV-2007", "acme",    8200.0),
    ("INV-2008", "acme",   15500.0),
    ("INV-2009", "acme",    2400.0),
    ("INV-2010", "zenith",  6750.0),
    ("INV-2011", "zenith", 11250.0),
    ("INV-2012", "zenith",  4800.0),
    ("INV-2013", "zenith", 14200.0),
    ("INV-2014", "globex",  3500.0),
    ("INV-2015", "globex",  9750.0),
    ("INV-2016", "globex",  5600.0),
    ("INV-2017", "globex", 22000.0),
    ("INV-2018", "acme",   18900.0),
]

# Aliases that resolve to a payer via cash_recon.entity_resolution. Lifted
# from cash_recon/synth.py:PAYER_FIXTURES so wires exercise the same alias
# table the seed configures.
_PAYER_ALIASES: dict[str, list[str]] = {
    "acme":   ["ACME CORP", "ACME CORPORATION", "Acme C.", "ACME CRP"],
    "zenith": ["ZENITH INDUSTRIES", "Zenith Ind.", "ZENITHIND"],
    "globex": ["GLOBEX LLC", "Globex L.L.C.", "GLOBEXLLC"],
}

_DECOY_NAMES: list[str] = [
    "UNKNOWN VENDOR", "REFUND DEPT", "MISC PAYMENT",
    "INTRST PYMNT", "SQUARE INC", "STRIPE TRANSFER",
]


def _typo(s: str, rng) -> str:
    """Drop one char or swap two adjacent — same noise model as synth.py."""
    if len(s) < 4:
        return s
    if rng.random() < 0.5:
        i = rng.randrange(len(s))
        return s[:i] + s[i + 1:]
    i = rng.randrange(len(s) - 1)
    return s[:i] + s[i + 1] + s[i] + s[i + 2:]


def _generate_demo_wires(seed: int = 42, n_wires: int = 18) -> list[dict]:
    """Produce a Plaid-shaped wire list with the same distribution
    cash_recon/synth.py uses for ranker training. Deterministic for a given
    seed so re-running the script gives the same demo data.
    """
    import random

    rng = random.Random(seed)

    by_payer: dict[str, list[tuple[str, float]]] = {}
    for inv_id, payer_id, amount in _SEED_INVOICES:
        by_payer.setdefault(payer_id, []).append((inv_id, amount))

    wires: list[dict] = []
    for _ in range(n_wires):
        roll = rng.random()
        days_ago = rng.randint(0, 5)

        if roll < 0.60:
            # Clean single-invoice payment.
            payer_id = rng.choice(list(by_payer.keys()))
            inv_id, amount = rng.choice(by_payer[payer_id])
            sender = rng.choice(_PAYER_ALIASES[payer_id])
            wires.append({
                "amount": -round(amount, 2),
                "description": f"PAYMENT {inv_id} {sender}",
                "days_ago": days_ago,
            })
        elif roll < 0.75:
            # Bundle: 2-3 invoices, same payer, summed.
            payer_id = rng.choice(list(by_payer.keys()))
            if len(by_payer[payer_id]) < 2:
                continue
            k = min(len(by_payer[payer_id]), rng.choice([2, 3]))
            chosen = rng.sample(by_payer[payer_id], k)
            sender = rng.choice(_PAYER_ALIASES[payer_id])
            total = round(sum(a for _, a in chosen), 2)
            wires.append({
                "amount": -total,
                "description": f"BULK PMT {sender}",
                "days_ago": days_ago,
            })
        elif roll < 0.85:
            # Partial: paid ~70% (typical short-pay scenario).
            payer_id = rng.choice(list(by_payer.keys()))
            inv_id, amount = rng.choice(by_payer[payer_id])
            sender = rng.choice(_PAYER_ALIASES[payer_id])
            partial = round(amount * rng.uniform(0.6, 0.8), 2)
            wires.append({
                "amount": -partial,
                "description": f"PARTIAL PMT {inv_id} {sender}",
                "days_ago": days_ago,
            })
        elif roll < 0.95:
            # Noisy: typo'd sender name, no invoice id in memo.
            payer_id = rng.choice(list(by_payer.keys()))
            _, amount = rng.choice(by_payer[payer_id])
            sender = _typo(rng.choice(_PAYER_ALIASES[payer_id]), rng)
            wires.append({
                "amount": -round(amount, 2),
                "description": f"WIRE FROM {sender}",
                "days_ago": days_ago,
            })
        else:
            # Decoy: not a payer we know; should land as unmatched.
            decoy = rng.choice(_DECOY_NAMES)
            amount = round(rng.uniform(150, 8000), 2)
            wires.append({
                "amount": -amount,
                "description": f"INCOMING {decoy}",
                "days_ago": days_ago,
            })

    return wires


# Module-level so the user_custom payload builder can reference it. Generated
# once at import time — deterministic via the seed. seed=1 / n=22 hits every
# bucket of the realistic distribution at least once, which gives the demo
# every recon outcome we want to showcase (auto_match, bundle, partial-pay,
# fuzzy-match, decoy-unmatched).
DEMO_INJECTIONS: list[dict] = _generate_demo_wires(seed=1, n_wires=22)


def _inject_demo_transactions(
    client: plaid_api.PlaidApi, access_token: str
) -> int:
    """Inject deterministic payer-matched transactions into the sandbox
    item. Plaid's response only carries request_id (the actual transactions
    populate asynchronously via DEFAULT_UPDATE webhook), so we return the
    count we requested rather than parsing the response. The caller is
    expected to poll transactions_sync afterwards until the new rows land.
    """
    today = date.today()
    txs = [
        CustomSandboxTransaction(
            date_transacted=today - timedelta(days=row["days_ago"]),
            date_posted=today - timedelta(days=row["days_ago"] - 1),
            amount=row["amount"],
            description=row["description"],
            iso_currency_code="USD",
        )
        for row in DEMO_INJECTIONS
    ]
    req = SandboxTransactionsCreateRequest(
        access_token=access_token,
        transactions=txs,
    )
    client.sandbox_transactions_create(req)

    # The injected transactions sit in limbo until a DEFAULT_UPDATE webhook
    # is delivered. Fire it manually so sandbox processes them — Plaid will
    # POST to PLAID_WEBHOOK_URL (handled by /recon/webhooks/plaid) and then
    # surface the transactions in the next transactions_sync call.
    fire_req = SandboxItemFireWebhookRequest(
        access_token=access_token,
        webhook_type=WebhookType("TRANSACTIONS"),
        webhook_code="DEFAULT_UPDATE",
    )
    client.sandbox_item_fire_webhook(fire_req)

    # transactions_refresh as a belt-and-suspenders pass — also forces
    # Plaid's transaction enrichment pipeline.
    try:
        client.transactions_refresh(
            TransactionsRefreshRequest(access_token=access_token)
        )
    except plaid.ApiException:
        pass

    return len(txs)


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
    cache = _load_cache()

    # Inject payer-matched transactions exactly once per access_token (tracked
    # in the cache file). Skipping the conditional `if not incoming` because
    # Plaid sandbox already preloads stock merchants like United Airlines —
    # they don't help us demo cash recon against our payer fixtures.
    if not cache.get("injected"):
        injected = _inject_demo_transactions(client, access_token)
        print(f"[plaid] injected {injected} payer-matched transactions; "
              f"waiting for Plaid to process them (DEFAULT_UPDATE webhook)...")
        cache["injected"] = True
        _save_cache(cache)

        # Plaid processes injected sandbox transactions asynchronously. We
        # match by amount — Plaid normalizes the description but amount
        # round-trips exactly. Up to 60 seconds of polling.
        target_amounts = {round(row["amount"], 2) for row in DEMO_INJECTIONS}
        for attempt in range(1, 21):
            _time.sleep(3)
            transactions = _pull_transactions(client, access_token)
            seen_amounts = {round(float(tx["amount"]), 2) for tx in transactions}
            matches = target_amounts & seen_amounts
            print(
                f"[plaid]   attempt {attempt} ({attempt * 3}s): "
                f"{len(matches)}/{len(target_amounts)} injected amounts visible"
            )
            if len(matches) >= len(target_amounts):
                break
    else:
        # user_custom items have transactions baked in at creation, but the
        # initial sync can still take a few seconds on the Plaid side. Poll
        # briefly until our injected amounts show up.
        target_amounts = {round(row["amount"], 2) for row in DEMO_INJECTIONS}
        transactions = []
        for attempt in range(1, 11):
            transactions = _pull_transactions(client, access_token)
            seen = {round(float(tx["amount"]), 2) for tx in transactions}
            matches = target_amounts & seen
            if matches:
                print(
                    f"[plaid]   attempt {attempt} ({attempt * 2}s): "
                    f"{len(matches)}/{len(target_amounts)} injected amounts visible"
                )
                if len(matches) >= len(target_amounts):
                    break
            else:
                print(f"[plaid]   attempt {attempt} ({attempt * 2}s): waiting for sync to surface injected txs...")
            _time.sleep(2)

    incoming = [tx for tx in transactions if float(tx["amount"]) < 0]
    print(f"[plaid] pulled {len(transactions)} transactions from sandbox")
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
