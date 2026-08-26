from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agentsentinel.data.synth_traces import LIMITATIONS, generate_dataset, write_dataset


def test_generate_dataset_meets_phase_2_counts_and_labels() -> None:
    normal, anomalous, metadata = generate_dataset(
        normal_count=500,
        anomalous_count=120,
        seed=123,
    )

    assert len(normal) == 500
    assert len(anomalous) == 120
    assert metadata["data_type"] == "synthetic"
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
