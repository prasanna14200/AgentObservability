# Phase 2 Completion — Synthetic Trace Generation

## TASK

Build reproducible synthetic telemetry for MVP 1.

## WHY

Phase 1 confirmed that locally available ATBench/ATBench500 data is useful for research context but lacks the operational telemetry AgentSentinel needs for MVP 1: timestamps, latency, token usage, retries, status, tool arguments, and trajectory-level labels.

## INPUT

- One customer-support-style agent.
- Three MVP tools: `search_customer`, `get_order`, `update_order`.
- Three injected anomaly types: `tool_loop`, `retry_storm`, `resource_spike`.
- Seed: `20260818`.

## COMMAND

```powershell
$env:PYTHONPATH = "src"
python -m agentsentinel.data.synth_traces --output data/synthetic --normal-count 500 --anomalous-count 120 --seed 20260818
pytest
```

## CODE

- `src/agentsentinel/data/synth_traces.py`
- `tests/test_synth_traces.py`
- `docs/project_status.md`
- `data/synthetic/README.md`
- `data/synthetic/metadata.json`
- `data/synthetic/normal/traces.jsonl`
- `data/synthetic/anomalous/traces.jsonl`

## EXPECTED OUTPUT

Expected:

- At least 500 normal synthetic traces.
- At least 100 anomalous synthetic traces.
- Synthetic data labeling and limitations.
- Deterministic generation for the same seed.

## VALIDATION

Actual:

- 500 normal synthetic traces generated.
- 120 anomalous synthetic traces generated.
- `pytest`: 4 passed.
- Anomaly labels include `tool_loop`, `retry_storm`, and `resource_spike`.

## COMMON ERRORS

- `ModuleNotFoundError: No module named 'agentsentinel'`
- `ImportError: cannot import name 'UTC' from 'datetime'`

## WHY THE ERROR HAPPENS

The repository uses a `src/` layout, so direct module execution needs `PYTHONPATH=src` unless the package is installed. The local `python` command resolved to Python 3.10, where `datetime.UTC` is unavailable.

## FIX

Use `$env:PYTHONPATH = "src"` before direct module execution. The generator now uses `datetime.timezone.utc`, which works on Python 3.10 while remaining compatible with Python 3.11+.

## CHECKPOINT

Phase 2 is complete. Do not begin Phase 3 until explicitly confirmed.

## Design Decisions

- Use JSONL so each trajectory is easy to stream or load independently in later preprocessing.
- Keep generated records schema-like but avoid enforcing the final Pydantic schema until Phase 3.
- Store hashed synthetic identifiers in tool input metadata to keep privacy expectations visible before the formal schema exists.
- Generate 120 anomalous traces instead of exactly 100 to keep each anomaly type evenly represented.

## Alternatives Considered

- CSV: simpler for spreadsheets, but weaker for nested event trajectories.
- Random split metadata now: postponed because temporal split belongs to preprocessing/evaluation phases.
- Pydantic validation now: postponed because Phase 3 is specifically responsible for the trace schema and redaction rules.

## Interview-Grade Questions

1. Why did we generate trajectory-level JSONL instead of one flat event CSV?
2. Why is synthetic data acceptable for MVP 1 but insufficient for final claims?
3. What leakage risks appear if abnormal traces influence normal training data?
4. Why are `tool_loop`, `retry_storm`, and `resource_spike` operational anomalies rather than content-safety labels?
5. What assumptions does this generator encode about customer-support agents?
6. How could this synthetic data make Isolation Forest look better than it really is?
7. Why should redaction begin at the trace schema layer in Phase 3 rather than downstream preprocessing?
8. Why is deterministic generation important for interviews and evaluation reproducibility?
