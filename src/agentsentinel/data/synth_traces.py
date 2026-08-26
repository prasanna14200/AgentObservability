"""Generate synthetic AgentSentinel MVP 1 tool-call traces.

Phase 2 only creates labeled synthetic telemetry. It does not define the final
Pydantic trace schema; privacy enforcement moves into that schema in Phase 3.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

DATASET_VERSION = "synthetic-mvp1-v1"
DEFAULT_SEED = 20260818
TOOLS = ("search_customer", "get_order", "update_order")
LIMITATIONS = (
    "Synthetic telemetry generated for MVP 1; does not represent all real "
    "production behavior, may encode generator assumptions, and may make "
    "injected anomalies easier to detect than real anomalies."
)



def generate_dataset(
    normal_count: int = 500,
    anomalous_count: int = 120,
    seed: int = DEFAULT_SEED,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Return deterministic normal traces, anomalous traces, and metadata."""

    if normal_count < 0 or anomalous_count < 0:
        raise ValueError("trace counts must be non-negative")

    rng = random.Random(seed)
    base_time = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)

    normal = [
        _normal_trace(rng, trace_index=i, base_time=base_time)
        for i in range(normal_count)
    ]

    anomaly_types = ("tool_loop", "retry_storm", "resource_spike")
    anomalous: list[dict[str, Any]] = []
    for i in range(anomalous_count):
        anomaly_type = anomaly_types[i % len(anomaly_types)]
        trace_index = normal_count + i
        anomalous.append(
            _anomalous_trace(
                rng,
                trace_index=trace_index,
                base_time=base_time,
                anomaly_type=anomaly_type,
            )
        )

    metadata = {
        "dataset_version": DATASET_VERSION,
        "data_type": "synthetic",
        "seed": seed,
        "normal_count": normal_count,
        "anomalous_count": anomalous_count,
        "anomaly_types": list(anomaly_types),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "limitations": LIMITATIONS,
    }
    return normal, anomalous, metadata


