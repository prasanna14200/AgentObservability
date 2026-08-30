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
data/features/v2/features.parquet.

## Label Source
Synthetic labels from Phase 2 generator (tool_loop, retry_storm,
resource_spike, normal). Not used for training — rules have no fitting step.

## Features
Uses all 13 pruned features from data/features/v2/:
- task_retry_zscore (for retry_storm detection)
- tool_repeat_in_session (for loop detection)
- task_token_zscore, task_cost_zscore (for resource_spike detection)
- Remaining features used for context but not as direct thresholds.

## Threshold Selection Method
Domain-knowledge thresholds:
- tool_repeat_in_session >= 3 for loop flag
- task_retry_zscore >= 2.0 for retry_storm flag
- task_token_zscore >= 2.5 OR task_cost_zscore >= 2.5 for resource_spike flag

## Known Failure Modes
- Low recall on sequential anomalies (tool_loop, retry_storm) because
  per-event thresholds don't capture cross-event trajectory patterns.
- May miss anomalies where individual events don't exceed thresholds but
  the sequence as a whole is anomalous.

## FP/FN Behavior
- FP: Legitimate multi-tool workflows may trigger loop or resource flags.
- FN: Slow-burn anomalies (gradual latency increase) may not exceed
  z-score thresholds on any single event.

## Latency/Hardware
- Inference: O(1) per event, no model loading required.
- Hardware: CPU-only, no GPU needed.

## Retraining Conditions
N/A — deterministic rules do not retrain. Thresholds may be revisited
if new anomaly types are added in future deliverables.

## Drift Assumptions
- Task-type z-score baselines assume the training distribution represents
  normal operation. Significant task-type drift may require baseline updates.
