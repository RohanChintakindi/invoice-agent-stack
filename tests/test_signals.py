from voice_agent.signals import (
    Sentiment,
    classify_hard_signal,
    parse_sentiment_response,
    sentiment_to_signal,
)
from voice_agent.state_machine import IntraCallSignal


# ---- hard signal regex matching ---------------------------------------------


def test_bankruptcy_word_detected():
    assert classify_hard_signal("we're considering filing for bankruptcy") == (
        IntraCallSignal.BANKRUPTCY_MENTIONED
    )
    assert classify_hard_signal("chapter 11 might be necessary") == (
        IntraCallSignal.BANKRUPTCY_MENTIONED
    )


def test_dispute_phrases_detected():
    assert classify_hard_signal("I never received that invoice") == (
        IntraCallSignal.DISPUTE_MENTIONED
    )
    assert classify_hard_signal("we already paid that one") == IntraCallSignal.DISPUTE_MENTIONED
    assert classify_hard_signal("the amount is wrong") == IntraCallSignal.DISPUTE_MENTIONED


def test_manager_request_detected():
    assert classify_hard_signal("let me ask my manager") == IntraCallSignal.MANAGER_REQUESTED
    assert classify_hard_signal("I need to check with my boss") == (
        IntraCallSignal.MANAGER_REQUESTED
    )


def test_payment_commitment_detected():
    assert classify_hard_signal("I'll pay it tomorrow") == IntraCallSignal.PAYMENT_COMMITTED
    assert classify_hard_signal("we will wire the money this week") == (
        IntraCallSignal.PAYMENT_COMMITTED
    )


def test_hesitation_detected():
    assert classify_hard_signal("let me check on that") == IntraCallSignal.HESITATION_DETECTED
    assert classify_hard_signal("I'm not sure honestly") == IntraCallSignal.HESITATION_DETECTED


def test_neutral_phrase_returns_none():
    assert classify_hard_signal("hello, this is Karen") is None


def test_specific_signals_take_precedence_over_general():
    # "bankruptcy" should trump "let me check" if both appear
    assert classify_hard_signal("let me check, but bankruptcy is on the table") == (
        IntraCallSignal.BANKRUPTCY_MENTIONED
    )


# ---- sentiment parsing -------------------------------------------------------


def test_parse_hostile_sentiment():
    raw = "sentiment: hostile\nconfidence: 0.85\nwhy: tone is angry and defensive"
    result = parse_sentiment_response(raw)
    assert result.sentiment == Sentiment.HOSTILE
    assert result.confidence == 0.85


def test_parse_neutral_sentiment_default():
    raw = "sentiment: neutral\nconfidence: 0.7\n"
    result = parse_sentiment_response(raw)
    assert result.sentiment == Sentiment.NEUTRAL


def test_parse_malformed_response_returns_neutral():
    raw = "uhhh"
    result = parse_sentiment_response(raw)
    assert result.sentiment == Sentiment.NEUTRAL


# ---- sentiment to signal mapping --------------------------------------------


def test_high_confidence_hostile_maps_to_signal():
    raw = "sentiment: hostile\nconfidence: 0.9\n"
    result = parse_sentiment_response(raw)
    assert sentiment_to_signal(result) == IntraCallSignal.HOSTILE_SENTIMENT


def test_low_confidence_returns_no_signal():
    raw = "sentiment: hostile\nconfidence: 0.4\n"
    result = parse_sentiment_response(raw)
    assert sentiment_to_signal(result) is None


def test_neutral_returns_no_signal():
    raw = "sentiment: neutral\nconfidence: 0.95\n"
    result = parse_sentiment_response(raw)
    assert sentiment_to_signal(result) is None
