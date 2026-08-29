# Phase 4 Completion — Instrumentation / Collection

## TASK

Build a local trace collector that validates, redacts, and persists trace payloads using the Phase 3 schema.

## WHY

AgentSentinel needs one reusable capture component before later runtime phases add FastAPI, policy decisions, OpenTelemetry, or database persistence. Phase 4 proves the collector can accept valid trace payloads, preserve provenance, reject malformed input, and keep raw secrets out of local sinks.

## INPUT

- Phase 2 normal synthetic traces.
- Phase 2 anomalous synthetic traces.
- Intentionally malformed test payloads.
- Phase 3 `TraceRecord` schema and redaction utilities.

## COMMAND

```powershell
$env:PYTHONPATH = "src"
pytest
```

## CODE

- `src/agentsentinel/observability/collector.py`
- `tests/integration/test_collector.py`
- `docs/design_decisions.md`
- `docs/project_status.md`

## EXPECTED OUTPUT

Expected:

- Valid normal and anomalous traces produce `TraceRecord` objects.
- Accepted traces are written to a local JSONL sink under `data/collected/`.
- Malformed traces return `None`, not an exception to the caller.
- Rejections include a validation reason and sanitized payload.
- Raw secret-shaped values do not reach accepted or rejected JSONL sinks.

## VALIDATION

Actual:

- Integration tests captured normal and anomalous Phase 2 traces.
- Integration tests rejected a malformed trace and logged a sanitized rejection.
- Injected raw secret-shaped values were hashed before reaching JSONL.
- `data_type` and `limitations` were preserved end-to-end.
- `pytest`: 11 passed.

## COMMON ERRORS

- Treating malformed input as a policy decision.
- Silently dropping malformed input without a rejection reason.
- Writing raw rejected payloads to logs.
- Mixing collector-produced data with `data/synthetic/`.

## WHY THE ERROR HAPPENS

Collector code often sits close to runtime control flow, so it is tempting to add allow/block semantics too early. Rejection logging can also leak secrets if it records the original malformed payload instead of a sanitized copy.

## FIX

`TraceCollector.capture()` sanitizes a copied payload first, validates it with `TraceRecord`, writes accepted traces to `data/collected/<label>/traces.jsonl`, and writes rejected sanitized payloads to `data/collected/rejected/rejections.jsonl`. It returns `None` for malformed input and does not implement policy behavior.

## CHECKPOINT

Phase 4 is complete after the Phase 4-specific Git commit is made. Do not begin Phase 5 until explicitly confirmed.

## Design Decisions

- Keep collection file-based for MVP 1.
- Use `data/collected/` so collector output never mixes with synthetic source data.
- Reject malformed input with a logged reason instead of partial acceptance.
- Keep policy/fail-open/fail-closed behavior out of this phase.

## Alternatives Considered

- OpenTelemetry now: deferred to Phase 14 because Phase 4 only needs local capture.
- Database writes now: deferred to Phase 20.
- Raising validation exceptions to callers: avoided so the fake agent harness can continue safely during MVP tests.

## Interview-Grade Questions

1. Why should the collector sanitize before schema materialization?
2. Why does Phase 4 return `None` for malformed input instead of deciding fail-open or fail-closed?
3. Why keep `data/collected/` separate from `data/synthetic/`?
4. What audit risks come from partial acceptance of malformed traces?
5. How does this collector prepare for OpenTelemetry without depending on it?
6. What changes when this collector moves into a live request path in Phase 13?
7. Why is rejected-input logging a privacy risk?
8. What would have to change before writing traces to Postgres?
