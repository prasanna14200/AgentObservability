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


def test_collector_redacts_bare_token_fields_in_rejection_log(tmp_path: Path) -> None:
    """Regression test: a malformed payload carrying a bare 'token' (or
    'session_token') field must not reach data/collected/rejected/rejections.jsonl
    unredacted. Prior to the Phase 4 patch, SENSITIVE_KEY_PARTS only matched
    access_token/auth_token/refresh_token, so this leaked raw to disk."""

    collector = TraceCollector(sink_root=tmp_path / "collected")
    malformed = {
        "trace_id": "trace-token-regression",
        "token": "RAW-BARE-TOKEN-SECRET",
        "session_token": "RAW-SESSION-TOKEN-SECRET",
        # deliberately missing required fields -> fails validation -> rejection path
    }

    rejected = collector.capture(malformed)

    assert rejected is None
    assert collector.rejections
    rejection_sink = tmp_path / "collected" / "rejected" / "rejections.jsonl"
    rejection_text = rejection_sink.read_text(encoding="utf-8")
    assert "RAW-BARE-TOKEN-SECRET" not in rejection_text
    assert "RAW-SESSION-TOKEN-SECRET" not in rejection_text
    assert "trace-token-regression" in rejection_text
    assert "sha256:" in rejection_text


def test_no_unredacted_secret_reaches_any_collected_jsonl(tmp_path: Path) -> None:
    """Exhaustive proof: a payload carrying every secret-shaped field across
    nested structures must not reach any data/collected/**/*.jsonl unredacted,
    INCLUDING the rejection log. The collector sanitizes before validation, so
    both accepted and rejected paths write only redacted values."""

    collector = TraceCollector(sink_root=tmp_path / "collected")
    raw_event = {
        "trace_id": "trace-redaction-proof",
        "dataset_version": "v1",
        "data_type": "real",
        "label": "normal",
        "agent_id": "agent-001",
        "task_id": "task-001",
        "task_type": "order_lookup",
        "events": [
            {
                "event_id": "evt-001",
                "trace_id": "trace-redaction-proof",
                "agent_id": "agent-001",
                "task_id": "task-001",
                "task_type": "order_lookup",
                "timestamp": "2026-01-01T09:00:00+00:00",
                "event_type": "tool_call",
                "tool_name": "search_customer",
                "tool_input_metadata": {
                    "customer_ref_hash": "safe-value",
                    "api_key": "RAW-API-KEY",
                    "access_token": "RAW-ACCESS-TOKEN",
                    "password": "RAW-PASSWORD",
                    "secret": "RAW-SECRET",
                    "credential": "RAW-CREDENTIAL",
                    "token": "RAW-BARE-TOKEN",
                    "session_token": "RAW-SESSION-TOKEN",
                    "nested": {
                        "apikey": "RAW-APIKEY-NESTED",
                        "bearer_token": "RAW-BEARER-TOKEN",
                    },
                    "list_of_dicts": [
                        {"id_token": "RAW-ID-TOKEN"},
                    ],
                },
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

    result = collector.capture(raw_event)

    all_files = list((tmp_path / "collected").rglob("*.jsonl"))
    all_text = "".join(path.read_text(encoding="utf-8") for path in all_files)

    assert "RAW-API-KEY" not in all_text
    assert "RAW-ACCESS-TOKEN" not in all_text
    assert "RAW-PASSWORD" not in all_text
    assert "RAW-SECRET" not in all_text
    assert "RAW-CREDENTIAL" not in all_text
    assert "RAW-BARE-TOKEN" not in all_text
    assert "RAW-SESSION-TOKEN" not in all_text
    assert "RAW-APIKEY-NESTED" not in all_text
    assert "RAW-BEARER-TOKEN" not in all_text
    assert "RAW-ID-TOKEN" not in all_text
    assert "safe-value" in all_text


def test_collector_default_sink_uses_collected_data_root() -> None:
    collector = TraceCollector()

    assert collector.sink_root == Path("data/collected")


def _load_first(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])
