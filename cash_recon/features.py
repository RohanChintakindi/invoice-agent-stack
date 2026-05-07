"""Per-(wire, invoice) feature engineering.

Features are intentionally kept small + interpretable so an XGBoost model
trains well on a small synthetic dataset and so a reviewer can read why
the model fired.

Numeric (8 features in `FEATURE_NAMES`):
    amount_delta_abs       |wire - inv| (raw $)
    amount_delta_rel       |wire - inv| / inv (0..inf, capped at 5)
    amount_exact_match     1 if within 0.01
    days_delta             received_on - due_date (signed)
    days_delta_abs         |received_on - due_date|
    memo_id_score          rapidfuzz partial_ratio(memo, invoice_id)/100
    memo_payer_score       rapidfuzz partial_ratio(memo, payer_name)/100
    sender_payer_score     rapidfuzz token_set_ratio(sender, payer_name)/100

Order is fixed so the trained model can be reused at inference.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from rapidfuzz import fuzz

FEATURE_NAMES = [
    "amount_delta_abs",
    "amount_delta_rel",
    "amount_exact_match",
    "days_delta",
    "days_delta_abs",
    "memo_id_score",
    "memo_payer_score",
    "sender_payer_score",
]


@dataclass
class WireView:
    """Read-only projection of a wire used by feature code."""
    wire_id: str
    amount: float
    received_on: date
    memo: str
    sender_name: str


@dataclass
class InvoiceView:
    """Read-only projection of an invoice used by feature code."""
    invoice_id: str
    payer_id: str
    payer_name: str
    amount: float
    due_date: date


def candidates_for_wire(
    wire: WireView,
    invoices: list[InvoiceView],
    *,
    payer_id: str | None,
    amount_tolerance_rel: float = 0.5,
    days_tolerance: int = 60,
) -> list[InvoiceView]:
    """Filter invoices to plausible matches for one wire.

    Two stages of filtering:

    1. Payer scope. If ER gave us a payer_id, restrict to that payer's
       invoices; otherwise consider every payer (slow path, used as a
       fallback when entity resolution fails).
    2. Amount + date windows. Within +/- amount_tolerance_rel and
       +/- days_tolerance.

    The model is allowed to see borderline candidates because the
    ranker job is to discriminate between "amount 1% off, memo matches"
    (likely partial) and "amount 1% off, memo unrelated" (probably not).
    """
    if payer_id is not None:
        scope = [inv for inv in invoices if inv.payer_id == payer_id]
    else:
        scope = invoices

    out: list[InvoiceView] = []
    for inv in scope:
        if inv.amount <= 0:
            continue
        rel = abs(wire.amount - inv.amount) / inv.amount
        if rel > amount_tolerance_rel:
            # Even allow bundle pieces — bundler runs after this so we
            # only need *some* hope here. A wire double the invoice is
            # too far for a single candidate but bundler will catch it.
            continue
        days = (wire.received_on - inv.due_date).days
        if abs(days) > days_tolerance:
            continue
        out.append(inv)
    return out


def featurize(wire: WireView, inv: InvoiceView) -> list[float]:
    """Return features in `FEATURE_NAMES` order."""
    amount_delta = abs(wire.amount - inv.amount)
    amount_delta_rel = amount_delta / inv.amount if inv.amount > 0 else 0.0
    days_delta = (wire.received_on - inv.due_date).days

    # rapidfuzz returns 0..100; we normalise to [0,1].
    memo_id_score = fuzz.partial_ratio(wire.memo.lower(), inv.invoice_id.lower()) / 100
    memo_payer_score = fuzz.partial_ratio(wire.memo.lower(), inv.payer_name.lower()) / 100
    sender_payer_score = (
        fuzz.token_set_ratio(wire.sender_name.lower(), inv.payer_name.lower()) / 100
    )

    return [
        amount_delta,
        min(amount_delta_rel, 5.0),
        1.0 if amount_delta < 0.01 else 0.0,
        float(days_delta),
        float(abs(days_delta)),
        memo_id_score,
        memo_payer_score,
        sender_payer_score,
    ]


def featurize_to_dict(wire: WireView, inv: InvoiceView) -> dict[str, float]:
    """Same as featurize() but returned as a name->value dict.

    Used when persisting features alongside a candidate so a reviewer
    can read them in the dashboard.
    """
    values = featurize(wire, inv)
    return dict(zip(FEATURE_NAMES, values, strict=True))
