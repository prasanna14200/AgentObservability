from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agentsentinel.schema.redaction import hash_sensitive_value, is_sensitive_key, redact_mapping
from agentsentinel.schema.trace import ToolArguments, TraceRecord

SYNTHETIC_PATHS = (
    Path("data/synthetic/normal/traces.jsonl"),
    Path("data/synthetic/anomalous/traces.jsonl"),
)


def test_all_phase2_synthetic_traces_validate_with_zero_data_loss() -> None:
    validated_count = 0

    for path in SYNTHETIC_PATHS:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = json.loads(line)
                trace = TraceRecord.model_validate(raw)

                assert trace.audit.data_type == "synthetic"
                assert trace.audit.limitations == raw["limitations"]
                assert trace.to_phase2_record() == raw
                validated_count += 1

    assert validated_count == 620


def test_tool_arguments_redact_sensitive_values_without_changing_shape() -> None:
    arguments = ToolArguments.model_validate(
        {
            "customer_ref_hash": "cust_hash_12345",
            "api_key": "raw-key",
            "nested": {"password": "raw-password"},
        }
    )

    assert arguments.redacted is True
    assert arguments.values["customer_ref_hash"] == "cust_hash_12345"
    assert arguments.values["api_key"].startswith("sha256:")
    assert arguments.values["nested"]["password"].startswith("sha256:")
    assert "raw-key" not in json.dumps(arguments.values)
    assert "raw-password" not in json.dumps(arguments.values)


def test_redaction_utility_is_deterministic_and_key_based() -> None:
    clean, redacted = redact_mapping({"access_token": "abc", "safe": "value"})

    assert redacted is True
    assert clean["access_token"] == hash_sensitive_value("abc")
    assert clean["safe"] == "value"
    assert is_sensitive_key("refresh-token") is True
    assert is_sensitive_key("customer_ref_hash") is False


def test_redaction_catches_bare_and_compound_token_fields() -> None:
    """Regression test for Phase 4 patch: prior SENSITIVE_KEY_PARTS only
    matched the compound names access_token/auth_token/refresh_token, so a
    field literally named 'token' (or other *_token variants) was written
    to disk unredacted. See docs/phase4_completion.md."""

    assert is_sensitive_key("token") is True
    assert is_sensitive_key("Token") is True
    assert is_sensitive_key("session_token") is True
    assert is_sensitive_key("bearer_token") is True
    assert is_sensitive_key("id_token") is True

    clean, redacted = redact_mapping(
        {"token": "raw-bare-token", "session_token": "raw-session-token", "safe": "value"}
    )

    assert redacted is True
    assert clean["token"].startswith("sha256:")
    assert clean["session_token"].startswith("sha256:")
    assert clean["safe"] == "value"
    assert "raw-bare-token" not in json.dumps(clean)
    assert "raw-session-token" not in json.dumps(clean)


def test_schema_does_not_define_raw_secret_fields() -> None:
    banned_names = {"api_key", "access_token", "password", "credential", "secret"}
    field_names = _collect_model_field_names(TraceRecord)

    assert field_names.isdisjoint(banned_names)


def test_real_trace_may_have_nullable_label_and_policy_version() -> None:
    raw = json.loads(Path("data/synthetic/normal/traces.jsonl").read_text(encoding="utf-8").splitlines()[0])
    raw["data_type"] = "real"
    raw["dataset_version"] = None
    raw["limitations"] = None
    raw["label"] = None
    raw["anomaly_type"] = None
    raw["policy_version"] = None

    trace = TraceRecord.model_validate(raw)

    assert trace.audit.data_type == "real"
    assert trace.audit.label is None
    assert trace.audit.anomaly_type is None
    assert trace.audit.policy_version is None


def _collect_model_field_names(model: type[BaseModel]) -> set[str]:
    names: set[str] = set()
    for field_name, field_info in model.model_fields.items():
        names.add(field_name)
        annotation = field_info.annotation
        nested = _extract_model(annotation)
        if nested is not None:
            names.update(_collect_model_field_names(nested))
    return names


def _extract_model(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())
    if origin in {list, tuple}:
        for arg in args:
            nested = _extract_model(arg)
            if nested is not None:
                return nested
    return None
