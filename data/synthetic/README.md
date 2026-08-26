# AgentSentinel Synthetic MVP 1 Data

DATA TYPE: SYNTHETIC

Dataset version: synthetic-mvp1-v1
Seed: 20260818
Normal traces: 500
Anomalous traces: 120
Anomaly types: tool_loop, retry_storm, resource_spike

## Limitations

Synthetic telemetry generated for MVP 1; does not represent all real production behavior, may encode generator assumptions, and may make injected anomalies easier to detect than real anomalies.

## Files

- `normal/traces.jsonl`: normal customer-support tool-call trajectories.
- `anomalous/traces.jsonl`: labeled injected anomalies.
- `metadata.json`: generation parameters and dataset metadata.
