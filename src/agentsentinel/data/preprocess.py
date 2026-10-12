"""Phase 5 — preprocessing of validated trace records.

Converts Trace-validated JSONL (Phase 2 synthetic and Phase 4 collected) into
a clean, analysis-ready tabular form. Does NOT perform feature engineering.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from agentsentinel.schema.redaction import redact_mapping

NULL_LABEL_SENTINEL = "__UNKNOWN__"
VERSION = "v1"
OUTPUT_DIR = Path("data/processed") / VERSION

REQUIRED_SOURCE_DIRS = {
    "synthetic": Path("data/synthetic"),
    "collected": Path("data/collected"),
}

INPUT_PATHS = {
    "synthetic": [
        Path("data/synthetic/normal/traces.jsonl"),
        Path("data/synthetic/anomalous/traces.jsonl"),
    ],
    "collected": [
        Path("data/collected/normal/traces.jsonl"),
        Path("data/collected/anomalous/traces.jsonl"),
    ],
}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _flatten_trace(trace: dict[str, Any], source: str) -> list[dict[str, Any]]:
    events = trace.get("events")
    if not isinstance(events, list) or not events:
        return []

    rows: list[dict[str, Any]] = []
    for event in events:
        tool_output = event.get("tool_output_metadata", {})
        tool_input = event.get("tool_input_metadata", {})
        if not isinstance(tool_input, dict):
            tool_input = {}
        safe_tool_input, _ = redact_mapping(tool_input)
        rows.append({
            "source": source,
            "trace_id": trace.get("trace_id"),
            "dataset_version": trace.get("dataset_version"),
            "data_type": trace.get("data_type", source),
            "limitations": trace.get("limitations"),
            "label": trace.get("label") or NULL_LABEL_SENTINEL,
            "anomaly_type": trace.get("anomaly_type") or NULL_LABEL_SENTINEL,
            "agent_id": event.get("agent_id"),
            "task_id": event.get("task_id"),
            "task_type": event.get("task_type"),
            "event_id": event.get("event_id"),
            "timestamp": event.get("timestamp"),
            "event_type": event.get("event_type"),
            "tool_name": event.get("tool_name"),
            "tool_input_metadata": json.dumps(safe_tool_input, sort_keys=True),
            "tool_output_metadata": json.dumps(tool_output, sort_keys=True),
            "latency_ms": event.get("latency_ms"),
            "status": event.get("status"),
            "retry_count": event.get("retry_count"),
            "token_usage": event.get("token_usage"),
            "estimated_cost_usd": event.get("estimated_cost_usd"),
            "permissions": json.dumps(event.get("permissions", []), sort_keys=True),
            "record_count": tool_output.get("record_count"),
            "result_size_bytes": tool_output.get("result_size_bytes"),
        })
    return rows


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="mixed")

    numeric_cols = ["latency_ms", "retry_count", "token_usage", "estimated_cost_usd", "record_count", "result_size_bytes"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def _deduplicate(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    before = len(df)
    df = df.drop_duplicates(subset=["source", "trace_id", "event_id"], keep="first")
    after = len(df)
    dropped = before - after
    return df, dropped


def load_raw_traces(input_paths: dict[str, list[Path]] | None = None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    if input_paths is None:
        input_paths = INPUT_PATHS
    for source, paths in input_paths.items():
        for path in paths:
            for trace in _load_jsonl(path):
                rows.extend(_flatten_trace(trace, source))

    df = pd.DataFrame(rows)
    return df


def preprocess(
    output_dir: Path = OUTPUT_DIR,
    version: str = VERSION,
    input_paths: dict[str, list[Path]] | None = None,
) -> dict[str, Any]:
    if input_paths is None:
        input_paths = INPUT_PATHS
    df = load_raw_traces(input_paths)
    before_dedup = len(df)

    df = _coerce_types(df)
    df, dropped = _deduplicate(df)

    output_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = output_dir / "traces.parquet"
    df.to_parquet(parquet_path, index=False, engine="pyarrow")

    manifest = {
        "version": version,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "input_sources": {source: [str(p) for p in paths] for source, paths in input_paths.items()},
        "row_counts": {
            "before_dedup": int(before_dedup),
            "after_dedup": int(len(df)),
            "dropped_duplicates": int(dropped),
        },
        "null_handling": {
            "label": f"filled with sentinel '{NULL_LABEL_SENTINEL}'",
            "anomaly_type": f"filled with sentinel '{NULL_LABEL_SENTINEL}'",
            "numeric_fields": "coerced with errors='coerce' (NaN for unparseable)",
        },
        "schema_version": VERSION,
    }

    manifest_path = output_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")

    return manifest
