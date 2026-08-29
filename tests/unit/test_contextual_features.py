from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from agentsentinel.features.contextual import (
    FEATURE_MANIFEST,
    METRIC_COLUMNS,
    _add_baseline_features,
    _add_permission_match,
    _add_trajectory_features,
    _build_tool_permissions_map,
    _safe_zscore,
    build_features,
)


def _make_trace_df(traces: list[dict]) -> pd.DataFrame:
    rows = []
    for trace in traces:
        for event in trace["events"]:
            rows.append({
                "trace_id": trace["trace_id"],
                "agent_id": trace["agent_id"],
                "task_type": trace["task_type"],
                "event_id": event["event_id"],
                "timestamp": event["timestamp"],
                "tool_name": event["tool_name"],
                "latency_ms": event["latency_ms"],
                "retry_count": event.get("retry_count", 0),
                "token_usage": event.get("token_usage", 0),
                "estimated_cost_usd": event.get("estimated_cost_usd", 0.0),
                "permissions": json.dumps(event.get("permissions", []), sort_keys=True),
            })
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def test_same_raw_latency_produces_different_zscore_under_different_agent_context() -> None:
    traces = [
        {
            "trace_id": "trace-a",
            "agent_id": "agent-fast",
            "task_type": "order_lookup",
            "events": [
                {"event_id": "a-e1", "timestamp": "2026-01-01T09:00:00+00:00", "tool_name": "search_customer", "latency_ms": 100, "permissions": ["orders:read"]},
                {"event_id": "a-e2", "timestamp": "2026-01-01T09:00:04+00:00", "tool_name": "get_order", "latency_ms": 110, "permissions": ["orders:read"]},
                {"event_id": "a-e3", "timestamp": "2026-01-01T09:00:08+00:00", "tool_name": "get_order", "latency_ms": 120, "permissions": ["orders:read"]},
                {"event_id": "a-e4", "timestamp": "2026-01-01T09:00:12+00:00", "tool_name": "update_order", "latency_ms": 130, "permissions": ["orders:read", "orders:write"]},
                {"event_id": "a-e5", "timestamp": "2026-01-01T09:00:16+00:00", "tool_name": "update_order", "latency_ms": 140, "permissions": ["orders:read", "orders:write"]},
                {"event_id": "a-e6", "timestamp": "2026-01-01T09:00:20+00:00", "tool_name": "update_order", "latency_ms": 150, "permissions": ["orders:read", "orders:write"]},
            ],
        },
        {
            "trace_id": "trace-b",
            "agent_id": "agent-slow",
            "task_type": "order_lookup",
            "events": [
                {"event_id": "b-e1", "timestamp": "2026-01-01T09:00:00+00:00", "tool_name": "search_customer", "latency_ms": 200, "permissions": ["orders:read"]},
                {"event_id": "b-e2", "timestamp": "2026-01-01T09:00:04+00:00", "tool_name": "get_order", "latency_ms": 210, "permissions": ["orders:read"]},
                {"event_id": "b-e3", "timestamp": "2026-01-01T09:00:08+00:00", "tool_name": "get_order", "latency_ms": 220, "permissions": ["orders:read"]},
                {"event_id": "b-e4", "timestamp": "2026-01-01T09:00:12+00:00", "tool_name": "update_order", "latency_ms": 230, "permissions": ["orders:read", "orders:write"]},
                {"event_id": "b-e5", "timestamp": "2026-01-01T09:00:16+00:00", "tool_name": "update_order", "latency_ms": 240, "permissions": ["orders:read", "orders:write"]},
                {"event_id": "b-e6", "timestamp": "2026-01-01T09:00:20+00:00", "tool_name": "update_order", "latency_ms": 150, "permissions": ["orders:read", "orders:write"]},
            ],
        },
    ]

    df = _make_trace_df(traces)
    df = _add_baseline_features(df)

    a_150 = df[(df["trace_id"] == "trace-a") & (df["latency_ms"] == 150)]
    b_150 = df[(df["trace_id"] == "trace-b") & (df["latency_ms"] == 150)]

    assert len(a_150) == 1
    assert len(b_150) == 1

    assert a_150["agent_latency_zscore"].iloc[0] != b_150["agent_latency_zscore"].iloc[0]
    assert a_150["task_latency_zscore"].iloc[0] == b_150["task_latency_zscore"].iloc[0]
    assert a_150["agent_task_latency_zscore"].iloc[0] != b_150["agent_task_latency_zscore"].iloc[0]


