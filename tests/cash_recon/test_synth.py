"""Synthetic dataset has the documented label mix and is deterministic."""

from __future__ import annotations

from cash_recon.synth import generate


def test_generate_is_deterministic_given_seed():
    a = generate(seed=42, n_wires=50, n_invoices=80)
    b = generate(seed=42, n_wires=50, n_invoices=80)
    assert [w.wire_id for w in a.wires] == [w.wire_id for w in b.wires]
    assert [i.invoice_id for i in a.invoices] == [i.invoice_id for i in b.invoices]


def test_generate_label_mix_roughly_matches_targets():
    ds = generate(seed=42, n_wires=400, n_invoices=200)
    counts = {label: 0 for label in ("single", "bundle", "partial", "noisy", "decoy")}
    for w in ds.wires:
        counts[w.truth_label] += 1
    n = len(ds.wires)
    # Single dominates and decoys exist.
    assert counts["single"] / n > 0.45
    assert counts["decoy"] / n > 0.01


def test_decoy_wires_have_no_truth_payer():
    ds = generate(seed=42, n_wires=200, n_invoices=80)
    for w in ds.wires:
        if w.truth_label == "decoy":
            assert w.truth_payer_id is None
            assert w.truth_invoice_ids == []
        else:
            assert w.truth_payer_id is not None


def test_aliases_cover_every_payer():
    ds = generate(seed=42, n_wires=10, n_invoices=10)
    payer_ids_in_aliases = set(ds.aliases.values())
    payer_ids_in_invoices = {inv.payer_id for inv in ds.invoices}
    # Every payer that has invoices should have at least one alias.
    assert payer_ids_in_invoices.issubset(payer_ids_in_aliases)
