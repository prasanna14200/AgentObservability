"""Calibrate an advisory Isolation Forest threshold on validation traces only."""

from __future__ import annotations

import json
import math
import platform
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import sklearn
from sklearn.metrics import average_precision_score, roc_auc_score

from agentsentinel.data.preprocess import _coerce_types, load_raw_traces
from agentsentinel.data.split import temporal_train_validation_test_split
from agentsentinel.detectors.isoforest import FEATURES, train_isolation_forest
from agentsentinel.detectors.rules import apply_rules
from agentsentinel.features.contextual import (
    _add_permission_match,
    _add_trajectory_features,
    _build_tool_permissions_map,
    fit_baselines,
    transform_with_baselines,
)

MAX_VALIDATION_FPR = 0.10
MODEL_VERSION = "payment-advisory-iforest-task-calibrated-temporal-60-20-20-v3"


def select_fpr_bounded_threshold(
    labels: list[bool],
    scores: list[float],
    *,
    max_fpr: float = MAX_VALIDATION_FPR,
) -> float:
    """Choose the highest-recall score threshold meeting validation FPR."""
    if len(labels) != len(scores) or not labels:
        raise ValueError("labels and scores must be nonempty and have equal length")
    if any(type(label) is not bool for label in labels):
        raise ValueError("labels must be boolean")
    if any(
        not isinstance(score, (int, float))
        or isinstance(score, bool)
        or not math.isfinite(score)
        for score in scores
    ):
        raise ValueError("scores must be finite numbers")
    if (
        not isinstance(max_fpr, (int, float))
        or isinstance(max_fpr, bool)
        or not math.isfinite(max_fpr)
        or not 0 <= max_fpr <= 1
    ):
        raise ValueError("maximum false-positive rate must be between zero and one")
    if len(set(labels)) != 2:
        raise ValueError("validation data must contain normal and anomalous examples")

    candidates = sorted(set(scores))
    candidates.append(max(candidates) + max(1.0, abs(max(candidates))))
    best: tuple[float, float, float, float] | None = None
    for threshold in candidates:
        predictions = [score >= threshold for score in scores]
        tp = sum(actual and predicted for actual, predicted in zip(labels, predictions))
        fp = sum(not actual and predicted for actual, predicted in zip(labels, predictions))
        positives = sum(labels)
        negatives = len(labels) - positives
        recall = tp / positives
        fpr = fp / negatives
        precision = tp / (tp + fp) if tp + fp else 0.0
        if fpr <= max_fpr:
            candidate = (recall, precision, -fpr, threshold)
            if best is None or candidate > best:
                best = candidate
    if best is None:
        raise ValueError("no threshold satisfies the validation false-positive constraint")
    return best[3]


def select_group_fpr_bounded_thresholds(
    labels: list[bool],
    scores: list[float],
    groups: list[str],
    *,
    max_fpr: float = MAX_VALIDATION_FPR,
) -> dict[str, float]:
    """Calibrate per-group thresholds on validation data; unseen groups fail closed."""
    if len(labels) != len(scores) or len(labels) != len(groups) or not labels:
        raise ValueError("labels, scores, and groups must be nonempty and have equal length")
    if any(type(label) is not bool for label in labels):
        raise ValueError("labels must be boolean")
    if any(
        not isinstance(score, (int, float))
        or isinstance(score, bool)
        or not math.isfinite(score)
        for score in scores
    ):
        raise ValueError("scores must be finite numbers")
    if (
        not isinstance(max_fpr, (int, float))
        or isinstance(max_fpr, bool)
        or not math.isfinite(max_fpr)
        or not 0 <= max_fpr <= 1
    ):
        raise ValueError("maximum false-positive rate must be between zero and one")
    if any(not isinstance(group, str) or not group for group in groups):
        raise ValueError("groups must be nonempty strings")
    thresholds: dict[str, float] = {}
    for group in sorted(set(groups)):
        selected = [index for index, value in enumerate(groups) if value == group]
        group_labels = [labels[index] for index in selected]
        group_scores = [scores[index] for index in selected]
        if len(set(group_labels)) != 2:
            thresholds[group] = max(group_scores) + max(1.0, abs(max(group_scores)))
            continue
        thresholds[group] = select_fpr_bounded_threshold(
            group_labels, group_scores, max_fpr=max_fpr
        )
    return thresholds


