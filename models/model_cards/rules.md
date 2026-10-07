# Model Card: Rules Engine

## Intended Use
Deterministic anomaly detection for customer-support agent tool-call traces.
Flags three anomaly types: tool_loop, retry_storm, resource_spike.

## Prohibited Use
- Not a substitute for human review of security incidents.
- Not trained on real customer data; synthetic MVP 1 data only.
- Not a risk scoring or policy enforcement system.

## Training Data
No training required. Rules are deterministic thresholds applied to
data/features/v3/features.parquet (pruned 13-feature set, dataset version
synthetic-mvp1-v2 with interleaved timestamps).

## Label Source
Synthetic labels from Phase 2 generator (tool_loop, retry_storm,
resource_spike, normal). Not used for training — rules have no fitting step.

## Features
Uses all 13 pruned features from data/features/v3/:
- task_retry_zscore (for retry_storm detection)
- tool_repeat_in_session (for loop detection)
- task_token_zscore, task_cost_zscore (for resource_spike detection)
- Remaining features used for context but not as direct thresholds.

Dropped features: agent_*_zscore (4 agent-level baselines, MVP 1 placeholders),
calls_so_far (perfectly correlated with call_position).

## Threshold Selection Method
Domain-knowledge thresholds:
- tool_repeat_in_session >= 3 for loop flag
- task_retry_zscore >= 2.0 for retry_storm flag
- task_token_zscore >= 2.5 OR task_cost_zscore >= 2.5 for resource_spike flag

## Known Failure Modes
- Low recall on sequential anomalies (tool_loop, retry_storm) because
  per-event thresholds don't capture cross-event trajectory patterns.
  Actual recall on held-out test set: 0.394 overall, 0.250 (tool_loop),
  0.600 (retry_storm), 0.412 (resource_spike).
- May miss anomalies where individual events don't exceed thresholds but
  the sequence as a whole is anomalous.
- Zero false positives on held-out test set (FPR 0.0%), but this comes at
  the cost of low recall.

## FP/FN Behavior
- FP: Legitimate multi-tool workflows may trigger loop or resource flags.
  Actual FP on held-out test set: 0 (FPR 0.0%).
- FN: Slow-burn anomalies (gradual latency increase) may not exceed
  z-score thresholds on any single event. Actual FN on held-out test set:
  63/104 anomalous events (recall 0.394).

## Performance (Held-Out Test Set)
- Precision: 1.000
- Recall: 0.394
- F1: 0.566
- PR-AUC: 0.574
- AUROC: 0.697
- FPR: 0.000
- TP: 41, FP: 0, TN: 246, FN: 63
- Per-type recall: tool_loop 0.250, retry_storm 0.600, resource_spike 0.412

## Latency/Hardware
- Inference: O(1) per event, no model loading required.
- Hardware: CPU-only, no GPU needed.

## Retraining Conditions
N/A — deterministic rules do not retrain. Thresholds may be revisited
if new anomaly types are added in future deliverables.

## Drift Assumptions
- Task-type z-score baselines assume the training distribution represents
  normal operation. Significant task-type drift may require baseline updates.
