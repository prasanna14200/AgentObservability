# AgentSentinel

AgentSentinel is an **offline AI-agent observability and anomaly-detection prototype**. It currently generates labeled synthetic customer-support tool-call traces, validates and redacts trace records, writes accepted/rejected local JSONL, preprocesses events, builds contextual features, and evaluates deterministic anomaly rules against a scikit-learn Isolation Forest.

The planned PayPal Sandbox purchase assistant is **not implemented**. There is no payment tool, live interception gateway, authorization policy, human review, or dashboard. Detector scores are advisory and must not authorize payments. The latest corrected synthetic evaluation still **fails Gate C** because Isolation Forest's held-out false-positive rate is 56.9%, above the registered 10% ceiling. See [Stage 2 verification](docs/paypal_hackathon/02_pipeline_verification.md).

## Run the offline pipeline (PowerShell)

Use Python 3.10 or newer. From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
$env:PYTHONPATH = "src"
python scripts/evaluate_offline.py
python -m pytest -o addopts='' -q
```

`scripts/evaluate_offline.py` regenerates synthetic v3 traces (seed `20260818`), processed v4 events, and v4 features; trains Isolation Forest on normal training traces (seed `42`); and prints held-out metrics. It calls no external payment service. `pyproject.toml` is the dependency declaration; `requirements.txt` is an empty historical file. Generated `data/processed/` files are ignored. Historical synthetic data, feature artifacts, notebooks, research, and model cards remain in the repository.

## Current architecture

```text
synthetic traces / local collector
          -> Pydantic schema + key-based redaction
          -> preprocessing -> chronological split
          -> baselines fitted on normal training rows
          -> rules + Isolation Forest -> offline evaluation
```

The collector records what happened; it does not intercept or authorize tool calls. The permission-match feature is an offline diagnostic inferred from training traces, not a trusted permission policy. Redaction is key-based and does not make the current trace store suitable for payment credentials.

## Hackathon work and provenance

- [Hackathon scope and verified rules](docs/hackathon_scope.md)
- [12-stage progress](docs/paypal_hackathon/PROGRESS.md)
- [Stage 1 audit](docs/paypal_hackathon/01_repository_audit.md) and [Stage 2 verification](docs/paypal_hackathon/02_pipeline_verification.md)
- [Gate C preregistration](docs/gate_c_preregistration.md), [model cards](models/model_cards/), and [source registry](research/sources.yaml)

The intended hackathon addition is a protected PayPal **Sandbox** purchase flow. It will keep payment authorization separate from telemetry and anomaly scores. Do not place sandbox credentials in Git, issue trackers, chat, traces, or screenshots. `.env.example` contains placeholders only; copy it to an ignored `.env` when later stages need local credentials.

## License

Apache-2.0. See [LICENSE](LICENSE).
