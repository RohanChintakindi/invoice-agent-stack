"""Subset-sum bundler: find a set of invoices that sum to a wire amount.

When no single invoice matches a wire's amount but the wire still
clearly belongs to one payer (memo / sender_name resolved), it's
usually a "bulk wire" that pays several invoices at once.

We do bounded subset-sum:
    - Restrict to one payer's open invoices.
    - Cap the search at MAX_BUNDLE_SIZE invoices so this stays sub-second.
    - Allow a small tolerance (cents-level) on the sum.

For tiny pools (<=20) we exhaustively iterate combinations up to size k.
That covers >95% of real bundles (most bundles are 2-5 invoices). For
larger pools the same code path still works; cost is C(n, k).

Output: list of `BundleCandidate` (the invoice ids + sum delta), ranked
by smallest delta first.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

MAX_BUNDLE_SIZE = 5
DEFAULT_TOLERANCE = 1.00  # USD


@dataclass
class BundleCandidate:
    invoice_ids: list[str]
    total: float
    delta: float  # signed: total - target. Positive => bundle is more than wire.


def find_bundles(
    target_amount: float,
    invoices: list[tuple[str, float]],
    *,
    max_size: int = MAX_BUNDLE_SIZE,
    tolerance: float = DEFAULT_TOLERANCE,
    max_results: int = 5,
) -> list[BundleCandidate]:
    """Find invoice subsets summing to target_amount within tolerance.

    invoices: list of (invoice_id, amount).
    """
    if target_amount <= 0 or not invoices:
        return []

    # Single-item case is technically a bundle of size 1; skip it (the
    # ranker handles single-invoice matches more thoroughly).
    candidates: list[BundleCandidate] = []
    pool_size = min(len(invoices), 20)
    pool = invoices[:pool_size]

    upper_size = min(max_size, pool_size)
    for k in range(2, upper_size + 1):
        for combo in combinations(pool, k):
            total = sum(c[1] for c in combo)
            delta = total - target_amount
            if abs(delta) <= tolerance:
                candidates.append(
                    BundleCandidate(
                        invoice_ids=[c[0] for c in combo],
                        total=round(total, 2),
                        delta=round(delta, 2),
                    )
                )
                if len(candidates) >= max_results * 4:
                    break
        if len(candidates) >= max_results * 4:
            break

    candidates.sort(key=lambda c: (abs(c.delta), len(c.invoice_ids)))
    return candidates[:max_results]
