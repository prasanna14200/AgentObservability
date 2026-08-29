# Design Decisions

## Phase 3 — Trace Schema And Redaction

The canonical trace schema is defined with Pydantic in `src/agentsentinel/schema/trace.py`. It accepts the Phase 2 JSONL shape without dropping fields, but internally separates audit metadata from tool-call content.

Why these fields:

- `trace_id`, `agent_id`, `task_id`, and `task_type` identify the trajectory and support later contextual baselines.
- `data_type` and `limitations` preserve provenance so synthetic and real runtime traces are never mixed silently.
- `label` and `anomaly_type` are nullable because synthetic/evaluation traces can be labeled, while real runtime traces usually are not.
- `policy_version` is nullable because the policy engine will populate it later; Phase 3 only reserves the audit slot.
- Tool name, permissions, timestamp, status, latency, retry count, token usage, and estimated cost support later feature engineering, detectors, risk scoring, policy decisions, and audit replay.

Why redaction is built in now:

Privacy must start at schema ingestion. `ToolArguments` runs tool-input metadata through `redact_mapping`, hashing values for sensitive keys such as API keys, access tokens, passwords, credentials, and secrets while preserving argument shape for auditing. This prevents downstream feature extraction, detectors, or persistence from becoming the first line of defense.

What was deliberately left out:

- No final feature vectors; Phase 6 owns feature engineering.
- No detector scores; Phases 8-11 own detectors.
- No risk or policy decision object; Phases 12-13 own that boundary.
- No database model or migration; Phase 20 owns persistence.
- No live request or FastAPI wrapper; Phase 13 introduces the live safety path.

## Phase 4 — Local Trace Collection

`TraceCollector` writes accepted traces to local JSONL files under `data/collected/` using the same label-based convention as Phase 2. This keeps MVP 1 reproducible with local files and avoids introducing OpenTelemetry, FastAPI, Postgres, or external services before the phases that need them.

Why file-based collection now:

- Phase 4 needs a reusable capture mechanism, not a distributed telemetry stack.
- JSONL matches the Phase 2 data format and keeps later preprocessing simple.
- Local files are easy to test deterministically and inspect during interviews.
- OTel export belongs to Phase 14, and database persistence belongs to Phase 20.

Malformed-input policy:

- Invalid payloads are rejected.
- The collector returns `None` instead of crashing the caller.
- A sanitized copy of the payload and the validation reason are written to `data/collected/rejected/rejections.jsonl`.
- Partial acceptance is deliberately avoided because later audit replay must not depend on half-valid records.

What is deferred:

- Runtime interception and live request handling are deferred to Phase 13.
- Fail-open/fail-closed behavior is deferred to the policy and runtime phases.
- OpenTelemetry abstraction is deferred to Phase 14.
- Model, telemetry, and service outage behavior is deferred to Phase 15.
