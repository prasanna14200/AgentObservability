"""Phase 6 — contextual feature engineering.

Builds features relative to agent identity, task type, and in-trace
trajectory. Does NOT train models or build rules.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from agentsentinel.data.preprocess import OUTPUT_DIR as PROCESSED_DIR

VERSION = "v1"
OUTPUT_DIR = Path("data/features") / VERSION
INPUT_PATH = PROCESSED_DIR / "traces.parquet"

METRIC_COLUMNS = ["latency_ms", "retry_count", "token_usage", "estimated_cost_usd"]

METRIC_SUFFIXES = {
    "latency_ms": "latency",
    "retry_count": "retry",
    "token_usage": "token",
    "estimated_cost_usd": "cost",
}

FEATURE_MANIFEST: list[dict[str, str]] = [
    {
        "name": "agent_latency_zscore",
        "derivation": "(latency_ms - agent_mean_latency_ms) / agent_std_latency_ms",
        "data_type": "float64",
        "baseline_scope": "agent-level",
        "contextual_rationale": "A 500ms call is normal for a slow agent but anomalous for a fast agent; global thresholds would misclassify.",
    },
    {
        "name": "agent_retry_zscore",
        "derivation": "(retry_count - agent_mean_retry_count) / agent_std_retry_count",
        "data_type": "float64",
        "baseline_scope": "agent-level",
        "contextual_rationale": "Retry behavior varies by agent configuration; absolute retry thresholds ignore agent-specific baselines.",
    },
    {
        "name": "agent_token_zscore",
        "derivation": "(token_usage - agent_mean_token_usage) / agent_std_token_usage",
        "data_type": "float64",
        "baseline_scope": "agent-level",
        "contextual_rationale": "Token consumption depends on agent prompt templates and tool chains; only the agent's own history sets context.",
    },
    {
        "name": "agent_cost_zscore",
        "derivation": "(estimated_cost_usd - agent_mean_cost) / agent_std_cost",
        "data_type": "float64",
        "baseline_scope": "agent-level",
        "contextual_rationale": "Cost baselines must be agent-relative because different agents use different models and tools.",
    },
    {
        "name": "task_latency_zscore",
        "derivation": "(latency_ms - task_type_mean_latency_ms) / task_type_std_latency_ms",
        "data_type": "float64",
        "baseline_scope": "task-type-level",
        "contextual_rationale": "A 100-tool-call trace is normal for order_lookup but abnormal for delivery_status; task context sets the norm.",
    },
    {
        "name": "task_retry_zscore",
        "derivation": "(retry_count - task_type_mean_retry_count) / task_type_std_retry_count",
        "data_type": "float64",
        "baseline_scope": "task-type-level",
        "contextual_rationale": "Retry storms are expected for complex multi-step tasks; task-relative z-scores distinguish normal complexity from failure.",
    },
    {
        "name": "task_token_zscore",
        "derivation": "(token_usage - task_type_mean_token_usage) / task_type_std_token_usage",
        "data_type": "float64",
        "baseline_scope": "task-type-level",
        "contextual_rationale": "Token budgets differ by task complexity; a high token count for a simple lookup is more suspicious than for a multi-step task.",
    },
    {
        "name": "task_cost_zscore",
        "derivation": "(estimated_cost_usd - task_type_mean_cost) / task_type_std_cost",
        "data_type": "float64",
        "baseline_scope": "task-type-level",
        "contextual_rationale": "Cost spikes must be evaluated against the task's normal budget, not a global average.",
    },
    {
        "name": "agent_task_latency_zscore",
        "derivation": "(latency_ms - agent_task_mean_latency_ms) / agent_task_std_latency_ms",
        "data_type": "float64",
        "baseline_scope": "agent+task-type-level",
        "contextual_rationale": "Combines agent and task context: the same latency can be normal for agent A on task X but anomalous for agent B on task X.",
    },
    {
        "name": "agent_task_retry_zscore",
        "derivation": "(retry_count - agent_task_mean_retry_count) / agent_task_std_retry_count",
        "data_type": "float64",
        "baseline_scope": "agent+task-type-level",
        "contextual_rationale": "Retry behavior is most comparable when both agent and task are held constant.",
    },
    {
        "name": "agent_task_token_zscore",
        "derivation": "(token_usage - agent_task_mean_token_usage) / agent_task_std_token_usage",
        "data_type": "float64",
        "baseline_scope": "agent+task-type-level",
        "contextual_rationale": "Token usage varies by agent model and task complexity; the joint baseline is the fairest comparator.",
    },
    {
        "name": "agent_task_cost_zscore",
        "derivation": "(estimated_cost_usd - agent_task_mean_cost) / agent_task_std_cost",
        "data_type": "float64",
        "baseline_scope": "agent+task-type-level",
        "contextual_rationale": "Cost anomalies are most meaningful when compared to the specific agent-task pair's history.",
    },
    {
        "name": "call_position",
        "derivation": "1-based index of this event within its trace's event sequence",
        "data_type": "int64",
        "baseline_scope": "trace-level",
        "contextual_rationale": "Position matters because early vs late failures have different diagnostic meaning.",
    },
    {
        "name": "calls_so_far",
        "derivation": "Cumulative count of events in the trace up to and including this event",
        "data_type": "int64",
        "baseline_scope": "trace-level",
        "contextual_rationale": "The same tool call count is normal for long workflows but abnormal for short ones; position provides that context.",
    },
    {
        "name": "time_since_last_call_ms",
        "derivation": "Milliseconds between this event and the previous event in the same trace; NaN for the first event",
        "data_type": "float64",
        "baseline_scope": "trace-level",
        "contextual_rationale": "Inter-call timing reveals stalls and loops that absolute latency masks.",
    },
    {
        "name": "tool_repeat_in_session",
        "derivation": "Number of times the same tool_name has been called earlier in this trace (0 for first occurrence)",
        "data_type": "int64",
        "baseline_scope": "trace-level",
        "contextual_rationale": "Repetition within a session signals loops or retries that a simple call count cannot distinguish.",
    },
    {
        "name": "permission_match",
        "derivation": "True if the event's declared permissions superset the tool's required permissions; False otherwise",
        "data_type": "bool",
        "baseline_scope": "deterministic",
        "contextual_rationale": "Permission mismatches are policy violations, not statistical anomalies; this flag feeds both rules and risk engines.",
    },
    {
        "name": "trace_event_count",
        "derivation": "Total number of events in the trace (repeated for each event in the trace)",
        "data_type": "int64",
        "baseline_scope": "trace-level",
        "contextual_rationale": "A 5-call trace is normal for simple tasks; without trace context, raw counts are meaningless.",
    },
]


def _parse_json(series: pd.Series) -> pd.Series:
    def _parse(val: Any) -> Any:
        if isinstance(val, str):
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                return val
        return val

    return series.apply(_parse)


def _build_tool_permissions_map(df: pd.DataFrame) -> dict[str, list[str]]:
    perms_col = _parse_json(df["permissions"])
    tool_perms: dict[str, list[str]] = {}
    for tool_name, group in df.groupby("tool_name", sort=False):
        union: set[str] = set()
        for perms in perms_col.loc[group.index]:
            if isinstance(perms, list):
                union.update(perms)
        tool_perms[tool_name] = sorted(union)
    return tool_perms


def _safe_zscore(value: pd.Series, mean: pd.Series, std: pd.Series) -> pd.Series:
    z = (value - mean) / std
    z = z.replace([float("inf"), float("-inf")], float("nan"))
    return z.fillna(0.0)


def _add_trajectory_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["trace_id", "timestamp"]).copy()
    df["call_position"] = df.groupby("trace_id").cumcount() + 1
    df["calls_so_far"] = df.groupby("trace_id")["trace_id"].cumcount() + 1
    df["time_since_last_call_ms"] = (
        df.groupby("trace_id")["timestamp"].diff().dt.total_seconds() * 1000
    )
    df["tool_repeat_in_session"] = df.groupby(["trace_id", "tool_name"]).cumcount()

    trace_event_count = df.groupby("trace_id")["event_id"].count().rename("trace_event_count")
    df = df.merge(trace_event_count.reset_index(), on="trace_id", how="left")
    return df


def _add_permission_match(df: pd.DataFrame, tool_perms_map: dict[str, list[str]]) -> pd.DataFrame:
    agent_perms = _parse_json(df["permissions"]).apply(lambda x: x if isinstance(x, list) else [])
    required_perms = df["tool_name"].map(lambda t: tool_perms_map.get(t, []))

    df["permission_match"] = [
        set(req).issubset(set(agent)) for agent, req in zip(agent_perms, required_perms)
    ]
    return df


def _add_baseline_features(df: pd.DataFrame) -> pd.DataFrame:
    for metric in METRIC_COLUMNS:
        suffix = METRIC_SUFFIXES[metric]

        agent_stats = df.groupby("agent_id")[metric].agg(["mean", "std"]).reset_index()
        agent_stats.columns = ["agent_id", f"agent_{suffix}_mean", f"agent_{suffix}_std"]
        df = df.merge(agent_stats, on="agent_id", how="left")

        task_stats = df.groupby("task_type")[metric].agg(["mean", "std"]).reset_index()
        task_stats.columns = ["task_type", f"task_{suffix}_mean", f"task_{suffix}_std"]
        df = df.merge(task_stats, on="task_type", how="left")

        agent_task_stats = (
            df.groupby(["agent_id", "task_type"])[metric].agg(["mean", "std"]).reset_index()
        )
        agent_task_stats.columns = [
            "agent_id",
            "task_type",
            f"agent_task_{suffix}_mean",
            f"agent_task_{suffix}_std",
        ]
        df = df.merge(agent_task_stats, on=["agent_id", "task_type"], how="left")

        df[f"agent_{suffix}_zscore"] = _safe_zscore(
            df[metric], df[f"agent_{suffix}_mean"], df[f"agent_{suffix}_std"]
        )
        df[f"task_{suffix}_zscore"] = _safe_zscore(
            df[metric], df[f"task_{suffix}_mean"], df[f"task_{suffix}_std"]
        )
        df[f"agent_task_{suffix}_zscore"] = _safe_zscore(
            df[metric], df[f"agent_task_{suffix}_mean"], df[f"agent_task_{suffix}_std"]
        )

        df.drop(
            columns=[
                f"agent_{suffix}_mean",
                f"agent_{suffix}_std",
                f"task_{suffix}_mean",
                f"task_{suffix}_std",
                f"agent_task_{suffix}_mean",
                f"agent_task_{suffix}_std",
            ],
            inplace=True,
        )

    return df


def build_features(output_dir: Path = OUTPUT_DIR, version: str = VERSION) -> dict[str, Any]:
    df = pd.read_parquet(INPUT_PATH)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    tool_perms_map = _build_tool_permissions_map(df)
    df = _add_trajectory_features(df)
    df = _add_permission_match(df, tool_perms_map)
    df = _add_baseline_features(df)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "features.parquet"
    df.to_parquet(output_path, index=False, engine="pyarrow")

    manifest = {
        "version": version,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "input_source": str(INPUT_PATH),
        "features": FEATURE_MANIFEST,
        "row_count": int(len(df)),
    }

    manifest_path = output_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")

    return manifest
