"""XGBoost ranker with isotonic calibration.

Trained from the synthetic dataset. The pipeline is:

    raw features -> XGBClassifier.predict_proba -> raw score in [0,1]
                 -> IsotonicRegression -> calibrated probability

We deliberately use a small, fast model. With ~8 features and a few
thousand training rows the whole thing fits and trains in <1s, which
keeps the demo / test loop snappy.

The model is persisted as two files:

    cash_recon/artifacts/ranker.json  (XGBoost native format)
    cash_recon/artifacts/calibrator.npz  (knots for isotonic regression)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
from sklearn.isotonic import IsotonicRegression
from xgboost import XGBClassifier

from cash_recon.features import FEATURE_NAMES, InvoiceView, WireView, candidates_for_wire, featurize
from cash_recon.synth import SynthDataset, generate

DEFAULT_ARTIFACT_DIR = Path(__file__).parent / "artifacts"
RANKER_FILE = "ranker.json"
CALIBRATOR_FILE = "calibrator.joblib"


@dataclass
class TrainingExample:
    features: list[float]
    label: int  # 1 if (wire, inv) is a true positive, else 0


def _build_training_set(
    ds: SynthDataset, *, candidates_per_wire_cap: int = 30
) -> list[TrainingExample]:
    """Generate (wire, invoice) candidate pairs labeled by ground truth.

    Negatives come from candidate generation against truth_payer_id
    AND from a random sample of other-payer invoices. Without the
    cross-payer negatives the model learns only the easy "same
    payer, similar amount" axis.
    """
    inv_views = [
        InvoiceView(
            invoice_id=i.invoice_id,
            payer_id=i.payer_id,
            payer_name=i.payer_name,
            amount=i.amount,
            due_date=i.due_date,
        )
        for i in ds.invoices
    ]

    examples: list[TrainingExample] = []
    rng = np.random.default_rng(seed=7)

    for w in ds.wires:
        wire = WireView(
            wire_id=w.wire_id,
            amount=w.amount,
            received_on=w.received_on,
            memo=w.memo,
            sender_name=w.sender_name,
        )
        truth_set = set(w.truth_invoice_ids)

        # Same-payer candidates (positives + same-payer negatives).
        same_payer = (
            candidates_for_wire(wire, inv_views, payer_id=w.truth_payer_id)
            if w.truth_payer_id
            else []
        )
        # Cross-payer hard negatives: random invoices from other payers.
        others = rng.choice(
            len(inv_views), size=min(8, len(inv_views)), replace=False
        ).tolist()
        cross = [inv_views[i] for i in others if inv_views[i].payer_id != w.truth_payer_id]
        pool = (same_payer + cross)[:candidates_per_wire_cap]

        for inv in pool:
            label = 1 if inv.invoice_id in truth_set else 0
            examples.append(TrainingExample(features=featurize(wire, inv), label=label))

    return examples


@dataclass
class TrainedRanker:
    model: XGBClassifier
    calibrator: IsotonicRegression
    feature_names: list[str]
    metrics: dict[str, float]

    def predict_calibrated(self, features: list[float]) -> tuple[float, float]:
        """Return (raw_score, calibrated_prob) for one (wire, invoice)."""
        x = np.asarray([features])
        raw = float(self.model.predict_proba(x)[0, 1])
        cal = float(self.calibrator.predict([raw])[0])
        return raw, cal

    def save(self, artifact_dir: Path | None = None) -> Path:
        d = artifact_dir or DEFAULT_ARTIFACT_DIR
        d.mkdir(parents=True, exist_ok=True)
        self.model.save_model(str(d / RANKER_FILE))
        joblib.dump(self.calibrator, d / CALIBRATOR_FILE)
        (d / "metrics.json").write_text(json.dumps(self.metrics, indent=2))
        return d


def train(
    dataset: SynthDataset | None = None,
    *,
    seed: int = 42,
    n_invoices: int = 200,
    n_wires: int = 200,
    test_split: float = 0.25,
) -> TrainedRanker:
    """Train ranker + calibrator on a synthetic dataset.

    A larger synthetic dataset is used here than the demo's so the model
    learns clean decision boundaries; demos don't retrain.
    """
    ds = dataset or generate(seed=seed, n_invoices=n_invoices, n_wires=n_wires)
    examples = _build_training_set(ds)

    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(examples))
    split = int(len(examples) * (1 - test_split))
    train_idx, test_idx = idx[:split], idx[split:]

    X = np.asarray([examples[i].features for i in train_idx])
    y = np.asarray([examples[i].label for i in train_idx])
    Xt = np.asarray([examples[i].features for i in test_idx])
    yt = np.asarray([examples[i].label for i in test_idx])

    pos = max(1, int(y.sum()))
    neg = max(1, int(len(y) - pos))
    model = XGBClassifier(
        n_estimators=120,
        max_depth=4,
        learning_rate=0.15,
        scale_pos_weight=neg / pos,
        eval_metric="logloss",
        random_state=seed,
    )
    model.fit(X, y)

    raw_train = model.predict_proba(X)[:, 1]
    raw_test = model.predict_proba(Xt)[:, 1]

    cal = IsotonicRegression(out_of_bounds="clip")
    cal.fit(raw_train, y)

    cal_test = cal.predict(raw_test)
    auc = _auc(yt, cal_test)
    brier = float(np.mean((cal_test - yt) ** 2))

    return TrainedRanker(
        model=model,
        calibrator=cal,
        feature_names=list(FEATURE_NAMES),
        metrics={"auc": auc, "brier": brier, "n_train": int(len(y)), "n_test": int(len(yt))},
    )


def _auc(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """Area under ROC. Hand-rolled so we don't pull sklearn.metrics for one number."""
    order = np.argsort(-y_score)
    y_sorted = y_true[order]
    pos = float(y_sorted.sum())
    neg = float(len(y_sorted) - pos)
    if pos == 0 or neg == 0:
        return 0.5
    cum_pos = np.cumsum(y_sorted == 1)
    cum_neg = np.cumsum(y_sorted == 0)
    # AUC = sum_over_neg(rank_of_negs among positives) / (pos*neg)
    auc_num = float(np.sum(cum_pos[y_sorted == 0]))
    return auc_num / (pos * neg)


def load(artifact_dir: Path | None = None) -> TrainedRanker:
    d = artifact_dir or DEFAULT_ARTIFACT_DIR
    model = XGBClassifier()
    model.load_model(str(d / RANKER_FILE))
    cal: IsotonicRegression = joblib.load(d / CALIBRATOR_FILE)
    metrics = {}
    metrics_path = d / "metrics.json"
    if metrics_path.exists():
        metrics = json.loads(metrics_path.read_text())
    return TrainedRanker(
        model=model, calibrator=cal, feature_names=list(FEATURE_NAMES), metrics=metrics,
    )
