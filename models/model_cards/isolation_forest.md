# Model Card: Isolation Forest

## Intended Use
Unsupervised anomaly detection for customer-support agent tool-call traces.
Detects anomalous events without requiring labeled anomaly data for training.

## Prohibited Use
- Not a substitute for human review of security incidents.
- Not trained on real customer data; synthetic MVP 1 data only.
- Not a risk scoring or policy enforcement system.
- Not validated for deployment in production without further evaluation
  on real-world data.

## Training Data
Trained on normal-labeled traces from the temporal train split:
- 496 traces, 0 anomalous traces
- All events from normal traces in the earliest 80% of the dataset by timestamp
- Data source: data/features/v2/features.parquet (pruned 13-feature set)

## Label Source
Synthetic labels from Phase 2 generator. Anomalous traces were EXCLUDED
from training per the Master Prompt protocol.

## Features
13 pruned features from data/features/v2/:
- 4 agent+task-type-level z-scores: agent_task_latency_zscore,
  agent_task_retry_zscore, agent_task_token_zscore, agent_task_cost_zscore
- 4 task-type-level z-scores: task_latency_zscore, task_retry_zscore,
  task_token_zscore, task_cost_zscore
- 5 trajectory/permission features: call_position, time_since_last_call_ms,
  tool_repeat_in_session, permission_match, trace_event_count

Dropped features: agent_*_zscore (4 agent-level baselines, MVP 1 placeholders),
calls_so_far (perfectly correlated with call_position).

## Threshold Selection Method
Unsupervised — Isolation Forest uses decision_function threshold internally.
No explicit threshold tuning on validation data. Gate C evaluation uses
default scikit-learn behavior with contamination="auto".

## Known Failure Modes
- May underperform on sequential anomalies (tool_loop, retry_storm) because
  the naive pooled-feature approach treats each event independently and
  loses cross-event trajectory context. This is the documented Trajectory
  Guard finding being tested in this project.
- Single-agent scope limits agent-level baseline features; agent_*_zscore
  features are redundant under MVP 1 and were pruned.

## FP/FN Behavior
- FP: Normal events with unusual but legitimate resource usage may be flagged.
- FN: Subtle anomalies that don't create sufficiently isolated feature
  vectors may be missed.

## Latency/Hardware
- Inference: O(1) per event with fitted model.
- Hardware: CPU-only, no GPU required.
- Model size: ~200 trees, ~few MB.

## Retraining Conditions
Retrain when:
- New task types are added (task-type baselines must be recomputed).
- Multi-agent data becomes available (agent-level baselines can be restored).
- Feature set changes (Phase 11 sequence-aware modeling).

## Drift Assumptions
- Assumes temporal stationarity within the train window.
- Assumes task-type distributions in test are similar to train.
- Significant drift in agent behavior or task mix may degrade performance.
