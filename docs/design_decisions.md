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

## Phase 7 — Exploratory Data Analysis

`notebooks/01_eda.ipynb` explores the Phase 6 feature matrix using only the 18 engineered features as the numeric feature set. Raw/passthrough columns are used for context and grouping only.

Key findings:

- **Class balance:** 1206 normal (66.2%), 617 anomalous (33.8%). All three anomaly types have >= 97 examples. `resource_spike` is the smallest at 97 — sufficient for detection but tight for per-category evaluation.
- **Feature redundancy:** 24 near-duplicate pairs flagged (|r| > 0.85). This is expected with a single-agent dataset: agent-level, task-type-level, and agent+task-type-level baselines collapse to nearly identical values. Additionally, `agent_token_zscore` and `agent_cost_zscore` are perfectly correlated (r=1.0) because cost is a linear function of token usage, and `call_position`/`calls_so_far` are perfectly correlated because both are cumulative event counters.
- **Label leakage:** None detected — `label` and `anomaly_type` are not referenced in `contextual.py`.
- **Per-anomaly separation:** `retry_storm` has distinctly higher `retry_count`-derived z-scores; `resource_spike` has higher `token_usage`/`cost` z-scores. A single "anomalous" bucket would lose this structure.

Implications for Phase 8:

- **Feature pruning decision:** The 18-engineered-feature set has been pruned to exactly 13 features for Phase 8 modeling. Dropped: all 4 agent-level baselines (`agent_latency_zscore`, `agent_retry_zscore`, `agent_token_zscore`, `agent_cost_zscore`) and `calls_so_far` (perfectly correlated with `call_position`).
- **Agent-level baseline limitation (stated, not bug):** The agent-level features (`agent_*_zscore`) are a placeholder for Deliverable 3+ multi-agent data. Under MVP 1's single-agent scope, they cannot demonstrate cross-agent contextuality — there is no second agent to contrast against. They are redundant with the finer-grained agent+task-type and task-type baselines in this dataset. A hiring panel should understand that these features exist in the schema/design to support the eventual multi-agent case, but are not meaningfully exercised by MVP 1's single-agent data.
- **Gate C pre-registered:** See `docs/gate_c_preregistration.md` for the pre-registered target metric (PR-AUC primary, F1 secondary) and FP ceiling (<= 10%). This was committed before any Isolation Forest training.

## Phase 2 Bug Fix — Temporal Split Artifact (2026-01-30)

**What the bug was:** Phase 2's synthetic trace generator (`src/agentsentinel/data/synth_traces.py`) generated all 500 normal traces first, then all 120 anomalous traces. Both groups computed `start = base_time + timedelta(minutes=trace_index * 3)`, so normal traces got trace_index 0-499 and anomalous traces got trace_index 500-619. Result: all normal traces had earlier timestamps than all anomalous traces — there was zero temporal overlap between classes.

**Why it happened:** Class-batched generation with sequential `trace_index` assignment. The generator iterated `for i in range(normal_count)` then `for i in range(anomalous_count)`, appending to separate lists. Timestamps were derived from the loop index, not from a shuffled global order.

**Impact on Phase 8:** When a temporal split (80/20 by timestamp) was applied, the first 80% of traces were ALL normal (496 traces, 0 anomalous), and the last 20% were ALL anomalous (120 anomalous, 4 normal). This made the split trivial: the model learned "everything before cutoff is normal, everything after is anomalous." The resulting FPR calculation was meaningless (6 normal events out of 9 total normal test events → 66.7% FPR, not a real model failure).

**The fix:** Regenerated Phase 2 synthetic data (dataset version `synthetic-mvp1-v2`) with interleaved timestamps. The generator now:
1. Creates all 620 trace assignments (type, anomaly_type) first
2. Shuffles each class independently using the fixed seed
3. Interleaves in round-robin fashion: normal[0], anomalous[0], normal[1], anomalous[1], etc.
4. Assigns `start = base_time + timedelta(minutes=shuffled_index * 3)` in this interleaved order

This preserves determinism (same seed → same interleaving every run) and keeps internal event timing within traces untouched. Only trace-level start-time ordering changes.

**Version mapping:**
- `data/synthetic/` → v2 (`synthetic-mvp1-v2`) — **current**
- `data/processed/v2/` → derived from synthetic v2 — **current**
- `data/features/v3/` → derived from processed v2 — **current**
- `data/processed/v1/`, `data/features/v1/`, `data/features/v2/` → **deprecated**, derived from old synthetic v1 with the timestamp bug

**Invalidated Phase 8 results:** The earlier Phase 8 report (PR-AUC 0.967/0.991, FPR 66.7%/0%, naive-pooled-hypothesis finding) was based on the buggy temporal split and is INVALIDATED. The corrected Phase 8 evaluation using the interleaved data reports: PR-AUC 0.195 (IF) / 0.574 (rules), FPR 61.8% (IF) / 0.0% (rules), Gate C FAIL. See `notebooks/02_classical_ml.ipynb` and `models/model_cards/` for the corrected results.

**Lesson for hiring panel:** This is an example of why data-generation artifacts can silently invalidate downstream evaluation. The split looked reasonable on the surface (80/20, temporal ordering preserved) but the data-generation order created a perfect class-separating artifact. Catching this required inspecting the generator code, not just the split logic.
