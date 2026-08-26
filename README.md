# AgentSentinel

AgentSentinel is a runtime safety control plane experiment for AI agent tool calls.

Current implementation focus: **Phase 2 only** — reproducible synthetic trace generation for MVP 1.

See [docs/project_status.md](docs/project_status.md) for the phase-by-phase project status.

## Run Phase 2

```powershell
$env:PYTHONPATH = "src"
python -m agentsentinel.data.synth_traces --output data/synthetic --normal-count 500 --anomalous-count 120 --seed 20260818
pytest
```

The generated datasets are synthetic and are labeled as such in both metadata and trace records.
