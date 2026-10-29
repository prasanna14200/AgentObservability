"""Evaluate advisory detectors on deterministic synthetic payment traces only."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from sklearn.metrics import average_precision_score

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentsentinel.data.payment_synth_traces import generate_payment_traces
from agentsentinel.detectors.payment import PaymentDetectorAdapter


def _report(labels: list[bool], predictions: list[bool], scores: list[float], runtime_ms: float) -> dict:
    tp = sum(actual and predicted for actual, predicted in zip(labels, predictions))
    fp = sum(not actual and predicted for actual, predicted in zip(labels, predictions))
    tn = sum(not actual and not predicted for actual, predicted in zip(labels, predictions))
    fn = sum(actual and not predicted for actual, predicted in zip(labels, predictions))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "pr_auc_average_precision": float(average_precision_score(labels, scores)),
        "false_positive_rate": fp / (fp + tn) if fp + tn else 0.0,
        "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "runtime_ms_total": runtime_ms,
        "runtime_ms_per_trace": runtime_ms / len(labels),
    }


def evaluate() -> dict:
    traces = generate_payment_traces()
    normal = [trace for trace in traces if trace.label == "normal"]
    anomalous = [trace for trace in traces if trace.label == "anomalous"]
    training = normal[:70]
    testing = normal[70:] + anomalous
    adapter = PaymentDetectorAdapter()
    model = adapter.fit_isolation_forest(training)

    labels = [trace.label == "anomalous" for trace in testing]
    started = time.perf_counter()
    rules = [adapter.rules(trace.events) for trace in testing]
    rules_runtime = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    forest = [adapter.isolation_forest(trace.events, model) for trace in testing]
    forest_runtime = (time.perf_counter() - started) * 1000

    return {
        "dataset": "payment-domain-synthetic-v1",
        "seed": 20261028,
        "total_traces": len(traces),
        "normal_traces_total": len(normal),
        "anomalous_traces_total": len(anomalous),
        "training_normal_traces": len(training),
        "evaluation_normal_traces": len(normal) - len(training),
        "evaluation_anomalous_traces": len(anomalous),
        "evaluation_scenario_counts": {
            scenario: sum(trace.scenario == scenario for trace in anomalous)
            for scenario in sorted({trace.scenario for trace in anomalous})
        },
        "evaluation_traces": len(testing),
        "rules": _report(
            labels,
            [result.anomaly for result in rules],
            [result.anomaly_score for result in rules],
            rules_runtime,
        ),
        "isolation_forest": _report(
            labels,
            [result.anomaly for result in forest],
            [result.anomaly_score for result in forest],
            forest_runtime,
        ),
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), indent=2, sort_keys=True))
