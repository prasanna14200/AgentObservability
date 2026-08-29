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

## Phase 5 — Preprocessing

`TraceCollector` sanitizes input before schema validation, so both accepted and rejected paths write only redacted values. Rejection payloads are extracted from `data/collected/rejected/rejections.jsonl` during preprocessing and included with `source="collected"`.

Preprocessing loads both synthetic (`data/synthetic/`) and collected (`data/collected/`) JSONL into a single DataFrame, with a `source` column preserving provenance. It does NOT perform feature engineering.

Why preprocessing is separated from feature engineering:

- Preprocessing normalizes units, parses timestamps, deduplicates, and flattens nested structures. Feature engineering computes derived behavior (e.g., calls per minute, error rates, latency deltas). Mixing them makes the pipeline harder to test, audit, and version independently.
- Phase 6 can add new features without changing the cleaned base schema. Phase 5 can fix data-quality bugs without changing feature definitions.

Null-handling choices:

- `label` and `anomaly_type` are nullable in the schema. Real traces often have no label. Preprocessing fills missing values with `__UNKNOWN__` rather than dropping rows, because the absence of a label is informative (it tells Phase 6 to treat the row as unlabeled).
- Numeric fields (`latency_ms`, `retry_count`, `token_usage`, `estimated_cost_usd`, `record_count`, `result_size_bytes`) are coerced with `pd.to_numeric(errors="coerce")`, producing `NaN` for unparseable values rather than crashing.

Versioned output format:

- Processed traces are written to `data/processed/<version>/traces.parquet`. Parquet preserves schema, compresses well, and loads fast in pandas for Phase 6/7. CSV is a human-readable fallback, but parquet avoids silent dtype drift.
- A small `manifest.json` records input sources, row counts before/after dedup, null-handling decisions, schema version, and generation timestamp.

What is deliberately left out:

- No derived ratios or baselines; Phase 6 owns feature engineering.
- No EDA plots/notebooks; Phase 7 consumes this phase's output.
- No changes to Phase 3 schema or Phase 4 collector logic.

Rejection-log exclusion:

- `data/collected/rejected/rejections.jsonl` is an audit trail for malformed input. It is NOT loaded into `data/processed/v1/traces.parquet`. Schema-invalid records must not enter feature baselines or the modeling dataset.
- `_flatten_trace` skips dicts without an `events` key, so even if a rejection file were accidentally pointed at by `INPUT_PATHS`, it would produce zero rows rather than placeholder rows.

## Phase 6 — Contextual Feature Engineering

Features are computed relative to agent identity, task type, and in-trace trajectory. The same raw value (e.g., 100 tool calls, 500ms latency) produces different feature outputs under different contexts because baselines are scoped per agent and per task type.

Why features are contextual rather than absolute:

- Global thresholds misclassify: a 500ms call is normal for a slow agent but anomalous for a fast agent. A 100-tool-call trace is normal for `order_lookup` but abnormal for `delivery_status`.
- Z-scores normalize each observation against its relevant distribution, making the feature matrix comparable across heterogeneous agents and tasks.
- Permission matching is deterministic (boolean) because policy violations are not statistical anomalies — they feed both the rules engine and risk engine as hard flags.

Baseline windowing/scoping choices:

- Agent-level baselines use all events for that `agent_id` in the processed dataset. With a single agent in MVP 1, agent and global distributions are identical, but the code is structured to scale to multiple agents.
- Task-type baselines use all events for that `task_type`.
- Agent+task-type baselines use all events for the specific (`agent_id`, `task_type`) pair. This is the finest granularity and produces the most discriminating z-scores.
- Trajectory features are computed per trace (position, cumulative calls, inter-call timing, tool repetition) and do not use cross-trace baselines.

What is deferred:

- Sequence-aware modeling (Phase 11) will use raw event sequences, not these aggregated contextual features.
- Rules thresholds (Phase 8) and Isolation Forest (Phase 8) will consume this feature matrix but are not built here.
- EDA notebooks (Phase 7) consume this phase's output.
