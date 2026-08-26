# AgentSentinel Project Status

Status date: 2026-08-18

## Specification Of Record

AgentSentinel is a runtime safety control plane for AI agent tool calls. It observes tool requests, extracts behavioral features, scores behavior using deterministic rules and learned detectors, and applies a separate policy engine that makes the final decision.

Important invariant: anomaly signals are advisory. A deterministic policy engine is the only component that can decide whether to allow, pause, confirm, downgrade, terminate, or block a tool request.

## Current Phase

**Phase 3 — Trace Schema**

Goal: define the canonical Pydantic schema consumed by later feature, detector, risk, policy, and audit phases. Privacy/redaction rules start at schema ingestion.

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

## Latest Checkpoint

Phase 3 is complete.

Actual output:

- Canonical Pydantic trace schema defined.
- Redaction/hash utility implemented.
- All 620 Phase 2 traces validate with zero data loss.
- `data_type` and `limitations` are preserved.
- Tests pass: 9 passed.

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
