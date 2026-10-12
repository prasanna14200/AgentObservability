# Stage 2 — Offline pipeline verification and correction

Completed: 2026-10-12. This stage covers the existing synthetic customer-support pipeline only. No payment schema, PayPal API, policy engine, live interception, or dashboard was added.

## Input, processing, output

- **Input:** historical v2 synthetic JSONL and v3 feature artifact, existing generator, schema, collector, preprocessing, features, rules, Isolation Forest, notebooks, model cards, and Gate C preregistration.
- **Processing:** reproduce the old evaluation; correct synthetic time placement and train/test feature leakage; orient anomaly scores consistently; rerun the unchanged detectors and thresholds; check redaction and repeatability.
- **Output:** corrected synthetic dataset `data/synthetic/v3/`, processed v4 (regenerated, ignored), tracked `data/features/v4/`, corrected evaluation script, focused regression tests, and this report. Historical datasets, v2/v3 feature artifacts, notebook findings, and model-card results remain available.

## Root causes and corrections

1. **Test-window and baseline leakage.** The generator's former round-robin loop emitted every anomalous trace during the first 240 of 620 time positions. Regenerating the tracked v2 JSONL from the source code produced a final 20% window with **zero anomalous traces**, despite its metadata naming v2. The locally saved processed v2 / features v3 artifacts had different timestamps and 23 anomalous test traces, so the old evaluation could not be reproduced from the tracked source files. The generator now uses deterministic, seeded positions spread through the full time window and writes a new `synthetic-mvp1-v3` dataset. The original v2 files are retained.
2. **Feature leakage.** `build_features()` previously computed grouped means and standard deviations, and inferred tool permissions, from the entire dataset before `temporal_split()`. Test and future events influenced the features used to train and test the model. It now splits raw processed rows by trace start time first, fits statistics and the offline permission map on **normal training rows only**, then applies the same frozen values to train and test. The registered 80/20 chronological trace split and 13 selected detector features are unchanged. There is no separate validation partition in the preregistered protocol; `transform_with_baselines()` also accepts validation rows without refitting if a future experiment adds one.
3. **Reversed ranking score.** In scikit-learn Isolation Forest, lower `score_samples` and lower `decision_function = score_samples - offset_` mean more anomalous; `predict()` returns `-1` when `decision_function < 0`. The old notebook passed `decision_function` directly to positive-class average precision and AUROC, reversing the ranking. `iso_anomaly_score = -decision_function` now makes higher mean more anomalous, and the notebook/evaluation use that field. The built-in `predict()` threshold and all rules thresholds remain unchanged.

The existing `permission_match` feature remains an **offline diagnostic**. Its map is now fitted on normal training rows only, and unknown tools yield `False`. Because that map is inferred from observed claims, it must never authorize a tool or payment. A trusted permission source belongs to later policy work.

## Evaluation protocol and results

Command from the repository root:

```powershell
$env:PYTHONPATH = "src"
python scripts/evaluate_offline.py
python -m pytest -o addopts='' -q
```

The script regenerates v3 synthetic traces with seed `20260818`, preprocesses those traces into v4, builds features after the trace-level chronological split, trains Isolation Forest only on normal training events with seed `42`, and scores the held-out test set. Current local versions: Python 3.10.11, pandas 2.3.3, scikit-learn 1.7.2, pyarrow 23.0.1. The current dataset has **620 traces / 1,831 events**. Cutoff: `2026-01-02T09:48:00+00:00`; training has 496 traces (395 normal, 101 anomalous) and 1,480 events; test has 124 traces (105 normal, 19 anomalous) and 351 events (262 normal, 89 anomalous). Baselines fit on the 953 normal training events. “PR-AUC” below is `average_precision_score`, matching the historical notebook convention.

| Evaluation | Detector | PR-AUC | AUROC | Precision | Recall | F1 | FPR | TP / FP / TN / FN |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Historical v3 features, original score direction | Rules | 0.574 | 0.697 | 1.000 | 0.394 | 0.566 | 0.000 | 41 / 0 / 246 / 63 |
| Historical v3 features, original score direction | Isolation Forest | 0.195 | 0.205 | 0.364 | 0.837 | 0.507 | 0.618 | 87 / 152 / 94 / 17 |
| Historical v3 features, score direction only corrected | Isolation Forest | 0.739 | 0.795 | 0.364 | 0.837 | 0.507 | 0.618 | 87 / 152 / 94 / 17 |
| **Corrected v4 features and v3 source data** | **Rules** | **0.463** | **0.640** | **1.000** | **0.281** | **0.439** | **0.000** | **25 / 0 / 262 / 64** |
| **Corrected v4 features and v3 source data** | **Isolation Forest** | **0.706** | **0.802** | **0.335** | **0.843** | **0.479** | **0.569** | **75 / 149 / 113 / 14** |

