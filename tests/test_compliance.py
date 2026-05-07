from voice_agent.compliance import (
    ComplianceResult,
    Verdict,
    combine,
    parse_consistency_response,
    regex_screen,
)


# ---- regex screen ------------------------------------------------------------


def test_clean_utterance_passes():
    text = "I just wanted to follow up on invoice 1023 — could we get a payment date?"
    assert regex_screen(text).verdict == Verdict.PASS


def test_legal_threat_blocks():
    text = "If you don't pay we'll sue you next week."
    result = regex_screen(text)
    assert result.verdict == Verdict.BLOCK
    assert "threat.legal" in result.rules_triggered


def test_credit_threat_blocks():
    text = "We'll report this to the credit bureau if it isn't paid."
    result = regex_screen(text)
    assert result.verdict == Verdict.BLOCK
    assert "threat.credit" in result.rules_triggered


def test_criminal_threat_blocks():
    text = "You could face criminal charges over this."
    result = regex_screen(text)
    assert result.verdict == Verdict.BLOCK
    assert "threat.criminal" in result.rules_triggered


def test_third_party_disclosure_blocks():
    text = "If we don't hear back we'll have to inform your employer about this debt."
    result = regex_screen(text)
    assert result.verdict == Verdict.BLOCK
    assert "third_party.disclosure" in result.rules_triggered


def test_profanity_blocks():
    text = "Pay the damn invoice."
    result = regex_screen(text)
    assert result.verdict == Verdict.BLOCK
    assert "profanity" in result.rules_triggered


def test_misleading_urgency_warns_only():
    text = "Just so you know, this is your final notice on the matter."
    result = regex_screen(text)
    assert result.verdict == Verdict.WARN
    assert "false.urgency" in result.rules_triggered


# ---- LLM response parser -----------------------------------------------------


def test_parse_pass_verdict():
    raw = "verdict: pass\nwhy: looks fine\n"
    result = parse_consistency_response(raw)
    assert result.verdict == Verdict.PASS


def test_parse_block_verdict():
    raw = "verdict: block\nwhy: contains an implied legal threat\n"
    result = parse_consistency_response(raw)
    assert result.verdict == Verdict.BLOCK
    assert "implied legal threat" in result.rationale


def test_parse_unknown_verdict_defaults_to_pass():
    raw = "verdict: unclear\n"
    result = parse_consistency_response(raw)
    assert result.verdict == Verdict.PASS


# ---- combine -----------------------------------------------------------------


def test_combine_block_wins_over_pass():
    regex = ComplianceResult(verdict=Verdict.PASS)
    llm = ComplianceResult(verdict=Verdict.BLOCK, rationale="implied threat")
    combined = combine(regex, llm)
    assert combined.verdict == Verdict.BLOCK


def test_combine_warn_does_not_override_block():
    regex = ComplianceResult(verdict=Verdict.BLOCK, rules_triggered=["threat.legal"])
    llm = ComplianceResult(verdict=Verdict.WARN)
    combined = combine(regex, llm)
    assert combined.verdict == Verdict.BLOCK
    assert "threat.legal" in combined.rules_triggered
