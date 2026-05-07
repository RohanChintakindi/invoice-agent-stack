"""Subset-sum bundler finds correct invoice combinations."""

from __future__ import annotations

from cash_recon.bundler import find_bundles


def test_finds_exact_two_invoice_bundle():
    invs = [("A", 100.0), ("B", 200.0), ("C", 150.0)]
    out = find_bundles(target_amount=300.0, invoices=invs)
    assert any(set(c.invoice_ids) == {"A", "B"} for c in out)


def test_returns_smallest_delta_first():
    invs = [("A", 100.0), ("B", 200.0), ("C", 150.0), ("D", 250.0)]
    out = find_bundles(target_amount=251.0, invoices=invs, tolerance=2.0)
    assert out
    # Best should be D (250) alone... but bundles min size is 2. So best is
    # A+C (250, delta=-1) — others tie.
    assert out[0].delta == -1.0


def test_returns_empty_when_no_combination_fits():
    invs = [("A", 100.0), ("B", 200.0)]
    out = find_bundles(target_amount=999.0, invoices=invs)
    assert out == []


def test_respects_max_size():
    invs = [("A", 25.0), ("B", 25.0), ("C", 25.0), ("D", 25.0), ("E", 25.0), ("F", 25.0)]
    out = find_bundles(target_amount=125.0, invoices=invs, max_size=4, tolerance=0.01)
    # 125 = 5 * 25 requires 5 invoices, but max_size=4 so no exact match.
    assert all(len(c.invoice_ids) <= 4 for c in out)


def test_within_tolerance_counts():
    invs = [("A", 100.49), ("B", 200.51)]
    out = find_bundles(target_amount=301.0, invoices=invs, tolerance=0.01)
    assert len(out) == 1
    assert set(out[0].invoice_ids) == {"A", "B"}


def test_empty_inputs():
    assert find_bundles(target_amount=100.0, invoices=[]) == []
    assert find_bundles(target_amount=0.0, invoices=[("A", 100)]) == []
