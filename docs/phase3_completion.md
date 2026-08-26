# Phase 3 Completion — Trace Schema

## TASK

Define the canonical Pydantic trace schema for AgentSentinel and validate every Phase 2 synthetic trace against it.

## WHY

Later phases need one trusted contract for feature engineering, detectors, risk scoring, policy decisions, and audit replay. Privacy rules also need to begin at ingestion, not after data has already moved through the system.

## INPUT

- `data/synthetic/normal/traces.jsonl`
- `data/synthetic/anomalous/traces.jsonl`
- Phase 2 trace fields from `src/agentsentinel/data/synth_traces.py`

## COMMAND

```powershell
$env:PYTHONPATH = "src"
pytest
```

## CODE

- `src/agentsentinel/schema/trace.py`
- `src/agentsentinel/schema/redaction.py`
- `tests/unit/test_trace_schema.py`
- `docs/design_decisions.md`

## EXPECTED OUTPUT

Expected:

- Pydantic models validate all Phase 2 synthetic traces.
- Audit metadata is separated from tool-call content.
- Redaction/hash mechanism exists for sensitive tool arguments.
- `data_type` and `limitations` survive validation.
- No existing Phase 2 field is silently dropped.

## VALIDATION

Actual:

- 620 Phase 2 traces validated.
- The schema reconstructs each Phase 2 JSON record exactly after validation.
- Redaction utility hashes sensitive keys while preserving argument shape.
- `pytest`: 9 passed.

## COMMON ERRORS

- Missing Pydantic dependency.
- Accidentally dropping Phase 2 fields while reshaping flat records into canonical nested models.
- Treating synthetic labels as mandatory for real runtime traces.
- Redacting too late in the pipeline.

## WHY THE ERROR HAPPENS

Schema boundaries tend to drift when generator output and canonical runtime models are designed separately. Synthetic traces also carry evaluation-only fields such as `label` and `anomaly_type`, while real traces need those fields to remain nullable.

## FIX

The schema accepts the Phase 2 flat JSONL shape through Pydantic `model_validator` adapters, stores audit metadata separately from content, and provides `to_phase2_record()` for zero-loss validation. Redaction is applied inside `ToolArguments`, before downstream consumers touch argument values.

## CHECKPOINT

Phase 3 is complete. Do not begin Phase 4 until explicitly confirmed.

## Design Decisions

- Keep the external Phase 2 JSONL files unchanged.
- Use canonical nested models internally: trace audit metadata, event audit metadata, tool content, resource usage, and redacted arguments.
- Allow `label`, `anomaly_type`, and `policy_version` to be nullable.
- Preserve `data_type` and `limitations` as first-class audit metadata.

## Alternatives Considered

- Flat Pydantic models only: easier, but weaker separation between audit metadata and content.
- Rejecting sensitive tool arguments outright: safer for storage, but less useful for policy/audit paths that need to know which argument keys were attempted.
- Hashing all argument values: simpler, but would destroy useful non-sensitive metadata for later feature engineering.

## Interview-Grade Questions

1. Why does the schema separate audit metadata from tool-call content?
2. Why are synthetic labels nullable instead of required for every trace?
3. Why is redaction implemented in the schema layer rather than preprocessing?
4. What does zero data loss mean in this phase, and how is it tested?
5. Why preserve sensitive argument key names while hashing their values?
6. Why is `policy_version` present but nullable in Phase 3?
7. What later bugs would `extra="forbid"` catch?
8. How could over-aggressive redaction hurt anomaly detection?
