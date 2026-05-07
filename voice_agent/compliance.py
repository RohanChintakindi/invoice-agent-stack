"""Compliance filter for outgoing assistant utterances.

Two-pass:
  - Regex pre-screen catches obvious violations (threats, false claims,
    third-party disclosure language, profanity). Fast and deterministic.
  - LLM consistency check catches subtler violations the regex misses
    (implied threats, misleading urgency, fabricated consequences).

Returns ComplianceResult with a verdict (pass/warn/block) and the
matched rules. The orchestrator decides whether to retry generation
with stricter constraints, sanitize, or hand off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True)
class ComplianceResult:
    verdict: Verdict
    rules_triggered: list[str] = field(default_factory=list)
    rationale: str = ""


# Each rule: (id, pattern, verdict, description)
_RULES: list[tuple[str, re.Pattern[str], Verdict, str]] = [
    (
        "threat.legal",
        re.compile(
            r"\b(sue|lawsuit|take\s+you\s+to\s+court|legal\s+action|file\s+suit)\b",
            re.IGNORECASE,
        ),
        Verdict.BLOCK,
        "Implied or explicit legal threat",
    ),
    (
        "threat.credit",
        re.compile(
            r"\b(report\s+(this\s+)?to\s+(the\s+)?(credit|bureau|equifax|transunion|experian)|"
            r"ruin\s+your\s+credit|credit\s+(score|history)\s+will)\b",
            re.IGNORECASE,
        ),
        Verdict.BLOCK,
        "Credit-reporting threat",
    ),
    (
        "threat.criminal",
        re.compile(
            r"\b(jail|prison|arrest|criminal\s+charges|prosecut(e|ion))\b",
            re.IGNORECASE,
        ),
        Verdict.BLOCK,
        "Criminal threat",
    ),
    (
        "threat.assets",
        re.compile(
            r"\b(seize|garnish|repossess|come\s+(after|for)\s+your)\b",
            re.IGNORECASE,
        ),
        Verdict.BLOCK,
        "Asset/garnishment threat",
    ),
    (
        "third_party.disclosure",
        re.compile(
            r"\b(tell\s+(your|their)\s+(spouse|husband|wife|family|coworker|"
            r"boss|employer)|inform\s+(your|the)\s+(employer|family))\b",
            re.IGNORECASE,
        ),
        Verdict.BLOCK,
        "Third-party debt disclosure",
    ),
    (
        "false.urgency",
        re.compile(
            r"\b(final\s+notice|last\s+chance|24\s+hours\s+or)\b",
            re.IGNORECASE,
        ),
        Verdict.WARN,
        "Possibly misleading urgency claim",
    ),
    (
        "profanity",
        re.compile(r"\b(damn|hell|crap|shit|fuck|ass(hole)?)\b", re.IGNORECASE),
        Verdict.BLOCK,
        "Profanity / unprofessional language",
    ),
]


def regex_screen(text: str) -> ComplianceResult:
    """Fast deterministic pre-screen on a candidate outgoing utterance."""
    triggered: list[str] = []
    worst = Verdict.PASS

    for rule_id, pattern, rule_verdict, _description in _RULES:
        if pattern.search(text):
            triggered.append(rule_id)
            if rule_verdict == Verdict.BLOCK:
                worst = Verdict.BLOCK
            elif rule_verdict == Verdict.WARN and worst != Verdict.BLOCK:
                worst = Verdict.WARN

    rationale = ""
    if triggered:
        rationale = "Triggered rules: " + ", ".join(triggered)

    return ComplianceResult(verdict=worst, rules_triggered=triggered, rationale=rationale)


# ---- LLM consistency check ---------------------------------------------------

CONSISTENCY_PROMPT = """\
You are a compliance reviewer for a collections-call agent. Review the
proposed outgoing utterance below. Flag any of these problems:

  - Threats of legal action, credit reporting, criminal charges, or
    asset seizure (real or implied).
  - Fabricated consequences not in the contract.
  - Misleading urgency ("final notice" when it isn't).
  - Statements that disclose the debt to a third party.
  - Profanity, condescension, or unprofessional tone.
  - Lies about the amount, due date, or contract terms.

Proposed utterance: {text}

Respond in exactly this format:
verdict: <pass|warn|block>
why: <one short sentence; required if verdict is not pass>
"""


def parse_consistency_response(raw: str) -> ComplianceResult:
    verdict = Verdict.PASS
    rationale = ""

    for line in raw.strip().splitlines():
        line = line.strip()
        if line.lower().startswith("verdict:"):
            value = line.split(":", 1)[1].strip().lower()
            try:
                verdict = Verdict(value)
            except ValueError:
                pass
        elif line.lower().startswith("why:"):
            rationale = line.split(":", 1)[1].strip()

    return ComplianceResult(verdict=verdict, rationale=rationale)


def combine(regex_result: ComplianceResult, llm_result: ComplianceResult) -> ComplianceResult:
    """Worst-case combine: a block from either path is a block."""
    order = {Verdict.PASS: 0, Verdict.WARN: 1, Verdict.BLOCK: 2}
    if order[llm_result.verdict] > order[regex_result.verdict]:
        worst = llm_result.verdict
    else:
        worst = regex_result.verdict

    rules = list(regex_result.rules_triggered)
    if llm_result.verdict != Verdict.PASS and "llm.consistency" not in rules:
        rules.append("llm.consistency")

    rationale_parts = []
    if regex_result.rationale:
        rationale_parts.append(regex_result.rationale)
    if llm_result.rationale:
        rationale_parts.append(f"LLM: {llm_result.rationale}")

    return ComplianceResult(
        verdict=worst,
        rules_triggered=rules,
        rationale="; ".join(rationale_parts),
    )
