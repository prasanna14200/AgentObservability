from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from agentsentinel.data.preprocess import (
    NULL_LABEL_SENTINEL,
    OUTPUT_DIR,
    VERSION,
    INPUT_PATHS,
    _coerce_types,
    _deduplicate,
    _flatten_trace,
    load_raw_traces,
    preprocess,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def test_load_raw_traces_preserves_synthetic_and_collected_provenance(tmp_path: Path) -> None:
    normal = {
        "trace_id": "trace-synth-001",
        "dataset_version": "v1",
        "data_type": "synthetic",
        "limitations": "synth",
        "label": "normal",
        "agent_id": "agent-1",
        "task_id": "task-1",
        "task_type": "order_lookup",
        "events": [
            {
                "event_id": "evt-1",
                "trace_id": "trace-synth-001",
                "agent_id": "agent-1",
                "task_id": "task-1",
                "task_type": "order_lookup",
                "timestamp": "2026-01-01T09:00:00+00:00",
                "event_type": "tool_call",
                "tool_name": "search_customer",
                "tool_input_metadata": {"customer_ref_hash": "cust-1"},
                "tool_output_metadata": {"record_count": 1, "result_size_bytes": 500},
                "latency_ms": 120,
                "status": "success",
                "retry_count": 0,
                "token_usage": 100,
                "estimated_cost_usd": 0.0002,
                "permissions": ["orders:read"],
            }
        ],
    }
    _write_jsonl(tmp_path / "normal" / "traces.jsonl", [normal])

    original = load_raw_traces.__wrapped__ if hasattr(load_raw_traces, "__wrapped__") else None

    import agentsentinel.data.preprocess as preprocess_module

    original_paths = preprocess_module.INPUT_PATHS.copy()
    try:
        preprocess_module.INPUT_PATHS = {
            "synthetic": [tmp_path / "normal" / "traces.jsonl"],
            "collected": [tmp_path / "rejected" / "rejections.jsonl"],
        }
        df = load_raw_traces()
        assert "source" in df.columns
        assert set(df["source"].unique()).issubset({"synthetic", "collected"})
        assert len(df) >= 1
    finally:
        preprocess_module.INPUT_PATHS = original_paths


def test_flattening_redacts_secret_shaped_tool_arguments() -> None:
    trace = {
        "trace_id": "trace-1",
        "events": [{
            "event_id": "event-1",
            "tool_input_metadata": {"Authorization": "Bearer RAW-SECRET", "query_kind": "lookup"},
        }],
    }
    row = _flatten_trace(trace, "collected")[0]
    assert "RAW-SECRET" not in row["tool_input_metadata"]
    assert json.loads(row["tool_input_metadata"])["query_kind"] == "lookup"


def test_missing_label_and_anomaly_type_filled_with_sentinel() -> None:
    trace = {
        "trace_id": "trace-null-001",
        "dataset_version": "v1",
        "data_type": "real",
        "limitations": None,
        "label": None,
        "anomaly_type": None,
        "agent_id": "agent-1",
        "task_id": "task-1",
        "task_type": "order_lookup",
        "events": [
            {
                "event_id": "evt-1",
                "trace_id": "trace-null-001",
                "agent_id": "agent-1",
                "task_id": "task-1",
                "task_type": "order_lookup",
                "timestamp": "2026-01-01T09:00:00+00:00",
                "event_type": "tool_call",
                "tool_name": "search_customer",
                "tool_input_metadata": {},
                "tool_output_metadata": {"record_count": 1, "result_size_bytes": 500},
                "latency_ms": 100,
                "status": "success",
                "retry_count": 0,
                "token_usage": 50,
                "estimated_cost_usd": 0.0001,
                "permissions": ["orders:read"],
            }
        ],
    }
    rows = _flatten_trace(trace, "collected")
    assert len(rows) == 1
    row = rows[0]
    assert row["label"] == NULL_LABEL_SENTINEL
    assert row["anomaly_type"] == NULL_LABEL_SENTINEL


def test_dedup_removes_duplicate_trace_event_rows() -> None:
    rows = [
        {"source": "synthetic", "trace_id": "t1", "event_id": "e1", "latency_ms": 100},
        {"source": "synthetic", "trace_id": "t1", "event_id": "e1", "latency_ms": 100},
        {"source": "synthetic", "trace_id": "t1", "event_id": "e2", "latency_ms": 200},
    ]
    df = pd.DataFrame(rows)
    deduped, dropped = _deduplicate(df)
    assert len(deduped) == 2
    assert dropped == 1
    assert set(deduped["event_id"]) == {"e1", "e2"}


def test_type_coercion_converts_numeric_fields() -> None:
    df = pd.DataFrame([
        {
            "source": "synthetic",
            "trace_id": "t1",
            "event_id": "e1",
            "latency_ms": "120",
            "retry_count": "2",
            "token_usage": "300",
            "estimated_cost_usd": "0.0006",
            "record_count": "5",
            "result_size_bytes": "1000",
            "timestamp": "2026-01-01T09:00:00+00:00",
        }
    ])
    result = _coerce_types(df)
    assert pd.api.types.is_integer_dtype(result["latency_ms"])
    assert pd.api.types.is_integer_dtype(result["retry_count"])
    assert pd.api.types.is_integer_dtype(result["token_usage"])
    assert pd.api.types.is_float_dtype(result["estimated_cost_usd"])
    assert result["latency_ms"].iloc[0] == 120
    assert result["estimated_cost_usd"].iloc[0] == 0.0006


def test_preprocess_writes_versioned_parquet_and_manifest(tmp_path: Path) -> None:
    import agentsentinel.data.preprocess as preprocess_module

    original_output = preprocess_module.OUTPUT_DIR
    original_paths = preprocess_module.INPUT_PATHS.copy()
    try:
        output = tmp_path / "processed" / VERSION
        preprocess_module.OUTPUT_DIR = output
        preprocess_module.INPUT_PATHS = {
            "synthetic": [],
            "collected": [],
        }
        manifest = preprocess(output_dir=output, version=VERSION)
        assert (output / "traces.parquet").exists()
        assert (output / "manifest.json").exists()
        assert manifest["version"] == VERSION
        assert "input_sources" in manifest
        assert "row_counts" in manifest
        assert "null_handling" in manifest
        assert manifest["null_handling"]["label"] == f"filled with sentinel '{NULL_LABEL_SENTINEL}'"
    finally:
        preprocess_module.OUTPUT_DIR = original_output
        preprocess_module.INPUT_PATHS = original_paths


def test_rejection_log_payloads_are_excluded_from_processed_dataset(tmp_path: Path) -> None:
    """Rejected records must not enter the processed DataFrame. The rejection
    log is an audit trail only; schema-invalid input should not pollute feature
    baselines or the modeling dataset."""

    normal_trace = {
        "trace_id": "trace-normal-001",
        "dataset_version": "v1",
        "data_type": "synthetic",
        "limitations": "synth",
        "label": "normal",
        "agent_id": "agent-1",
        "task_id": "task-1",
        "task_type": "order_lookup",
        "events": [
            {
                "event_id": "evt-1",
                "trace_id": "trace-normal-001",
                "agent_id": "agent-1",
                "task_id": "task-1",
                "task_type": "order_lookup",
                "timestamp": "2026-01-01T09:00:00+00:00",
                "event_type": "tool_call",
                "tool_name": "search_customer",
                "tool_input_metadata": {},
                "tool_output_metadata": {"record_count": 1, "result_size_bytes": 500},
                "latency_ms": 100,
                "status": "success",
                "retry_count": 0,
                "token_usage": 50,
                "estimated_cost_usd": 0.0001,
                "permissions": ["orders:read"],
            }
        ],
    }
    normal_path = tmp_path / "normal" / "traces.jsonl"
    _write_jsonl(normal_path, [normal_trace])

    rejection = {
        "rejected_at": "2026-01-01T09:00:00+00:00",
        "reason": "validation error",
        "trace_id": "trace-rej-001",
        "sanitized_event": {
            "trace_id": "trace-rej-001",
            "dataset_version": "v1",
            "data_type": "real",
            "label": "normal",
            "agent_id": "agent-1",
            "task_id": "task-1",
            "task_type": "order_lookup",
            "events": [
                {
                    "event_id": "evt-1",
                    "trace_id": "trace-rej-001",
                    "agent_id": "agent-1",
                    "task_id": "task-1",
                    "task_type": "order_lookup",
                    "timestamp": "2026-01-01T09:00:00+00:00",
                    "event_type": "tool_call",
                    "tool_name": "search_customer",
                    "tool_input_metadata": {},
                    "tool_output_metadata": {"record_count": 0, "result_size_bytes": 0},
                    "latency_ms": 50,
                    "status": "error",
                    "retry_count": 1,
                    "token_usage": 10,
                    "estimated_cost_usd": 0.00002,
                    "permissions": [],
                }
            ],
        },
    }
    rej_path = tmp_path / "rejected" / "rejections.jsonl"
    _write_jsonl(rej_path, [rejection])

    import agentsentinel.data.preprocess as preprocess_module

    original_paths = preprocess_module.INPUT_PATHS.copy()
    try:
        preprocess_module.INPUT_PATHS = {
            "synthetic": [normal_path],
            "collected": [rej_path],
        }
        df = load_raw_traces()
        assert len(df) == 1
        assert df.iloc[0]["trace_id"] == "trace-normal-001"
        assert "trace-rej-001" not in df["trace_id"].values
    finally:
        preprocess_module.INPUT_PATHS = original_paths
