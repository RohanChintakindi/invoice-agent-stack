"""Candidate generation + featurize behave correctly."""

from __future__ import annotations

from datetime import date

from cash_recon.features import (
    FEATURE_NAMES,
    InvoiceView,
    WireView,
    candidates_for_wire,
    featurize,
    featurize_to_dict,
)


def _wire(amount: float = 1000.0, memo: str = "INV-1023 ACME") -> WireView:
    return WireView(
        wire_id="W1", amount=amount, received_on=date(2026, 5, 1),
        memo=memo, sender_name="ACME CORP",
    )


def _inv(invoice_id="INV-1023", amount=1000.0, payer_id="acme") -> InvoiceView:
    return InvoiceView(
        invoice_id=invoice_id, payer_id=payer_id, payer_name="Acme Corp",
        amount=amount, due_date=date(2026, 4, 28),
    )


def test_featurize_returns_correct_length_and_values():
    feats = featurize(_wire(), _inv())
    assert len(feats) == len(FEATURE_NAMES)
    feats_dict = featurize_to_dict(_wire(), _inv())
    assert feats_dict["amount_exact_match"] == 1.0
    assert feats_dict["memo_id_score"] > 0.9
    assert feats_dict["memo_payer_score"] > 0.5


def test_candidates_filter_by_payer_when_provided():
    inv_ours = _inv("INV-1", 1000.0, "acme")
    inv_other = _inv("INV-2", 1000.0, "zenith")
    cands = candidates_for_wire(_wire(), [inv_ours, inv_other], payer_id="acme")
    assert [c.invoice_id for c in cands] == ["INV-1"]


def test_candidates_skip_amount_far_outside_window():
    inv_close = _inv("INV-1", 1010.0, "acme")
    inv_far = _inv("INV-2", 5000.0, "acme")
    cands = candidates_for_wire(_wire(amount=1000.0), [inv_close, inv_far], payer_id="acme")
    assert [c.invoice_id for c in cands] == ["INV-1"]


def test_candidates_include_when_payer_unknown():
    inv = _inv("INV-1", 1000.0, "acme")
    cands = candidates_for_wire(_wire(), [inv], payer_id=None)
    assert len(cands) == 1


def test_amount_delta_features_signed_and_unsigned():
    feats = featurize_to_dict(_wire(amount=900.0), _inv("INV-1", 1000.0))
    assert feats["amount_delta_abs"] == 100.0
    assert abs(feats["amount_delta_rel"] - 0.1) < 1e-6
    assert feats["amount_exact_match"] == 0.0
