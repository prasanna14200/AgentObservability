# Gate C Pre-Registration

## Target Metric
PR-AUC (primary)
F1 at a fixed operating threshold (secondary, reported for interpretability but not gating)

## FP Ceiling
<= 10% false-positive rate on the held-out test set

## Rationale
Anomaly prevalence is ~19% at trace level (120/620) and ~34% at event level (617/1823). PR-AUC is robust to this imbalance where accuracy would be misleading. The FP ceiling reflects that false alerts erode operator trust in a safety system regardless of training outcome.

## Split Protocol
Temporal split by timestamp: earliest 80% of traces by start time = train, latest 20% = test. Cutoff timestamp: 2026-01-02T09:48:00+00:00. Train: 496 traces (97 anomalous, 399 normal). Test: 124 traces (23 anomalous, 101 normal). No trace_id appears in both splits. Note: an earlier Phase 2 data-generation bug caused all normal traces to precede all anomalous traces in timestamp, making the original split trivially class-separated. The bug was fixed by interleaving timestamps; this split uses the corrected data (synthetic-mvp1-v2).

## Feature Set
13 features (pruned from 18):
- 4 agent+task-type-level z-scores: agent_task_latency_zscore, agent_task_retry_zscore, agent_task_token_zscore, agent_task_cost_zscore
- 4 task-type-level z-scores: task_latency_zscore, task_retry_zscore, task_token_zscore, task_cost_zscore
- 5 trajectory/permission features: call_position, time_since_last_call_ms, tool_repeat_in_session, permission_match, trace_event_count

Dropped: all 4 agent-level baselines (agent_*_zscore) and calls_so_far (perfectly correlated with call_position). Agent-level baselines are documented as MVP 1 placeholders for Deliverable 3+ multi-agent data; they cannot demonstrate cross-agent contextuality under single-agent scope.

## Reproducibility
- Random seed: 42 (scikit-learn IsolationForest)
- Dataset version: data/features/v3/features.parquet (synthetic-mvp1-v2)
- scikit-learn version: recorded at training time
- Hardware: recorded at training time

## Date Pre-Registered
2026-01-30

## Registered By
Human review (see conversation record), implemented by coding agent per instruction.
