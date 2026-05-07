"""Map a wire's memo / sender_name string to a canonical payer_id.

Two layers, in order of preference:

    1. PayerAlias table — exact (case-insensitive) hit on a known alias.
       This is the fast path and the highest confidence.
    2. rapidfuzz fallback — best fuzzy match across all known aliases
       (and payer.name). Returns a payer_id only if score >= threshold.

The threshold is conservative on purpose. Borderline matches go to the
review queue with payer=None instead of being silently misrouted.

Production note: every confirmed human review for a previously-unseen
sender string can be persisted as a PayerAlias with source="learned",
which expands the table over time without a feature flag.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz, process
from sqlmodel import Session, select

from cash_recon.models import PayerAlias
from shared.models import Payer

DEFAULT_FUZZY_THRESHOLD = 86  # rapidfuzz token_set_ratio (0..100)


@dataclass
class ResolutionResult:
    payer_id: str | None
    method: str  # "exact_alias" | "fuzzy_alias" | "fuzzy_payer_name" | "no_match"
    score: float  # 0..100
    matched_against: str | None = None


def _normalize(s: str) -> str:
    return s.strip().upper()


def resolve(
    session: Session,
    *,
    memo: str = "",
    sender_name: str = "",
    threshold: int = DEFAULT_FUZZY_THRESHOLD,
) -> ResolutionResult:
    """Find the best payer_id for this wire string."""
    # Try sender_name first, fall back to memo. Many bank feeds put the
    # canonical name in sender and only descriptors in memo.
    haystacks: list[str] = []
    if sender_name:
        haystacks.append(_normalize(sender_name))
    if memo:
        haystacks.append(_normalize(memo))
    if not haystacks:
        return ResolutionResult(payer_id=None, method="no_match", score=0.0)

    # 1. Exact alias hit.
    aliases = list(session.exec(select(PayerAlias)))
    alias_by_key: dict[str, str] = {_normalize(a.alias): a.payer_id for a in aliases}
    for h in haystacks:
        if h in alias_by_key:
            return ResolutionResult(
                payer_id=alias_by_key[h],
                method="exact_alias",
                score=100.0,
                matched_against=h,
            )
        # Exact substring of an alias is also a strong signal.
        for ali, pid in alias_by_key.items():
            if ali in h:
                return ResolutionResult(
                    payer_id=pid, method="exact_alias",
                    score=99.0, matched_against=ali,
                )

    # 2. Fuzzy match over alias strings.
    if alias_by_key:
        alias_keys = list(alias_by_key.keys())
        best = process.extractOne(
            haystacks[0], alias_keys, scorer=fuzz.token_set_ratio
        )
        if best is not None and best[1] >= threshold:
            ali, score, _ = best
            return ResolutionResult(
                payer_id=alias_by_key[ali],
                method="fuzzy_alias",
                score=float(score),
                matched_against=ali,
            )

    # 3. Fuzzy match over payer.name.
    payers = list(session.exec(select(Payer)))
    name_by_id = {_normalize(p.name): p.payer_id for p in payers}
    if name_by_id:
        best = process.extractOne(
            haystacks[0], list(name_by_id.keys()), scorer=fuzz.token_set_ratio
        )
        if best is not None and best[1] >= threshold:
            n, score, _ = best
            return ResolutionResult(
                payer_id=name_by_id[n],
                method="fuzzy_payer_name",
                score=float(score),
                matched_against=n,
            )

    return ResolutionResult(payer_id=None, method="no_match", score=0.0)


def learn_alias(
    session: Session,
    *,
    alias: str,
    payer_id: str,
) -> PayerAlias:
    """Persist a (alias -> payer_id) mapping learned from a human review.

    Idempotent: if the alias already exists for the same payer, no-op.
    """
    norm = _normalize(alias)
    existing = list(
        session.exec(
            select(PayerAlias).where(PayerAlias.alias == norm).where(
                PayerAlias.payer_id == payer_id
            )
        )
    )
    if existing:
        return existing[0]
    row = PayerAlias(alias=norm, payer_id=payer_id, source="learned")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
