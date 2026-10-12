from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agentsentinel.data.synth_traces import LIMITATIONS, DATASET_VERSION, generate_dataset, write_dataset
from agentsentinel.schema.trace import TraceRecord


def test_generate_dataset_meets_phase_2_counts_and_labels() -> None:
    normal, anomalous, metadata = generate_dataset(
        normal_count=500,
        anomalous_count=120,
        seed=123,
    )

    assert len(normal) == 500
    assert len(anomalous) == 120
    assert metadata["data_type"] == "synthetic"
    assert metadata["dataset_version"] == DATASET_VERSION
    assert metadata["normal_count"] == 500
    assert metadata["anomalous_count"] == 120

    assert {trace["label"] for trace in normal} == {"normal"}
    assert {trace["data_type"] for trace in normal + anomalous} == {"synthetic"}
    assert {trace["limitations"] for trace in normal + anomalous} == {LIMITATIONS}
    assert {trace["anomaly_type"] for trace in anomalous} == {
        "tool_loop",
        "retry_storm",
        "resource_spike",
    }


def test_generated_events_use_mvp_tools_and_redacted_metadata() -> None:
    normal, anomalous, _ = generate_dataset(normal_count=20, anomalous_count=9, seed=456)
    allowed_tools = {"search_customer", "get_order", "update_order"}

    for trace in normal + anomalous:
        assert trace["events"]
        for event in trace["events"]:
            assert event["tool_name"] in allowed_tools
            serialized_input = json.dumps(event["tool_input_metadata"]).lower()
            assert "password" not in serialized_input
            assert "api_key" not in serialized_input
            assert "token" not in serialized_input
            assert "hash" in serialized_input


def test_write_dataset_is_deterministic_for_same_seed(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    write_dataset(first, normal_count=12, anomalous_count=6, seed=789)
    write_dataset(second, normal_count=12, anomalous_count=6, seed=789)

    assert _sha256(first / "normal" / "traces.jsonl") == _sha256(
        second / "normal" / "traces.jsonl"
    )
    assert _sha256(first / "anomalous" / "traces.jsonl") == _sha256(
        second / "anomalous" / "traces.jsonl"
    )


def test_write_dataset_creates_expected_files(tmp_path: Path) -> None:
    write_dataset(tmp_path, normal_count=5, anomalous_count=3, seed=101)

    assert (tmp_path / "normal" / "traces.jsonl").exists()
    assert (tmp_path / "anomalous" / "traces.jsonl").exists()
    assert (tmp_path / "metadata.json").exists()
    assert "DATA TYPE: SYNTHETIC" in (tmp_path / "README.md").read_text(encoding="utf-8")


def test_generate_dataset_interleaves_timestamps() -> None:
    normal, anomalous, _ = generate_dataset(normal_count=50, anomalous_count=30, seed=42)
    all_traces = normal + anomalous

    timestamps = [trace["events"][0]["timestamp"] for trace in all_traces if trace["events"]]
    labels = [trace["label"] for trace in all_traces if trace["events"]]

    normal_timestamps = [ts for ts, label in zip(timestamps, labels) if label == "normal"]
    anomalous_timestamps = [ts for ts, label in zip(timestamps, labels) if label == "anomalous"]

    assert min(normal_timestamps) < min(anomalous_timestamps)
    assert max(normal_timestamps) > min(anomalous_timestamps)
    assert max(anomalous_timestamps) > min(normal_timestamps)


def test_chronological_test_window_contains_both_classes() -> None:
    normal, anomalous, _ = generate_dataset(seed=20260818)
    ordered = sorted(normal + anomalous, key=lambda trace: trace["events"][0]["timestamp"])
    last_twenty_percent = ordered[int(len(ordered) * 0.8):]
    assert {trace["label"] for trace in last_twenty_percent} == {"normal", "anomalous"}


def test_corrected_generated_dataset_validates_against_existing_schema() -> None:
    normal, anomalous, _ = generate_dataset(seed=20260818)
    for trace in normal + anomalous:
        assert TraceRecord.model_validate(trace).to_phase2_record() == trace


def test_generate_dataset_deterministic_with_shuffle() -> None:
    first_normal, first_anomalous, _ = generate_dataset(normal_count=60, anomalous_count=20, seed=77)
    second_normal, second_anomalous, _ = generate_dataset(normal_count=60, anomalous_count=20, seed=77)

    assert len(first_normal) == len(second_normal) == 60
    assert len(first_anomalous) == len(second_anomalous) == 20

    for i, (a, b) in enumerate(zip(first_normal, second_normal)):
        assert a == b, f"Normal trace {i} differs between runs with same seed"
    for i, (a, b) in enumerate(zip(first_anomalous, second_anomalous)):
        assert a == b, f"Anomalous trace {i} differs between runs with same seed"


def test_generate_dataset_different_seed_produces_different_output() -> None:
    first_normal, first_anomalous, _ = generate_dataset(normal_count=10, anomalous_count=5, seed=1)
    second_normal, second_anomalous, _ = generate_dataset(normal_count=10, anomalous_count=5, seed=2)

    assert first_normal != second_normal or first_anomalous != second_anomalous


def test_generate_dataset_version_is_bumped() -> None:
    _, _, metadata = generate_dataset()
    assert metadata["dataset_version"] == DATASET_VERSION
    assert metadata["dataset_version"] != "synthetic-mvp1-v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
