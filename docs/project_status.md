# AgentSentinel Project Status

Status date: 2026-08-18

## Specification Of Record

AgentSentinel is a runtime safety control plane for AI agent tool calls. It observes tool requests, extracts behavioral features, scores behavior using deterministic rules and learned detectors, and applies a separate policy engine that makes the final decision.

Important invariant: anomaly signals are advisory. A deterministic policy engine is the only component that can decide whether to allow, pause, confirm, downgrade, terminate, or block a tool request.

## Current Phase

**Phase 4 — Instrumentation / Collection**

Goal: build the local collector that validates, redacts, and records trace payloads without adding a live request path, policy engine, network service, or database.

## Locked Work

| Phase | Status | Notes |
| --- | --- | --- |
| Phase 0 — Positioning and architecture | Complete / locked | Existing docs are under `docs/phase0/`. Do not reopen unless a factual claim must be re-verified. |
| Phase 1 — Data-source audit | Complete / locked | Dataset audit artifacts are under `research/dataset_audits/`. MVP 1 uses synthetic telemetry. |

## Active Work

| Phase | Status | Deliverables |
| --- | --- | --- |
| Phase 2 — Synthetic trace generation | Complete | `src/agentsentinel/data/synth_traces.py`, `data/synthetic/normal/`, `data/synthetic/anomalous/`, Phase 2 tests |
| Phase 3 — Trace schema | Complete | `src/agentsentinel/schema/trace.py`, `src/agentsentinel/schema/redaction.py`, `tests/unit/test_trace_schema.py`, `docs/design_decisions.md` |
| Phase 4 — Instrumentation / collection | Complete | `src/agentsentinel/observability/collector.py`, `tests/integration/test_collector.py`, local JSONL sink under `data/collected/` |

## Latest Checkpoint

Phase 4 is complete.

Actual output:

- Git prerequisite completed with catch-up commit `be2ede6`.
- `TraceCollector` validates, redacts, and writes accepted traces to a local JSONL sink.
- Malformed inputs are rejected with documented reasons and sanitized rejection logs.
- Tests pass: 11 passed.

## MVP 1 Scope

Included now:

- One customer-support-style agent.
- Three tools: `search_customer`, `get_order`, `update_order`.
- Normal synthetic traces.
- Three injected anomaly types: `tool_loop`, `retry_storm`, `resource_spike`.
- Clear synthetic-data labeling and limitations.
- Reproducible generation using a fixed seed.

Explicitly postponed:

- Pydantic trace schema.
- Feature engineering.
- Rules and Isolation Forest.
- Autoencoder and sequence model.
- FastAPI, LangGraph, MCP, OpenTelemetry, Postgres, Redis, Kafka, dashboard, deployment.

## Gate A Target

Phase 2 contributes the data needed for Gate A:

- At least 500 normal synthetic traces.
- At least 100 labeled abnormal synthetic traces.
- Synthetic data limitations documented with the generated data.

## Phase 2 Checkpoint Criteria

- [x] Synthetic trace generator runs from the command line.
- [x] Normal and anomalous datasets are written under `data/synthetic/`.
- [x] Every trace includes `data_type: synthetic`.
- [x] Every trace includes a limitations field.
- [x] Anomalous traces are labeled with one of the three injected anomaly types.
- [x] Generation is deterministic for the same seed and inputs.
- [x] Tests pass.

## Phase 3 Checkpoint Criteria

- [x] Trace schema defined in Pydantic with the required fields.
- [x] Redaction/hash utility implemented and tested.
- [x] Every Phase 2 synthetic trace validates against the schema with zero data loss.
- [x] No raw secret/credential field exists anywhere in the schema.
- [x] `data_type` and `limitations` fields are preserved from Phase 2 output.
- [x] Tests pass.
- [x] `docs/design_decisions.md` updated.

## Phase 4 Checkpoint Criteria

- [x] Git repository initialized, `.gitignore` in place, Phase 0-3 catch-up commit made and confirmed via `git log`.
- [x] `TraceCollector` implemented; validates, redacts, and writes to `data/collected/`.
- [x] Malformed-input behavior implemented and documented.
- [x] Integration test harness runs normal, anomalous, and malformed events through the collector.
- [x] No unredacted secret-shaped field reaches the JSONL sink.
- [x] Tests pass.
- [x] `docs/design_decisions.md` updated.
- [x] New commit made for Phase 4 work specifically.
