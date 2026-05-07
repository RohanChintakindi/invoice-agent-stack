"""Personality state machine for the collections voice agent.

State is a (phase, tone) tuple. Phase moves through the dunning lifecycle;
tone modulates delivery. Initial state is derived from per-payer context
(trust score, days overdue, broken promises). Intra-call transitions
react to signals detected during the conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Phase(str, Enum):
    FRIENDLY_REMINDER = "friendly_reminder"
    FIRM_FOLLOWUP = "firm_followup"
    ESCALATION = "escalation"
    PRE_LEGAL = "pre_legal"
    PAUSED = "paused"


class Tone(str, Enum):
    WARM = "warm"
    PROFESSIONAL = "professional"
    FIRM = "firm"
    COLD = "cold"


# Ordered cold → warm so we can step in either direction.
_TONE_LADDER: list[Tone] = [Tone.COLD, Tone.FIRM, Tone.PROFESSIONAL, Tone.WARM]


def _soften(tone: Tone) -> Tone:
    idx = _TONE_LADDER.index(tone)
    return _TONE_LADDER[min(idx + 1, len(_TONE_LADDER) - 1)]


def _harden(tone: Tone) -> Tone:
    idx = _TONE_LADDER.index(tone)
    return _TONE_LADDER[max(idx - 1, 0)]


@dataclass(frozen=True)
class State:
    phase: Phase
    tone: Tone
    reason: str = ""


@dataclass(frozen=True)
class PayerContext:
    """Inputs used to compute the initial call state."""

    trust_score: float
    days_overdue: int
    broken_promises: int
    has_active_promise: bool = False
    prior_call_count: int = 0


class IntraCallSignal(str, Enum):
    HOSTILE_SENTIMENT = "hostile_sentiment"
    POSITIVE_RESPONSE = "positive_response"
    DISPUTE_MENTIONED = "dispute_mentioned"
    MANAGER_REQUESTED = "manager_requested"
    HESITATION_DETECTED = "hesitation_detected"
    PAYMENT_COMMITTED = "payment_committed"
    BANKRUPTCY_MENTIONED = "bankruptcy_mentioned"


def initial_state(ctx: PayerContext) -> State:
    if ctx.has_active_promise:
        return State(Phase.PAUSED, Tone.PROFESSIONAL, "active payment promise on file")

    phase = _initial_phase(ctx)
    tone = _initial_tone(ctx, phase)
    reason = (
        f"trust={ctx.trust_score:.2f} overdue={ctx.days_overdue}d "
        f"broken_promises={ctx.broken_promises}"
    )
    return State(phase, tone, reason)


def _initial_phase(ctx: PayerContext) -> Phase:
    # Multiple broken promises is the strongest signal — bypass time-based bumps.
    if ctx.broken_promises >= 2:
        return Phase.PRE_LEGAL if ctx.days_overdue >= 60 else Phase.ESCALATION
    if ctx.days_overdue >= 60:
        return Phase.PRE_LEGAL
    if ctx.broken_promises >= 1 and ctx.days_overdue >= 30:
        return Phase.PRE_LEGAL
    if ctx.days_overdue >= 30:
        return Phase.ESCALATION
    if ctx.days_overdue >= 8 or ctx.broken_promises >= 1:
        return Phase.FIRM_FOLLOWUP
    return Phase.FRIENDLY_REMINDER


def _initial_tone(ctx: PayerContext, phase: Phase) -> Tone:
    # Late-stage phases don't allow warm tones regardless of trust.
    if phase == Phase.PRE_LEGAL:
        return Tone.COLD if ctx.trust_score < 0.40 else Tone.FIRM
    if phase == Phase.ESCALATION:
        return Tone.FIRM if ctx.trust_score < 0.60 else Tone.PROFESSIONAL

    # Early-stage phases scale tone with trust.
    if ctx.trust_score >= 0.85:
        return Tone.WARM
    if ctx.trust_score >= 0.65:
        return Tone.PROFESSIONAL
    if ctx.trust_score >= 0.45:
        return Tone.FIRM if phase == Phase.FIRM_FOLLOWUP else Tone.PROFESSIONAL
    if ctx.trust_score >= 0.25:
        return Tone.FIRM
    return Tone.COLD


def transition_intra_call(state: State, signal: IntraCallSignal) -> State:
    """Apply a real-time signal observed during the call."""

    if state.phase == Phase.PAUSED:
        if signal == IntraCallSignal.BANKRUPTCY_MENTIONED:
            return State(Phase.PRE_LEGAL, Tone.FIRM, "bankruptcy mentioned during paused phase")
        return state

    if signal == IntraCallSignal.HOSTILE_SENTIMENT:
        return State(state.phase, _soften(state.tone), "hostile sentiment → de-escalate")

    if signal == IntraCallSignal.POSITIVE_RESPONSE:
        return State(state.phase, _soften(state.tone), "positive engagement → soften")

    if signal == IntraCallSignal.PAYMENT_COMMITTED:
        return State(Phase.PAUSED, Tone.PROFESSIONAL, "payment commitment received")

    if signal == IntraCallSignal.BANKRUPTCY_MENTIONED:
        return State(Phase.PRE_LEGAL, Tone.PROFESSIONAL, "bankruptcy mentioned")

    if signal == IntraCallSignal.DISPUTE_MENTIONED:
        return State(state.phase, state.tone, "dispute mentioned — orchestrator should branch")

    if signal == IntraCallSignal.MANAGER_REQUESTED:
        return State(state.phase, state.tone, "manager requested — schedule callback")

    if signal == IntraCallSignal.HESITATION_DETECTED:
        return State(state.phase, state.tone, "hesitation detected — probe for blocker")

    return state
