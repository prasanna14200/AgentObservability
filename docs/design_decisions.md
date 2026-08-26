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