def write_dataset(
    output: Path,
    normal_count: int = 500,
    anomalous_count: int = 120,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Generate and write Phase 2 synthetic traces under the output directory."""

    normal, anomalous, metadata = generate_dataset(
        normal_count=normal_count,
        anomalous_count=anomalous_count,
        seed=seed,
    )
    normal_dir = output / "normal"
    anomalous_dir = output / "anomalous"
    normal_dir.mkdir(parents=True, exist_ok=True)
    anomalous_dir.mkdir(parents=True, exist_ok=True)

    _write_jsonl(normal_dir / "traces.jsonl", normal)
    _write_jsonl(anomalous_dir / "traces.jsonl", anomalous)
    _write_json(output / "metadata.json", metadata)
    _write_readme(output / "README.md", metadata)
    return metadata


def _normal_trace(
    rng: random.Random,
    trace_index: int,
    base_time: datetime,
) -> dict[str, Any]:
    task_type = rng.choice(("order_lookup", "address_change", "delivery_status"))
    agent_id = "support-agent-001"
    task_id = f"task-{trace_index:06d}"
    start = base_time + timedelta(minutes=trace_index * 3)
    tool_sequence = _normal_tool_sequence(rng, task_type)
    events = [
        _event(
            rng,
            trace_id=f"trace-{trace_index:06d}",
            task_id=task_id,
            agent_id=agent_id,
            task_type=task_type,
            event_index=event_index,
            timestamp=start + timedelta(seconds=event_index * rng.randint(4, 20)),
            tool_name=tool_name,
            status="success",
            retry_count=0,
            latency_range=(80, 850),
            token_range=(80, 650),
        )
        for event_index, tool_name in enumerate(tool_sequence)
    ]
    return _trace(trace_index, agent_id, task_id, task_type, "normal", None, events)


def _anomalous_trace(
    rng: random.Random,
    trace_index: int,
    base_time: datetime,
    anomaly_type: str,
) -> dict[str, Any]:
    task_type = rng.choice(("order_lookup", "address_change", "delivery_status"))
    agent_id = "support-agent-001"
    task_id = f"task-{trace_index:06d}"
    start = base_time + timedelta(minutes=trace_index * 3)

    if anomaly_type == "tool_loop":
        tool_sequence = ["search_customer", "get_order"] * 4
        events = [
            _event(
                rng,
                trace_id=f"trace-{trace_index:06d}",
                task_id=task_id,
                agent_id=agent_id,
                task_type=task_type,
                event_index=event_index,
                timestamp=start + timedelta(seconds=event_index * rng.randint(2, 8)),
                tool_name=tool_name,
                status="success",
                retry_count=0,
                latency_range=(90, 700),
                token_range=(90, 500),
            )
            for event_index, tool_name in enumerate(tool_sequence)
        ]
    elif anomaly_type == "retry_storm":
        tool_sequence = ["search_customer", "get_order", "get_order", "get_order", "get_order"]
        events = []
        for event_index, tool_name in enumerate(tool_sequence):
            failed_retry = event_index >= 1
            events.append(
                _event(
                    rng,
                    trace_id=f"trace-{trace_index:06d}",
                    task_id=task_id,
                    agent_id=agent_id,
                    task_type=task_type,
                    event_index=event_index,
                    timestamp=start + timedelta(seconds=event_index * rng.randint(3, 10)),
                    tool_name=tool_name,
                    status="error" if failed_retry else "success",
                    retry_count=event_index if failed_retry else 0,
                    latency_range=(700, 2_400) if failed_retry else (80, 500),
                    token_range=(150, 700),
                )
            )
    elif anomaly_type == "resource_spike":
        tool_sequence = _normal_tool_sequence(rng, task_type)
        events = []
        for event_index, tool_name in enumerate(tool_sequence):
            spike_event = event_index == len(tool_sequence) - 1
            events.append(
                _event(
                    rng,
                    trace_id=f"trace-{trace_index:06d}",
                    task_id=task_id,
                    agent_id=agent_id,
                    task_type=task_type,
                    event_index=event_index,
                    timestamp=start + timedelta(seconds=event_index * rng.randint(4, 20)),
                    tool_name=tool_name,
                    status="success",
                    retry_count=0,
                    latency_range=(900, 2_500) if spike_event else (80, 700),
                    token_range=(4_000, 8_500) if spike_event else (80, 650),
                )
            )
    else:
        raise ValueError(f"unsupported anomaly_type: {anomaly_type}")

    return _trace(trace_index, agent_id, task_id, task_type, "anomalous", anomaly_type, events)


def _normal_tool_sequence(rng: random.Random, task_type: str) -> list[str]:
    if task_type == "order_lookup":
        return ["search_customer", "get_order"]
    if task_type == "address_change":
        return ["search_customer", "get_order", "update_order"]
    sequence = ["search_customer", "get_order"]
    if rng.random() < 0.25:
        sequence.append("get_order")
    return sequence


def _trace(
    trace_index: int,
    agent_id: str,
    task_id: str,
    task_type: str,
    label: str,
    anomaly_type: str | None,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "trace_id": f"trace-{trace_index:06d}",
        "dataset_version": DATASET_VERSION,
        "data_type": "synthetic",
        "limitations": LIMITATIONS,
        "label": label,
        "anomaly_type": anomaly_type,
        "agent_id": agent_id,
        "task_id": task_id,
        "task_type": task_type,
        "events": events,
    }


def _event(
    rng: random.Random,
    trace_id: str,
    task_id: str,
    agent_id: str,
    task_type: str,
    event_index: int,
    timestamp: datetime,
    tool_name: str,
    status: str,
    retry_count: int,
    latency_range: tuple[int, int],
    token_range: tuple[int, int],
) -> dict[str, Any]:
    token_usage = rng.randint(*token_range)
    latency_ms = rng.randint(*latency_range)
    return {
        "event_id": f"{trace_id}-event-{event_index:02d}",
        "trace_id": trace_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "timestamp": timestamp.isoformat(),
        "event_type": "tool_call",
        "tool_name": tool_name,
        "tool_input_metadata": _tool_input_metadata(rng, tool_name),
        "tool_output_metadata": {
            "record_count": rng.randint(0, 3),
            "result_size_bytes": rng.randint(250, 4_000),
        },
        "latency_ms": latency_ms,
        "status": status,
        "retry_count": retry_count,
        "token_usage": token_usage,
        "estimated_cost_usd": round(token_usage * 0.000002, 6),
        "permissions": _permissions_for_tool(tool_name),
        "task_type": task_type,
    }


def _tool_input_metadata(rng: random.Random, tool_name: str) -> dict[str, Any]:
    customer_ref = f"cust_hash_{rng.randint(10000, 99999)}"
    if tool_name == "search_customer":
        return {"customer_ref_hash": customer_ref, "query_kind": "customer_lookup"}
    if tool_name == "get_order":
        return {
            "customer_ref_hash": customer_ref,
            "order_ref_hash": f"order_hash_{rng.randint(10000, 99999)}",
        }
    return {
        "customer_ref_hash": customer_ref,
        "order_ref_hash": f"order_hash_{rng.randint(10000, 99999)}",
        "update_kind": rng.choice(("address_note", "delivery_instruction")),
    }


def _permissions_for_tool(tool_name: str) -> list[str]:
    if tool_name == "update_order":
        return ["orders:read", "orders:write"]
    return ["orders:read"]


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_readme(path: Path, metadata: dict[str, Any]) -> None:
    text = f"""# AgentSentinel Synthetic MVP 1 Data

DATA TYPE: SYNTHETIC

Dataset version: {metadata["dataset_version"]}
Seed: {metadata["seed"]}
Normal traces: {metadata["normal_count"]}
Anomalous traces: {metadata["anomalous_count"]}
Anomaly types: {", ".join(metadata["anomaly_types"])}

## Limitations

{metadata["limitations"]}

## Files

- `normal/traces.jsonl`: normal customer-support tool-call trajectories.
- `anomalous/traces.jsonl`: labeled injected anomalies.
- `metadata.json`: generation parameters and dataset metadata.
"""
    path.write_text(text, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/synthetic"))
    parser.add_argument("--normal-count", type=int, default=500)
    parser.add_argument("--anomalous-count", type=int, default=120)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    metadata = write_dataset(
        output=args.output,
        normal_count=args.normal_count,
        anomalous_count=args.anomalous_count,
        seed=args.seed,
    )
    print(
        "Wrote synthetic dataset "
        f"{metadata['dataset_version']} with {metadata['normal_count']} normal "
        f"and {metadata['anomalous_count']} anomalous traces to {args.output}"
    )


if __name__ == "__main__":
    main()
