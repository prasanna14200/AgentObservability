"""Phase 8 — temporal train/test split for anomaly detection.

Splits traces by timestamp (earliest -> train, latest -> test) to preserve
temporal ordering and avoid data leakage. Asserts no trace_id appears in
both splits.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from agentsentinel.features.contextual import FEATURE_MANIFEST

PRUNED_FEATURES = [f["name"] for f in FEATURE_MANIFEST if f["name"] not in {
    "agent_latency_zscore", "agent_retry_zscore", "agent_token_zscore", "agent_cost_zscore", "calls_so_far"
}]

INPUT_PATH = "data/features/v4/features.parquet"


def load_features() -> pd.DataFrame:
    df = pd.read_parquet(INPUT_PATH)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def temporal_split(df: pd.DataFrame, train_frac: float = 0.8) -> tuple[pd.DataFrame, pd.DataFrame]:
    trace_times = df.groupby("trace_id").agg({
        "timestamp": "min",
        "label": "first",
        "anomaly_type": "first",
    }).reset_index()

    trace_times = trace_times.sort_values("timestamp")
    cutoff_idx = int(len(trace_times) * train_frac)
    cutoff_time = trace_times.iloc[cutoff_idx]["timestamp"]

    train_trace_ids = set(trace_times.iloc[:cutoff_idx]["trace_id"])
    test_trace_ids = set(trace_times.iloc[cutoff_idx:]["trace_id"])

    assert train_trace_ids.isdisjoint(test_trace_ids), "Trace leakage detected between train and test splits"

    train = df[df["trace_id"].isin(train_trace_ids)].copy()
    test = df[df["trace_id"].isin(test_trace_ids)].copy()

    return train, test, cutoff_time


def get_split_info(df: pd.DataFrame, train_frac: float = 0.8) -> dict[str, Any]:
    train, test, cutoff_time = temporal_split(df, train_frac=train_frac)

    train_traces = train["trace_id"].nunique()
    test_traces = test["trace_id"].nunique()
    train_anomalous = train[train["label"] == "anomalous"]["trace_id"].nunique()
    test_anomalous = test[test["label"] == "anomalous"]["trace_id"].nunique()

    return {
        "cutoff_time": cutoff_time.isoformat(),
        "train_frac": train_frac,
        "train_traces": int(train_traces),
        "test_traces": int(test_traces),
        "train_events": int(len(train)),
        "test_events": int(len(test)),
        "train_anomalous_traces": int(train_anomalous),
        "test_anomalous_traces": int(test_anomalous),
        "train_normal_traces": int(train_traces - train_anomalous),
        "test_normal_traces": int(test_traces - test_anomalous),
        "features_used": PRUNED_FEATURES,
    }
