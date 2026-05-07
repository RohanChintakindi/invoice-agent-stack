from voice_agent.state_machine import (
    IntraCallSignal,
    PayerContext,
    Phase,
    State,
    Tone,
    initial_state,
    transition_intra_call,
)


# ---- initial_state: phase ----------------------------------------------------


def test_high_trust_zero_overdue_is_friendly_reminder():
    ctx = PayerContext(trust_score=0.9, days_overdue=3, broken_promises=0)
    assert initial_state(ctx).phase == Phase.FRIENDLY_REMINDER


def test_eight_days_overdue_is_firm_followup():
    ctx = PayerContext(trust_score=0.7, days_overdue=10, broken_promises=0)
    assert initial_state(ctx).phase == Phase.FIRM_FOLLOWUP


def test_thirty_days_overdue_is_escalation():
    ctx = PayerContext(trust_score=0.7, days_overdue=35, broken_promises=0)
    assert initial_state(ctx).phase == Phase.ESCALATION


def test_sixty_days_overdue_is_pre_legal():
    ctx = PayerContext(trust_score=0.5, days_overdue=70, broken_promises=0)
    assert initial_state(ctx).phase == Phase.PRE_LEGAL


def test_two_broken_promises_force_escalation_even_when_recent():
    ctx = PayerContext(trust_score=0.7, days_overdue=12, broken_promises=2)
    assert initial_state(ctx).phase == Phase.ESCALATION


def test_one_broken_promise_plus_thirty_days_is_pre_legal():
    ctx = PayerContext(trust_score=0.5, days_overdue=35, broken_promises=1)
    assert initial_state(ctx).phase == Phase.PRE_LEGAL


def test_active_promise_pauses_regardless_of_other_signals():
    ctx = PayerContext(
        trust_score=0.2, days_overdue=45, broken_promises=1, has_active_promise=True
    )
    assert initial_state(ctx).phase == Phase.PAUSED


# ---- initial_state: tone -----------------------------------------------------


def test_high_trust_in_friendly_phase_is_warm():
    ctx = PayerContext(trust_score=0.9, days_overdue=3, broken_promises=0)
    assert initial_state(ctx).tone == Tone.WARM


def test_very_low_trust_in_friendly_phase_is_cold():
    ctx = PayerContext(trust_score=0.15, days_overdue=2, broken_promises=0)
    assert initial_state(ctx).tone == Tone.COLD


def test_pre_legal_never_warm_even_with_high_trust():
    ctx = PayerContext(trust_score=0.95, days_overdue=70, broken_promises=0)
    assert initial_state(ctx).tone in (Tone.FIRM, Tone.COLD)


def test_escalation_never_warm():
    ctx = PayerContext(trust_score=0.95, days_overdue=35, broken_promises=0)
    assert initial_state(ctx).tone in (Tone.PROFESSIONAL, Tone.FIRM, Tone.COLD)


# ---- transition_intra_call ---------------------------------------------------


def test_hostile_sentiment_softens_tone_one_step():
    state = State(Phase.FIRM_FOLLOWUP, Tone.FIRM)
    new = transition_intra_call(state, IntraCallSignal.HOSTILE_SENTIMENT)
    assert new.tone == Tone.PROFESSIONAL
    assert new.phase == Phase.FIRM_FOLLOWUP


def test_hostile_sentiment_caps_at_warm():
    state = State(Phase.FRIENDLY_REMINDER, Tone.WARM)
    new = transition_intra_call(state, IntraCallSignal.HOSTILE_SENTIMENT)
    assert new.tone == Tone.WARM


def test_payment_committed_moves_to_paused():
    state = State(Phase.ESCALATION, Tone.FIRM)
    new = transition_intra_call(state, IntraCallSignal.PAYMENT_COMMITTED)
    assert new.phase == Phase.PAUSED
    assert new.tone == Tone.PROFESSIONAL


def test_bankruptcy_jumps_to_pre_legal():
    state = State(Phase.FRIENDLY_REMINDER, Tone.WARM)
    new = transition_intra_call(state, IntraCallSignal.BANKRUPTCY_MENTIONED)
    assert new.phase == Phase.PRE_LEGAL


def test_paused_state_only_breaks_for_bankruptcy():
    state = State(Phase.PAUSED, Tone.PROFESSIONAL)
    # Most signals should not move us out of paused
    for signal in [
        IntraCallSignal.HOSTILE_SENTIMENT,
        IntraCallSignal.HESITATION_DETECTED,
        IntraCallSignal.MANAGER_REQUESTED,
        IntraCallSignal.DISPUTE_MENTIONED,
    ]:
        assert transition_intra_call(state, signal).phase == Phase.PAUSED

    # Bankruptcy is the exception
    new = transition_intra_call(state, IntraCallSignal.BANKRUPTCY_MENTIONED)
    assert new.phase == Phase.PRE_LEGAL


def test_dispute_carries_branch_signal_in_reason():
    state = State(Phase.FIRM_FOLLOWUP, Tone.PROFESSIONAL)
    new = transition_intra_call(state, IntraCallSignal.DISPUTE_MENTIONED)
    assert "dispute" in new.reason.lower()
    assert new.phase == state.phase
    assert new.tone == state.tone
