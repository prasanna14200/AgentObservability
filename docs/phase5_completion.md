# Phase 5 Completion — Preprocessing

## TASK

Transform raw validated Trace records (from Phase 2 synthetic data and Phase 4's collector output) into a clean, analysis-ready tabular form. Do not engineer features — only clean, normalize, and flatten.

## WHY

Phase 6 (feature engineering) and Phase 7 (EDA) need a single, consistent input table. Raw JSONL traces have nested structures, inconsistent timestamp formats, and nullable fields that must be handled uniformly before any feature computation.

## INPUT

- `data/synthetic/normal/traces.jsonl` (500 traces)
- `data/synthetic/anomalous/traces.jsonl` (120 traces)
- `data/collected/normal/traces.jsonl`
- `data/collected/anomalous/traces.jsonl`
- Phase 4 `TraceCollector` output (schema-valid records only)
- Phase 5 `preprocess.py` module

## COMMAND

```powershell
$env:PYTHONPATH = "src"
pytest
```

## CODE

- `src/agentsentinel/data/preprocess.py`
- `tests/unit/test_preprocess.py`
- `docs/design_decisions.md`
- `docs/phase5_completion.md`

## EXPECTED OUTPUT

Expected:

- `data/processed/v1/traces.parquet` with flat, typed columns.
- `data/processed/v1/manifest.json` recording provenance, row counts, null-handling, schema version, and timestamp.
- `source` column preserved (synthetic vs collected).
- Missing `label`/`anomaly_type` filled with `__UNKNOWN__` sentinel, not dropped.
- Deduplication by `trace_id` + `event_id`.
- Numeric fields coerced to proper dtypes.
- Rejection log (`data/collected/rejected/rejections.jsonl`) excluded from processed output.

## VALIDATION

Actual:

- `pytest`: 20 passed.
- `data/processed/v1/traces.parquet` shape: 1823 rows × 24 columns.
- `manifest.json` contains all five required fields: `input_sources`, `row_counts`, `null_handling`, `schema_version`, `generated_at`.
- Rejection records are excluded from the processed dataset; the rejection log remains the audit trail only.
- Provenance preserved: `source` column distinguishes synthetic from collected traces.

## COMMON ERRORS

- Silently merging synthetic and collected data without provenance.
- Dropping rows with missing `label`/`anomaly_type` instead of filling sentinel.
- Performing feature engineering (derived ratios, baselines) in preprocessing.
- Including rejected, schema-invalid records in the modeling dataset.

## WHY THE ERROR HAPPENS

Preprocessing is often confused with feature engineering. It's tempting to compute "calls per minute" or "latency z-score" while flattening, but that mixes data cleaning with domain logic. Similarly, rejected records are sanitized but still invalid — including them in the feature matrix pollutes baselines.

## FIX

`preprocess.py` loads only schema-valid JSONL (synthetic + collected accepted traces), flattens nested events into rows, coerces types, deduplicates, fills missing nullable fields with `__UNKNOWN__`, and writes a versioned parquet + manifest. Rejection log entries are never loaded into the DataFrame.

## CHECKPOINT

Phase 5 is complete after the Phase 5-specific Git commit is made. Do not begin Phase 6 until both pre-Phase 6 confirmations are resolved and reported.

## Design Decisions

- Keep preprocessing separate from feature engineering.
- Use parquet for versioned output to preserve schema and dtype.
- Fill missing labels with `__UNKNOWN__` rather than dropping rows.
- Exclude rejection log from processed dataset — it is an audit trail, not a modeling input.

## Alternatives Considered

- CSV output: rejected because parquet preserves dtypes and compresses better.
- Dropping rows with missing labels: rejected because missing labels are informative for Phase 6.
- Including rejection records with an `is_rejected` flag: rejected per human decision (Option 2) — invalid data must not enter feature baselines.

## Phase 5 Patch — Rejection-Log Exclusion (found during pre-Phase-6 confirmation)

**Found:** `test_rejection_log_payloads_are_included_as_collected_source` asserted that rejected records from `data/collected/rejected/rejections.jsonl` were merged into the processed dataset with `source="collected"`. This meant schema-invalid input could flow into Phase 6 feature engineering and pollute agent/task_type baselines.

**Fix:** Removed `data/collected/rejected/rejections.jsonl` from `INPUT_PATHS` in `preprocess.py`. Rejection records are now excluded entirely from `data/processed/v1/traces.parquet`. The rejection log remains the audit trail. `_flatten_trace` was also hardened to skip non-trace dicts (no `events` key) rather than emitting placeholder rows. The test was renamed to `test_rejection_log_payloads_are_excluded_from_processed_dataset` and now asserts that rejection records produce zero rows in the processed DataFrame.

**Verified:** Full suite: 20 passed. `manifest.json` input_sources no longer lists `data/collected/rejected/rejections.jsonl`.

**Scope:** This patch is committed separately from Phase 6 work.

## Interview-Grade Questions

1. Why is preprocessing separated from feature engineering?
2. Why fill missing `label`/`anomaly_type` with a sentinel instead of dropping rows?
3. Why use parquet instead of CSV for processed output?
4. Why must rejected records be excluded from the modeling dataset?
5. How does the `source` column prevent silent dataset mixing?
6. What happens if a trace has no events after flattening?