def _group_predictions(
    groups: pd.Series,
    scores: np.ndarray,
    thresholds: dict[str, float],
    *,
    index: pd.Index,
) -> pd.Series:
    predictions = [
        score >= thresholds.get(str(group), math.inf)
        for group, score in zip(groups, scores)
    ]
    return pd.Series(predictions, index=index)


def _metrics(
    labels: pd.Series,
    predictions: pd.Series,
    scores: pd.Series,
    *,
    trace_ids: pd.Series,
) -> dict[str, Any]:
    actual = labels.astype(bool)
    predicted = predictions.astype(bool)
    tp = int((actual & predicted).sum())
    fp = int((~actual & predicted).sum())
    tn = int((~actual & ~predicted).sum())
    fn = int((actual & ~predicted).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    cluster_ci = _trace_cluster_intervals(labels, predicted, trace_ids)
    normal_events = fp + tn
    return {
        "events": int(len(labels)),
        "normal_events": int(normal_events),
        "anomalous_events": int(actual.sum()),
        "pr_auc": float(average_precision_score(actual, scores)),
        "roc_auc": float(roc_auc_score(actual, scores)),
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "fpr": fp / (fp + tn) if fp + tn else 0.0,
        "fnr": fn / (fn + tp) if fn + tp else 0.0,
        "fpr_cluster_bootstrap_95_ci": cluster_ci["fpr"],
        "fpr_zero_false_positive_exact_95_upper_bound": (
            1 - 0.05 ** (1 / normal_events) if fp == 0 and normal_events else None
        ),
        "recall_cluster_bootstrap_95_ci": cluster_ci["recall"],
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
    }


def _trace_cluster_intervals(
    labels: pd.Series, predictions: pd.Series, trace_ids: pd.Series
) -> dict[str, list[float] | None]:
    grouped = pd.DataFrame(
        {
            "trace_id": trace_ids.to_numpy(),
            "actual": labels.astype(bool).to_numpy(),
            "predicted": predictions.astype(bool).to_numpy(),
        }
    )
    grouped["normal_events"] = (~grouped["actual"]).astype(int)
    grouped["false_positives"] = (
        ~grouped["actual"] & grouped["predicted"]
    ).astype(int)
    grouped["anomalous_events"] = grouped["actual"].astype(int)
    grouped["true_positives"] = (
        grouped["actual"] & grouped["predicted"]
    ).astype(int)
    counts = grouped.groupby("trace_id", sort=True)[
        ["normal_events", "false_positives", "anomalous_events", "true_positives"]
    ].sum()
    rng = np.random.default_rng(42)
    samples = rng.integers(0, len(counts), size=(1000, len(counts)))
    sampled = counts.to_numpy()[samples].sum(axis=1)

    def interval(numerator: np.ndarray, denominator: np.ndarray) -> list[float] | None:
        valid = denominator > 0
        if not valid.any():
            return None
        values = numerator[valid] / denominator[valid]
        low, high = np.quantile(values, [0.025, 0.975])
        return [float(low), float(high)]

    return {
        "fpr": interval(sampled[:, 1], sampled[:, 0]),
        "recall": interval(sampled[:, 3], sampled[:, 2]),
    }


def _features_for_split(
    frame: pd.DataFrame,
    baselines: dict[str, pd.DataFrame],
    permissions: dict[str, list[str]],
) -> pd.DataFrame:
    return transform_with_baselines(
        _add_permission_match(_add_trajectory_features(frame), permissions),
        baselines,
    )


def run() -> dict[str, Any]:
    source = Path("data/synthetic/v3")
    raw = load_raw_traces(
        {
            "synthetic": [
                source / "normal" / "traces.jsonl",
                source / "anomalous" / "traces.jsonl",
            ]
        }
    )
    raw = _coerce_types(raw)
    train_raw, validation_raw, test_raw, train_cutoff, test_cutoff = (
        temporal_train_validation_test_split(raw)
    )
    normal_train = train_raw.loc[train_raw["label"] == "normal"]
    if normal_train.empty:
        raise ValueError("training split must contain normal traces")
    baselines = fit_baselines(normal_train)
    permissions = _build_tool_permissions_map(normal_train)
    train = _features_for_split(train_raw, baselines, permissions)
    validation = _features_for_split(validation_raw, baselines, permissions)
    test = _features_for_split(test_raw, baselines, permissions)

    model = train_isolation_forest(train)
    validation_scores = -model.decision_function(validation[FEATURES].values)
    threshold = select_fpr_bounded_threshold(
        validation["label"].eq("anomalous").tolist(),
        validation_scores.tolist(),
    )
    zero_fpr_validation_threshold = select_fpr_bounded_threshold(
        validation["label"].eq("anomalous").tolist(),
        validation_scores.tolist(),
        max_fpr=0.0,
    )
    test_scores = -model.decision_function(test[FEATURES].values)
    test_labels = test["label"].eq("anomalous")
    test_predictions = pd.Series(test_scores >= threshold, index=test.index)
    test_metrics = _metrics(
        test_labels,
        test_predictions,
        pd.Series(test_scores, index=test.index),
        trace_ids=test["trace_id"],
    )

    validation_labels = validation["label"].eq("anomalous")
    validation_predictions = pd.Series(validation_scores >= threshold, index=validation.index)
    validation_metrics = _metrics(
        validation_labels,
        validation_predictions,
        pd.Series(validation_scores, index=validation.index),
        trace_ids=validation["trace_id"],
    )
    zero_fpr_validation_predictions = pd.Series(
        validation_scores >= zero_fpr_validation_threshold,
        index=validation.index,
    )
    zero_fpr_validation_metrics = _metrics(
        validation_labels,
        zero_fpr_validation_predictions,
        pd.Series(validation_scores, index=validation.index),
        trace_ids=validation["trace_id"],
    )
    zero_fpr_test_predictions = pd.Series(
        test_scores >= zero_fpr_validation_threshold,
        index=test.index,
    )
    zero_fpr_test_metrics = _metrics(
        test_labels,
        zero_fpr_test_predictions,
        pd.Series(test_scores, index=test.index),
        trace_ids=test["trace_id"],
    )

    group_thresholds = select_group_fpr_bounded_thresholds(
        validation_labels.tolist(),
        validation_scores.tolist(),
        validation["task_type"].astype(str).tolist(),
    )
    group_validation_predictions = _group_predictions(
        validation["task_type"],
        validation_scores,
        group_thresholds,
        index=validation.index,
    )
    group_validation_metrics = _metrics(
        validation_labels,
        group_validation_predictions,
        pd.Series(validation_scores, index=validation.index),
        trace_ids=validation["trace_id"],
    )
    group_test_predictions = _group_predictions(
        test["task_type"],
        test_scores,
        group_thresholds,
        index=test.index,
    )
    group_test_metrics = _metrics(
        test_labels,
        group_test_predictions,
        pd.Series(test_scores, index=test.index),
        trace_ids=test["trace_id"],
    )

    rules = apply_rules(test)
    rules_scores = rules["rules_anomalous"].astype(float)
    rules_metrics = _metrics(
        test_labels, rules["rules_anomalous"], rules_scores, trace_ids=test["trace_id"]
    )
    per_type = _per_anomaly_type(test, test_labels, test_predictions)
    group_per_type = _per_anomaly_type(test, test_labels, group_test_predictions)

    source_metadata = json.loads((source / "metadata.json").read_text(encoding="utf-8"))
    trace_summary = raw.groupby("trace_id").agg(
        label=("label", "first"),
        anomaly_type=("anomaly_type", "first"),
        timestamp=("timestamp", "min"),
    )
    return {
        "model_version": MODEL_VERSION,
        "dataset_version": source_metadata["dataset_version"],
        "dataset_seed": source_metadata["seed"],
        "limitations": source_metadata["limitations"],
        "python": platform.python_version(),
        "pandas": pd.__version__,
        "scikit_learn": sklearn.__version__,
        "protocol": {
            "trace_split": "chronological 60% train / 20% validation / 20% test",
            "train_cutoff": train_cutoff.isoformat(),
            "test_cutoff": test_cutoff.isoformat(),
            "feature_baselines_fit": "normal-labeled training events only",
            "detector_fit": "normal-labeled training traces only",
            "threshold_selection": (
                "global and per-task thresholds maximize validation recall under "
                "validation-only FPR caps; test labels are not used for selection"
            ),
            "validation_threshold": threshold,
            "zero_validation_fpr_threshold": zero_fpr_validation_threshold,
            "task_group_validation_thresholds": group_thresholds,
            "task_group_calibration": (
                "per-task thresholds selected only on validation, each subject to "
                "a 0.10 empirical FPR cap; unseen task groups produce no alerts"
            ),
            "maximum_validation_fpr": MAX_VALIDATION_FPR,
            "trace_label_counts": {
                str(key): int(value)
                for key, value in trace_summary["label"].value_counts().items()
            },
            "trace_anomaly_type_counts": {
                str(key): int(value)
                for key, value in trace_summary.loc[
                    trace_summary["label"] == "anomalous", "anomaly_type"
                ].value_counts().items()
            },
            "trace_overlap": {
                "train_validation": len(set(train.trace_id) & set(validation.trace_id)),
                "train_test": len(set(train.trace_id) & set(test.trace_id)),
                "validation_test": len(set(validation.trace_id) & set(test.trace_id)),
            },
        },
        "validation_metrics": validation_metrics,
        "untouched_test_metrics": test_metrics,
        "zero_validation_fpr_validation_metrics": zero_fpr_validation_metrics,
        "zero_validation_fpr_untouched_test_metrics": zero_fpr_test_metrics,
        "task_group_validation_metrics": group_validation_metrics,
        "task_group_untouched_test_metrics": group_test_metrics,
        "task_group_untouched_test_per_anomaly_type": group_per_type,
        "task_group_test_false_positive_breakdown_by_task_type": _false_positive_breakdown(
            test, test_labels, group_test_predictions
        ),
        "untouched_test_per_anomaly_type": per_type,
        "test_false_positive_breakdown_by_task_type": _false_positive_breakdown(
            test, test_labels, test_predictions
        ),
        "rules_fixed_threshold_test_metrics": rules_metrics,
        "gate_c_target_fpr_le_10_percent": test_metrics["fpr"] <= MAX_VALIDATION_FPR,
        "task_group_gate_c_target_fpr_le_10_percent": (
            group_test_metrics["fpr"] <= MAX_VALIDATION_FPR
        ),
    }


def _per_anomaly_type(
    test: pd.DataFrame,
    labels: pd.Series,
    predictions: pd.Series,
) -> dict[str, dict[str, int | float]]:
    alerted = int(predictions.sum())
    per_type: dict[str, dict[str, int | float]] = {}
    anomalous_test = test.loc[labels]
    for anomaly_type in sorted(anomalous_test["anomaly_type"].unique()):
        mask = test["anomaly_type"].eq(anomaly_type)
        detected = int((mask & predictions).sum())
        support = int(mask.sum())
        per_type[str(anomaly_type)] = {
            "events": support,
            "detected": detected,
            "false_negatives": support - detected,
            "precision_one_vs_rest": detected / alerted if alerted else 0.0,
            "recall": detected / support if support else 0.0,
        }
    return per_type


def _false_positive_breakdown(
    test: pd.DataFrame, labels: pd.Series, predictions: pd.Series
) -> dict[str, dict[str, int | float]]:
    normal = test.loc[~labels].copy()
    normal["false_positive"] = predictions.loc[normal.index].to_numpy()
    breakdown: dict[str, dict[str, int | float]] = {}
    for task_type, group in normal.groupby("task_type", sort=True):
        false_positives = int(group["false_positive"].sum())
        event_count = len(group)
        breakdown[str(task_type)] = {
            "normal_events": int(event_count),
            "false_positives": false_positives,
            "fpr": false_positives / event_count if event_count else 0.0,
        }
    return breakdown


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