def test_same_raw_latency_produces_different_zscore_under_different_task_type_context() -> None:
    traces = [
        {
            "trace_id": "trace-short",
            "agent_id": "agent-1",
            "task_type": "short_task",
            "events": [
                {"event_id": "s-e1", "timestamp": "2026-01-01T09:00:00+00:00", "tool_name": "search_customer", "latency_ms": 10, "permissions": ["orders:read"]},
                {"event_id": "s-e2", "timestamp": "2026-01-01T09:00:04+00:00", "tool_name": "get_order", "latency_ms": 20, "permissions": ["orders:read"]},
                {"event_id": "s-e3", "timestamp": "2026-01-01T09:00:08+00:00", "tool_name": "update_order", "latency_ms": 100, "permissions": ["orders:read", "orders:write"]},
            ],
        },
        {
            "trace_id": "trace-long",
            "agent_id": "agent-1",
            "task_type": "long_task",
            "events": [
                {"event_id": "l-e1", "timestamp": "2026-01-01T09:00:00+00:00", "tool_name": "search_customer", "latency_ms": 200, "permissions": ["orders:read"]},
                {"event_id": "l-e2", "timestamp": "2026-01-01T09:00:04+00:00", "tool_name": "get_order", "latency_ms": 300, "permissions": ["orders:read"]},
                {"event_id": "l-e3", "timestamp": "2026-01-01T09:00:08+00:00", "tool_name": "update_order", "latency_ms": 400, "permissions": ["orders:read", "orders:write"]},
            ],
        },
        {
            "trace_id": "trace-mid",
            "agent_id": "agent-1",
            "task_type": "mid_task",
            "events": [
                {"event_id": "m-e1", "timestamp": "2026-01-01T09:00:00+00:00", "tool_name": "search_customer", "latency_ms": 50, "permissions": ["orders:read"]},
                {"event_id": "m-e2", "timestamp": "2026-01-01T09:00:04+00:00", "tool_name": "get_order", "latency_ms": 100, "permissions": ["orders:read"]},
                {"event_id": "m-e3", "timestamp": "2026-01-01T09:00:08+00:00", "tool_name": "update_order", "latency_ms": 200, "permissions": ["orders:read", "orders:write"]},
            ],
        },
    ]

    df = _make_trace_df(traces)
    df = _add_baseline_features(df)

    short_10 = df[(df["trace_id"] == "trace-short") & (df["latency_ms"] == 10)]
    long_200 = df[(df["trace_id"] == "trace-long") & (df["latency_ms"] == 200)]
    mid_50 = df[(df["trace_id"] == "trace-mid") & (df["latency_ms"] == 50)]

    assert len(short_10) == 1
    assert len(long_200) == 1
    assert len(mid_50) == 1

    assert short_10["task_latency_zscore"].iloc[0] != long_200["task_latency_zscore"].iloc[0]
    assert short_10["task_latency_zscore"].iloc[0] != mid_50["task_latency_zscore"].iloc[0]
    assert long_200["task_latency_zscore"].iloc[0] != mid_50["task_latency_zscore"].iloc[0]


