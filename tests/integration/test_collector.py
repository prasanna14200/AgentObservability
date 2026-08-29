from __future__ import annotations

import copy
import json
from pathlib import Path

from agentsentinel.observability.collector import TraceCollector
from agentsentinel.schema.trace import TraceRecord


def test_collector_captures_phase2_traces_and_rejects_malformed_input(tmp_path: Path) -> None:
    collector = TraceCollector(sink_root=tmp_path / "collected")
    normal = _load_first(Path("data/synthetic/normal/traces.jsonl"))
    anomalous = _load_first(Path("data/synthetic/anomalous/traces.jsonl"))
    normal_with_secret = copy.deepcopy(normal)
    normal_with_secret["events"][0]["tool_input_metadata"]["api_key"] = "raw-secret-value"

    accepted_normal = collector.capture(normal_with_secret)
    accepted_anomalous = collector.capture(anomalous)

    assert isinstance(accepted_normal, TraceRecord)
    assert isinstance(accepted_anomalous, TraceRecord)
    assert accepted_normal.audit.data_type == normal["data_type"]
    assert accepted_normal.audit.limitations == normal["limitations"]
    assert accepted_anomalous.audit.anomaly_type == "tool_loop"
    assert accepted_normal.events[0].content.arguments.redacted is True

    normal_sink = tmp_path / "collected" / "normal" / "traces.jsonl"
    anomalous_sink = tmp_path / "collected" / "anomalous" / "traces.jsonl"
    assert normal_sink.exists()
    assert anomalous_sink.exists()
    assert "raw-secret-value" not in normal_sink.read_text(encoding="utf-8")
    assert "sha256:" in normal_sink.read_text(encoding="utf-8")

    malformed = copy.deepcopy(normal)
    malformed["api_key"] = "malformed-top-level-secret"
    del malformed["trace_id"]

    rejected = collector.capture(malformed)

    assert rejected is None
    assert collector.rejections
    rejection_sink = tmp_path / "collected" / "rejected" / "rejections.jsonl"
    rejection_text = rejection_sink.read_text(encoding="utf-8")
    assert "trace_id" in rejection_text
    assert "malformed-top-level-secret" not in rejection_text
    assert "sha256:" in rejection_text


def test_collector_default_sink_uses_collected_data_root() -> None:
    collector = TraceCollector()

    assert collector.sink_root == Path("data/collected")


def _load_first(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])
