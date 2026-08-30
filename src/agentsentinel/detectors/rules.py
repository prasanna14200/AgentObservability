"""Phase 8 — deterministic rules engine for anomaly detection.

Applies domain-knowledge thresholds to flag specific anomaly types.
No training required.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

ZSCORE_FEATURES = {
    "retry": "task_retry_zscore",
    "latency": "task_latency_zscore",
    "token": "task_token_zscore",
    "cost": "task_cost_zscore",
}

LOOP_REPEAT_THRESHOLD = 3
RETRY_ZSCORE_THRESHOLD = 2.0
RESOURCE_ZSCORE_THRESHOLD = 2.5


def _flag_loop(df: pd.DataFrame) -> pd.Series:
    return df["tool_repeat_in_session"] >= LOOP_REPEAT_THRESHOLD


def _flag_retry_storm(df: pd.DataFrame) -> pd.Series:
    return df[ZSCORE_FEATURES["retry"]] >= RETRY_ZSCORE_THRESHOLD


def _flag_resource_spike(df: pd.DataFrame) -> pd.Series:
    token_spike = df[ZSCORE_FEATURES["token"]] >= RESOURCE_ZSCORE_THRESHOLD
    cost_spike = df[ZSCORE_FEATURES["cost"]] >= RESOURCE_ZSCORE_THRESHOLD
    return token_spike | cost_spike


def apply_rules(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rules_loop"] = _flag_loop(df)
    df["rules_retry_storm"] = _flag_retry_storm(df)
    df["rules_resource_spike"] = _flag_resource_spike(df)
    df["rules_anomalous"] = df["rules_loop"] | df["rules_retry_storm"] | df["rules_resource_spike"]
    return df


def get_rules_metadata() -> dict[str, Any]:
    return {
        "loop_threshold": LOOP_REPEAT_THRESHOLD,
        "loop_justification": "3+ repetitions of the same tool within a session strongly suggests a loop rather than legitimate repeated calls.",
        "retry_zscore_threshold": RETRY_ZSCORE_THRESHOLD,
        "retry_justification": "A z-score >= 2.0 means retry count is 2 standard deviations above the task-type mean, which is a strong signal of a retry storm.",
        "resource_zscore_threshold": RESOURCE_ZSCORE_THRESHOLD,
        "resource_justification": "A z-score >= 2.5 for token usage or cost indicates a resource spike relative to the task-type baseline. Slightly higher than retry threshold because resource variance is naturally larger.",
    }
