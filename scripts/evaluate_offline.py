"""Reproduce the registered offline Gate C comparison on synthetic traces."""

from __future__ import annotations

import json
import platform
from pathlib import Path

import pandas as pd
import sklearn
from sklearn.metrics import average_precision_score, roc_auc_score

from agentsentinel.data.preprocess import preprocess
from agentsentinel.data.synth_traces import write_dataset
from agentsentinel.data.split import load_features, temporal_split
from agentsentinel.detectors.isoforest import predict_isolation_forest, train_isolation_forest
from agentsentinel.detectors.rules import apply_rules
from agentsentinel.features.contextual import build_features


def metrics(y_true: pd.Series, flagged: pd.Series, anomaly_score: pd.Series) -> dict:
    actual = y_true.astype(bool)
    predicted = flagged.astype(bool)
    tp = int((actual & predicted).sum())
    fp = int((~actual & predicted).sum())
    tn = int((~actual & ~predicted).sum())
    fn = int((actual & ~predicted).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "pr_auc": float(average_precision_score(actual, anomaly_score)),
        "auroc": float(roc_auc_score(actual, anomaly_score)),
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "fpr": fp / (fp + tn) if fp + tn else 0.0,
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
    }


def run() -> dict:
    source_path = Path("data/synthetic/v3")
    processed_path = Path("data/processed/v4")
    feature_path = Path("data/features/v4")
    write_dataset(source_path)
    preprocess(
        output_dir=processed_path,
        version="v4",
        input_paths={"synthetic": [source_path / "normal/traces.jsonl", source_path / "anomalous/traces.jsonl"]},
    )
    feature_manifest = build_features(
        output_dir=feature_path, version="v4", input_path=processed_path / "traces.parquet"
    )
    df = load_features()
    train, test, cutoff = temporal_split(df)
    truth = test["label"].eq("anomalous")
    if truth.nunique() != 2:
        raise ValueError("chronological test window must contain both normal and anomalous events")
    rules = apply_rules(test)
    model = train_isolation_forest(train)
    iso = predict_isolation_forest(model, test)
    report = {
        "dataset_version": sorted(df["dataset_version"].unique().tolist()),
        "python": platform.python_version(),
        "pandas": pd.__version__,
        "scikit_learn": sklearn.__version__,
        "cutoff_time": cutoff.isoformat(),
        "train_traces": int(train.trace_id.nunique()),
        "test_traces": int(test.trace_id.nunique()),
        "test_events": len(test),
        "test_anomalous_events": int(truth.sum()),
        "feature_manifest": str(feature_path / "manifest.json"),
        "baseline_fit": feature_manifest["baseline_fit"],
        "rules": metrics(truth, rules["rules_anomalous"], rules["rules_anomalous"].astype(int)),
        "isolation_forest": metrics(truth, iso["iso_anomaly"], iso["iso_anomaly_score"]),
    }
    report["gate_c_pass"] = (
        report["isolation_forest"]["pr_auc"] > report["rules"]["pr_auc"]
        and report["isolation_forest"]["pr_auc"] > 0.5
        and report["isolation_forest"]["fpr"] <= 0.10
    )
    return report


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
