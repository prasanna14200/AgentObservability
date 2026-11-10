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


def temporal_split(
    df: pd.DataFrame, train_frac: float = 0.8
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
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


def temporal_train_validation_test_split(
    df: pd.DataFrame,
    *,
    train_frac: float = 0.6,
    validation_frac: float = 0.2,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.Timestamp,
    pd.Timestamp,
]:
    """Split whole traces chronologically without sharing trace IDs across sets."""
    if df.empty:
        raise ValueError("dataset must contain traces")
    if df["trace_id"].isna().any():
        raise ValueError("trace identifiers must not be null")
    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
        raise ValueError("timestamps must be parsed datetimes")
    if df["timestamp"].isna().any():
        raise ValueError("timestamps must not be null")
    if (
        not 0 < train_frac < 1
        or not 0 < validation_frac < 1
        or train_frac + validation_frac >= 1
    ):
        raise ValueError("train and validation fractions must leave nonempty chronological splits")
    if df["label"].isna().any():
        raise ValueError("trace labels must not be null")
    if not df["label"].isin({"normal", "anomalous"}).all():
        raise ValueError("labels must be normal or anomalous")
    trace_times = df.groupby("trace_id").agg(
        timestamp=("timestamp", "min"),
        label_count=("label", lambda values: values.nunique(dropna=False)),
        anomaly_type_count=("anomaly_type", lambda values: values.nunique(dropna=False)),
    ).reset_index()
    if (trace_times["label_count"] != 1).any() or (trace_times["anomaly_type_count"] != 1).any():
        raise ValueError("each trace must have one consistent label and anomaly type")
    trace_times = trace_times.sort_values(["timestamp", "trace_id"], kind="mergesort")
    train_end = int(len(trace_times) * train_frac)
    validation_end = int(len(trace_times) * (train_frac + validation_frac))
    if train_end < 1 or validation_end <= train_end or validation_end >= len(trace_times):
        raise ValueError("dataset is too small for the requested chronological split")
    train_cutoff = trace_times.iloc[train_end]["timestamp"]
    test_cutoff = trace_times.iloc[validation_end]["timestamp"]
    train_ids = set(trace_times.loc[trace_times["timestamp"] < train_cutoff, "trace_id"])
    validation_ids = set(
        trace_times.loc[
            (trace_times["timestamp"] >= train_cutoff)
            & (trace_times["timestamp"] < test_cutoff),
            "trace_id",
        ]
    )
    test_ids = set(trace_times.loc[trace_times["timestamp"] >= test_cutoff, "trace_id"])
    if train_ids & validation_ids or train_ids & test_ids or validation_ids & test_ids:
        raise ValueError("trace leakage detected between splits")
    if not train_ids or not validation_ids or not test_ids:
        raise ValueError("timestamp ties leave an empty chronological split")
    return (
        df[df["trace_id"].isin(train_ids)].copy(),
        df[df["trace_id"].isin(validation_ids)].copy(),
        df[df["trace_id"].isin(test_ids)].copy(),
        train_cutoff,
        test_cutoff,
    )


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