The first three rows use the **old** saved feature artifact (350 test events, 104 anomalous) and are retained for comparison. The last two rows use a **new, reproducible** dataset and leakage-free features (351 test events, 89 anomalous). Their differences cannot be attributed solely to one fix. All rows were recomputed locally; the notebooks have no saved execution outputs.

**Gate C: FAIL.** The preregistered learned-detector requirement is to beat the rule-only PR-AUC while staying at or below **10% FPR**. Corrected Isolation Forest AP is 0.706 versus rules 0.463, but FPR is **56.9%**, well above the ceiling. The prior notebook also described a >0.5 AP target; the corrected score clears it. No threshold was tuned after viewing test results. A passing ranking score does not make the current operating threshold deployable.

## Evidence of leakage prevention and repeatability

- `test_build_features_is_invariant_to_future_metric_changes` changes a later test trace's latency from 50 to 100,000 while keeping earlier training rows fixed. All checked training z-scores and permission flags stay identical; the later trace's score changes.
- `test_future_rows_cannot_change_frozen_training_features` tests the frozen transformer on unseen rows. `test_temporal_split_produces_disjoint_trace_ids` and the new chronological-window test confirm trace separation and both labels in the test window.
- The score regression test verifies `decision_function = score_samples - offset_`, `iso_anomaly_score = -decision_function`, and that flagged events rank higher on the anomaly score.
- Two consecutive script runs produced identical detector metrics and identical SHA-256 hashes for normal JSONL (`92e7004b5e76d9894a25d883b12fae293abb9f70ef33d4961866c3bd7be8b965`), anomalous JSONL (`f2777e90c6c0de6af6cfa9b468f4cb880b50ae74cc280508585eb1c37f14953a`), and v4 feature parquet (`c42ffb5030648f59f9f63d6bad394147e8edf8911e0b5211ae568598683e4ede`). Generated metadata timestamps vary by run; data content and metrics did not.
- **Full suite:** `49 passed in 2.90s`, 0 failed on the final run. The previous Stage 1 baseline was 41 passed. This verifies local behavior on the installed environment, not a fresh installation or production use.

## Privacy review

The schema still rejects unexpected structural fields and redacts recognized secret-shaped tool arguments. Collector tests cover accepted and rejected paths. Stage 2 verified and fixed two concrete paths: `Authorization` headers are now redacted, and malformed non-mapping payloads no longer write their raw `repr` to the rejection log. Preprocessing also re-redacts tool arguments before writing parquet, closing a direct-file path that bypassed the collector. Regression tests cover these paths.

Remaining limits: redaction is key-name based, so unknown keys or a secret embedded in a nominally safe value can still reach stored telemetry. The hash uses a fixed public salt and is unsuitable for protecting guessable secrets. Rejection logs still retain sanitized payload fields, and source JSONL is assumed trusted enough to parse. Payment work needs an allowlisted telemetry model that never stores raw payment tokens, plus separate trusted authorization records. These are prerequisites for Stage 3 and later security stages, not claims that current logs are safe for payment data.

## Exact files changed

- Pipeline and evaluation: `src/agentsentinel/data/synth_traces.py`, `src/agentsentinel/data/preprocess.py`, `src/agentsentinel/data/split.py`, `src/agentsentinel/features/contextual.py`, `src/agentsentinel/detectors/isoforest.py`, `scripts/evaluate_offline.py`, `notebooks/02_classical_ml.ipynb`.
- Privacy: `src/agentsentinel/schema/redaction.py`, `src/agentsentinel/observability/collector.py`.
- Tests: `tests/test_synth_traces.py`, `tests/unit/test_preprocess.py`, `tests/unit/test_contextual_features.py`, `tests/unit/test_detectors.py`, `tests/unit/test_trace_schema.py`, `tests/integration/test_collector.py`.
- Provenance/setup: `data/synthetic/v3/`, `data/features/v4/`, `.gitignore`, `pyproject.toml`, `models/model_cards/isolation_forest.md`, `models/model_cards/rules.md`, this report, `docs/paypal_hackathon/PROGRESS.md`.

## Remaining limitations and Stage 3 prerequisites

The detectors are evaluated on one synthetic support agent and three injected anomaly classes. There is no real-traffic validation, no separate preregistered validation set, no saved fitted model, and no live inference contract. `trace_event_count` describes the completed trace and cannot be used at the first tool call in a future online gateway. Rules have low recall; Isolation Forest fails Gate C. Do not use either as a payment authorization decision.

Stage 3 can now extend the trace schema while preserving support traces. It must define payment intent and authorization scope separately from telemetry, enforce narrow field allowlists for sensitive payment metadata, and add schema tests. It must not assume that this offline collector or detector is a protected payment gateway. Stage 2 is complete; Stage 3 awaits approval.
