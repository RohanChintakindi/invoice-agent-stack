"""Synthetic invoice/wire dataset generator.

Produces a realistic-but-deterministic dataset for training and demos.
Real-world reconciliation messiness is simulated explicitly:

    - 60% of wires pay one invoice cleanly (memo names invoice id).
    - 15% are bundled — one wire pays N invoices.
    - 10% are partial — wire amount is less than invoice amount.
    - 10% are noisy memos (typo'd payer name, no invoice id).
    - 5% are decoys — money from someone we don't have an invoice for.

Each generated record carries a `truth_invoice_ids` field on the wire
for supervised training; production code never sees this.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from datetime import date, timedelta

# Stock list of realistic-looking commercial payers. Aliases per payer
# simulate the real problem: the same buyer pays under different memo
# strings depending on who initiates the wire.
PAYER_FIXTURES: list[tuple[str, str, list[str]]] = [
    ("acme", "Acme Corp", ["ACME CORP", "ACME CORPORATION", "Acme C.", "ACME CRP"]),
    ("zenith", "Zenith Industries", ["ZENITH INDUSTRIES", "Zenith Ind.", "ZENITHIND"]),
    ("globex", "Globex LLC", ["GLOBEX LLC", "Globex L.L.C.", "GLOBEXLLC"]),
    ("initech", "Initech", ["INITECH", "Initech Inc", "INITECHINC"]),
    ("hooli", "Hooli", ["HOOLI", "Hooli Inc.", "HOOLIINC"]),
    ("piedpiper", "Pied Piper", ["PIED PIPER", "PIEDPIPER", "Pied Piper Co"]),
    ("massivedyn", "Massive Dynamic", ["MASSIVE DYN", "MASSIVE DYNAMIC", "MASSIVEDYNAMIC"]),
    ("soylent", "Soylent Corp", ["SOYLENT", "SOYLENT CORP", "SOYLENTCORP"]),
]

DECOY_NAMES = ["UNKNOWN VENDOR", "REFUND DEPT", "MISC PAYMENT", "WIRE 88312"]


@dataclass
class SynthInvoice:
    invoice_id: str
    payer_id: str
    payer_name: str
    amount: float
    issued_on: date
    due_date: date


@dataclass
class SynthWire:
    wire_id: str
    amount: float
    received_on: date
    memo: str
    sender_name: str
    truth_payer_id: str | None  # None for decoys
    truth_invoice_ids: list[str] = field(default_factory=list)
    truth_label: str = "single"  # "single" | "bundle" | "partial" | "noisy" | "decoy"


@dataclass
class SynthDataset:
    invoices: list[SynthInvoice]
    wires: list[SynthWire]
    aliases: dict[str, str]  # alias -> payer_id (the canonical map)


def _typo(s: str, rng: random.Random) -> str:
    """Drop one char with 50% prob, swap two chars with 50%."""
    if len(s) < 4:
        return s
    if rng.random() < 0.5:
        i = rng.randrange(len(s))
        return s[:i] + s[i + 1 :]
    i = rng.randrange(len(s) - 1)
    return s[:i] + s[i + 1] + s[i] + s[i + 2 :]


def _wire_id(rng: random.Random) -> str:
    return "WIRE-" + "".join(rng.choices(string.digits, k=8))


def generate(
    seed: int = 42,
    n_invoices: int = 100,
    n_wires: int = 80,
    today: date | None = None,
) -> SynthDataset:
    """Build a deterministic dataset for training / demos."""
    rng = random.Random(seed)
    today = today or date(2026, 5, 1)

    invoices: list[SynthInvoice] = []
    for i in range(n_invoices):
        payer_id, payer_name, _aliases = rng.choice(PAYER_FIXTURES)
        issued = today - timedelta(days=rng.randint(7, 60))
        due = issued + timedelta(days=rng.choice([15, 30, 45]))
        amount = round(rng.uniform(500, 25_000), 2)
        invoices.append(
            SynthInvoice(
                invoice_id=f"INV-{1000 + i}",
                payer_id=payer_id,
                payer_name=payer_name,
                amount=amount,
                issued_on=issued,
                due_date=due,
            )
        )

    # Precompute open invoices per payer for sampling.
    invoices_by_payer: dict[str, list[SynthInvoice]] = {}
    for inv in invoices:
        invoices_by_payer.setdefault(inv.payer_id, []).append(inv)

    aliases: dict[str, str] = {}
    for payer_id, _, alias_list in PAYER_FIXTURES:
        for a in alias_list:
            aliases[a] = payer_id

    wires: list[SynthWire] = []
    for _ in range(n_wires):
        roll = rng.random()
        if roll < 0.60:
            # Single clean match.
            payer_id = rng.choice(list(invoices_by_payer.keys()))
            inv = rng.choice(invoices_by_payer[payer_id])
            sender = rng.choice([a for a in aliases if aliases[a] == payer_id])
            memo = f"PAYMENT {inv.invoice_id} {sender}"
            wires.append(
                SynthWire(
                    wire_id=_wire_id(rng),
                    amount=inv.amount,
                    received_on=inv.due_date + timedelta(days=rng.randint(-3, 7)),
                    memo=memo,
                    sender_name=sender,
                    truth_payer_id=payer_id,
                    truth_invoice_ids=[inv.invoice_id],
                    truth_label="single",
                )
            )
        elif roll < 0.75:
            # Bundle: 2-3 invoices same payer in one wire.
            payer_id = rng.choice(list(invoices_by_payer.keys()))
            payer_invs = invoices_by_payer[payer_id]
            if len(payer_invs) < 2:
                continue
            k = min(len(payer_invs), rng.choice([2, 3]))
            chosen = rng.sample(payer_invs, k)
            sender = rng.choice([a for a in aliases if aliases[a] == payer_id])
            wires.append(
                SynthWire(
                    wire_id=_wire_id(rng),
                    amount=round(sum(c.amount for c in chosen), 2),
                    received_on=max(c.due_date for c in chosen)
                    + timedelta(days=rng.randint(-2, 10)),
                    memo=f"BULK PMT {sender}",
                    sender_name=sender,
                    truth_payer_id=payer_id,
                    truth_invoice_ids=[c.invoice_id for c in chosen],
                    truth_label="bundle",
                )
            )
        elif roll < 0.85:
            # Partial: wire amount = invoice * 0.3..0.8.
            payer_id = rng.choice(list(invoices_by_payer.keys()))
            inv = rng.choice(invoices_by_payer[payer_id])
            sender = rng.choice([a for a in aliases if aliases[a] == payer_id])
            partial_amt = round(inv.amount * rng.uniform(0.3, 0.8), 2)
            wires.append(
                SynthWire(
                    wire_id=_wire_id(rng),
                    amount=partial_amt,
                    received_on=inv.due_date + timedelta(days=rng.randint(-3, 14)),
                    memo=f"PARTIAL {inv.invoice_id} {sender}",
                    sender_name=sender,
                    truth_payer_id=payer_id,
                    truth_invoice_ids=[inv.invoice_id],
                    truth_label="partial",
                )
            )
        elif roll < 0.95:
            # Noisy memo: typo'd payer, no invoice id at all.
            payer_id = rng.choice(list(invoices_by_payer.keys()))
            inv = rng.choice(invoices_by_payer[payer_id])
            sender_canon = rng.choice([a for a in aliases if aliases[a] == payer_id])
            sender = _typo(sender_canon, rng)
            wires.append(
                SynthWire(
                    wire_id=_wire_id(rng),
                    amount=inv.amount,
                    received_on=inv.due_date + timedelta(days=rng.randint(-1, 5)),
                    memo=sender,  # nothing useful in memo
                    sender_name=sender,
                    truth_payer_id=payer_id,
                    truth_invoice_ids=[inv.invoice_id],
                    truth_label="noisy",
                )
            )
        else:
            # Decoy: real bank wire but not ours.
            sender = rng.choice(DECOY_NAMES)
            wires.append(
                SynthWire(
                    wire_id=_wire_id(rng),
                    amount=round(rng.uniform(100, 5000), 2),
                    received_on=today - timedelta(days=rng.randint(0, 30)),
                    memo=sender + " " + "".join(rng.choices(string.digits, k=6)),
                    sender_name=sender,
                    truth_payer_id=None,
                    truth_invoice_ids=[],
                    truth_label="decoy",
                )
            )

    return SynthDataset(invoices=invoices, wires=wires, aliases=aliases)
