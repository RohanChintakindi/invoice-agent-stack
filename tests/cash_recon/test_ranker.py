"""Ranker training metrics + persistence roundtrip."""

from __future__ import annotations

from cash_recon.ranker import load, train


def test_train_reaches_high_auc_on_synth():
    r = train(seed=42, n_invoices=200, n_wires=200)
    # Synth labels are clean — model should easily exceed 0.9 AUC.
    assert r.metrics["auc"] >= 0.9
    assert r.metrics["brier"] < 0.05


def test_save_and_load_roundtrip(tmp_path):
    r = train(seed=42, n_invoices=120, n_wires=120)
    out = r.save(tmp_path)
    assert (out / "ranker.json").exists()
    assert (out / "calibrator.joblib").exists()
    assert (out / "metrics.json").exists()
    loaded = load(tmp_path)
    # Same input -> same output (within float tolerance).
    test_features = [100.0, 0.05, 0.0, 5.0, 5.0, 0.6, 0.7, 0.8]
    raw1, cal1 = r.predict_calibrated(test_features)
    raw2, cal2 = loaded.predict_calibrated(test_features)
    assert abs(raw1 - raw2) < 1e-6
    assert abs(cal1 - cal2) < 1e-6


def test_predict_calibrated_within_unit_interval():
    r = train(seed=42, n_invoices=80, n_wires=80)
    feats = [50.0, 0.05, 1.0, 0.0, 0.0, 1.0, 1.0, 1.0]
    raw, cal = r.predict_calibrated(feats)
    assert 0.0 <= raw <= 1.0
    assert 0.0 <= cal <= 1.0
