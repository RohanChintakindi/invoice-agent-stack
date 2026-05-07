"""Intra-call signal classification.

Two paths:
  - classify_hard_signal: deterministic regex pass for branch-critical
    phrases (manager request, dispute, bankruptcy, payment commitment).
    These need exact-match precision so we don't miss a branch.
  - classify_sentiment: LLM-backed nuanced sentiment (hostile / positive /
    hesitant / neutral). Soft signals — wrong calls are recoverable.

The orchestrator runs the hard pass synchronously and the LLM pass in
parallel with the response-generation call to stay inside latency budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from voice_agent.state_machine import IntraCallSignal


# Order matters: more-specific patterns should come first.
_HARD_SIGNAL_PATTERNS: list[tuple[re.Pattern[str], IntraCallSignal]] = [
    # Bankruptcy: catch direct mentions
    (
        re.compile(r"\b(bankrupt(cy)?|chapter\s*(7|11|13)|insolven(t|cy))\b", re.IGNORECASE),
        IntraCallSignal.BANKRUPTCY_MENTIONED,
    ),
    # Dispute: payer challenges the invoice itself
    (
        re.compile(
            r"\b(dispute|disagree|never\s+received|already\s+paid|wrong\s+amount|"
            r"amount\s+is\s+wrong|didn'?t\s+(receive|get)|not\s+correct)\b",
            re.IGNORECASE,
        ),
        IntraCallSignal.DISPUTE_MENTIONED,
    ),
    # Manager: callback handler
    (
        re.compile(
            r"\b(my\s+manager|let\s+me\s+ask|talk\s+to\s+(my\s+)?(manager|boss|cfo|controller)|"
            r"escalat(e|ion)|need\s+to\s+check\s+with)\b",
            re.IGNORECASE,
        ),
        IntraCallSignal.MANAGER_REQUESTED,
    ),
    # Payment commitment
    (
        re.compile(
            r"\b((I|we)('ll| will)\s+(pay|wire|send|process)|"
            r"pay(ing|ment)\s+(by|on|tomorrow|this\s+week|today|tonight))\b",
            re.IGNORECASE,
        ),
        IntraCallSignal.PAYMENT_COMMITTED,
    ),
    # Hesitation
    (
        re.compile(
            r"\b(let\s+me\s+(check|see|look)|I\s+(need|have)\s+to\s+|"
            r"I'?m\s+not\s+sure|I\s+don'?t\s+know|hold\s+on)\b",
            re.IGNORECASE,
        ),
        IntraCallSignal.HESITATION_DETECTED,
    ),
]


def classify_hard_signal(text: str) -> IntraCallSignal | None:
    """Return the first matching hard signal, or None."""
    for pattern, signal in _HARD_SIGNAL_PATTERNS:
        if pattern.search(text):
            return signal
    return None


# ---- LLM-backed soft sentiment ----------------------------------------------


class Sentiment(str, Enum):
    HOSTILE = "hostile"
    POSITIVE = "positive"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class SentimentResult:
    sentiment: Sentiment
    confidence: float
    rationale: str = ""


SENTIMENT_PROMPT = """\
Classify the sentiment of the following utterance from someone receiving
a collections call. Respond with one of: hostile, positive, neutral.

- hostile: angry, irritated, defensive, pushing back hard
- positive: cooperative, apologetic, agreeable, problem-solving
- neutral: businesslike, factual, no strong emotion either way

Utterance: {text}

Respond in exactly this format:
sentiment: <hostile|positive|neutral>
confidence: <0.0-1.0>
why: <one short sentence>
"""


def parse_sentiment_response(raw: str) -> SentimentResult:
    """Parse the structured response; tolerate minor format drift."""
    sentiment = Sentiment.NEUTRAL
    confidence = 0.5
    rationale = ""

    for line in raw.strip().splitlines():
        line = line.strip()
        if line.lower().startswith("sentiment:"):
            value = line.split(":", 1)[1].strip().lower()
            if value in (s.value for s in Sentiment):
                sentiment = Sentiment(value)
        elif line.lower().startswith("confidence:"):
            try:
                confidence = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.lower().startswith("why:"):
            rationale = line.split(":", 1)[1].strip()

    return SentimentResult(sentiment=sentiment, confidence=confidence, rationale=rationale)


def sentiment_to_signal(sentiment: SentimentResult) -> IntraCallSignal | None:
    """Map sentiment to the corresponding intra-call signal, if any."""
    if sentiment.confidence < 0.6:
        return None
    if sentiment.sentiment == Sentiment.HOSTILE:
        return IntraCallSignal.HOSTILE_SENTIMENT
    if sentiment.sentiment == Sentiment.POSITIVE:
        return IntraCallSignal.POSITIVE_RESPONSE
    return None