def test_permission_match_is_deterministic_boolean() -> None:
    traces = [
        {
            "trace_id": "trace-1",
            "agent_id": "agent-1",
            "task_type": "order_lookup",
            "events": [
                {"event_id": "e1", "timestamp": "2026-01-01T09:00:00+00:00", "tool_name": "update_order", "latency_ms": 100, "permissions": ["orders:read"]},
            ],
        },
        {
            "trace_id": "trace-2",
            "agent_id": "agent-2",
            "task_type": "order_lookup",
            "events": [
                {"event_id": "e2", "timestamp": "2026-01-01T09:00:00+00:00", "tool_name": "update_order", "latency_ms": 100, "permissions": ["orders:read", "orders:write"]},
            ],
        },
    ]

    df = _make_trace_df(traces)
    tool_perms_map = _build_tool_permissions_map(df)
    df = _add_permission_match(df, tool_perms_map)

    assert df["permission_match"].dtype == bool
    assert df.loc[df["trace_id"] == "trace-1", "permission_match"].iloc[0] == False
    assert df.loc[df["trace_id"] == "trace-2", "permission_match"].iloc[0] == True


def test_trajectory_features_are_correct() -> None:
    traces = [
        {
            "trace_id": "trace-loop",
            "agent_id": "agent-1",
            "task_type": "order_lookup",
            "events": [
                {"event_id": "e1", "timestamp": "2026-01-01T09:00:00+00:00", "tool_name": "search_customer", "latency_ms": 100, "permissions": ["orders:read"]},
                {"event_id": "e2", "timestamp": "2026-01-01T09:00:05+00:00", "tool_name": "search_customer", "latency_ms": 100, "permissions": ["orders:read"]},
                {"event_id": "e3", "timestamp": "2026-01-01T09:00:10+00:00", "tool_name": "get_order", "latency_ms": 100, "permissions": ["orders:read"]},
            ],
        },
    ]

    df = _make_trace_df(traces)
    df = _add_trajectory_features(df)

    assert list(df["call_position"]) == [1, 2, 3]
    assert list(df["calls_so_far"]) == [1, 2, 3]
    assert list(df["time_since_last_call_ms"].iloc[1:]) == [5000.0, 5000.0]
    assert pd.isna(df["time_since_last_call_ms"].iloc[0])
    assert list(df["tool_repeat_in_session"]) == [0, 1, 0]
    assert list(df["trace_event_count"]) == [3, 3, 3]


def test_feature_manifest_contains_all_required_fields() -> None:
    required_keys = {"name", "derivation", "data_type", "baseline_scope", "contextual_rationale"}
    feature_names = {f["name"] for f in FEATURE_MANIFEST}

    assert len(FEATURE_MANIFEST) == 18
    for feature in FEATURE_MANIFEST:
        assert required_keys.issubset(feature.keys()), f"Missing keys in {feature['name']}"

    expected_features = {
        "agent_latency_zscore", "agent_retry_zscore", "agent_token_zscore", "agent_cost_zscore",
        "task_latency_zscore", "task_retry_zscore", "task_token_zscore", "task_cost_zscore",
        "agent_task_latency_zscore", "agent_task_retry_zscore", "agent_task_token_zscore", "agent_task_cost_zscore",
        "call_position", "calls_so_far", "time_since_last_call_ms", "tool_repeat_in_session",
        "permission_match", "trace_event_count",
    }
    assert feature_names == expected_features


def test_build_features_writes_parquet_and_manifest(tmp_path: Path) -> None:
    import agentsentinel.features.contextual as ctx_module

    original_output = ctx_module.OUTPUT_DIR
    try:
        output = tmp_path / "features" / "v1"
        ctx_module.OUTPUT_DIR = output
        manifest = build_features(output_dir=output)
        assert (output / "features.parquet").exists()
        assert (output / "manifest.json").exists()
        assert manifest["version"] == "v1"
        assert manifest["row_count"] == 1823
        assert len(manifest["features"]) == 18
    finally:
        ctx_module.OUTPUT_DIR = original_output


def test_no_model_training_or_rules_in_contextual_module() -> None:
    with open("src/agentsentinel/features/contextual.py", "r", encoding="utf-8") as f:
        source = f.read()

    banned = ["IsolationForest", "RandomForest", "XGBoost", "train_test_split", "fit(", "predict(", "rule_engine"]
    for token in banned:
        assert token not in source, f"Phase 6 must not contain model/rules logic: {token}"
