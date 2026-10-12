# Stage 1 — Repository audit

Audit date: 2026-10-12. Scope: the local checkout on `main` at `7780861`. This stage changes documentation only. No PayPal call, model retraining, pipeline regeneration, or runtime integration was performed.

## Objective and stage flow

Establish what the existing customer-support observability pipeline actually does before adapting it to a sandbox purchase assistant.

- **Input:** master plan, phase and design documents, research audits, source, tests, notebooks, model cards, and local data artifacts.
- **Processing:** inventory files, compare claims with code and artifacts, run the existing test suite, and make read-only checks of the reported evaluation.
- **Output:** this audit, a Stage 2 work list, and `PROGRESS.md`.

## Baseline and evidence

| Check | Actual result |
| --- | --- |
| Working tree before audit | Clean (`git status --short` empty). |
| Test command | `python -m pytest -o addopts='' -q` with Python 3.10.11 and installed local dependencies. |
| Tests | **41 passed in 2.06s; 0 failed**. Existing tests cover generation, schema and redaction, collector, preprocessing, features, split, rules, and Isolation Forest interface. |
| Data | `data/synthetic/` has 500 normal and 120 anomalous traces, version `synthetic-mvp1-v2`; all 620 are exercised by the schema test. |
| Current feature artifact | Local `data/features/v3/features.parquet`: 1,823 events, 620 traces, all `source=synthetic`; 1,209 normal and 614 anomalous event labels. `data/collected/` is absent locally. |
| Notebooks | Both tracked notebooks have code and narrative, but zero saved execution counts and zero saved outputs. This audit did not execute them. |
| Model artifact | No fitted model file is tracked. The Isolation Forest is trained in memory by tests/notebook code. |

Passing unit tests establish the current offline code baseline. They do not establish live interception, payment safety, or validity of reported detector metrics.

## Original phase status

These are the **original 29-phase master-plan phases**, distinct from the new 12 PayPal stages. “Complete” describes the implemented phase artifact, not production readiness.

| Original phase | Audit status | Evidence and qualification |
| --- | --- | --- |
| 0 — Definition and architecture | Mostly complete | `docs/phase0/` covers boundary, architecture, MVP, risks, and research. Its completion checklist still leaves final current prior-art verification unchecked. |
| 1 — Data-source audit | Complete for local research scope | ATBench/ATBench-Claw/Codex inspection scripts and captured outputs exist under `research/dataset_audits/`; no real operational telemetry is integrated. |
| 2 — Synthetic traces | Complete | Deterministic generator and tracked 500/120 JSONL dataset, seed and limitations metadata, generation tests. Synthetic customer-support behavior only. |
| 3 — Trace schema | Complete for customer-support traces | Pydantic schema, consistency checks, redaction tests, and 620-trace round-trip test. Tool names are restricted to the three existing support tools. |
| 4 — Collection | Complete as local offline collector | `TraceCollector` validates, redacts, writes JSONL, and logs malformed inputs; integration tests pass. No live tool interception or fail-closed gateway. |
| 5 — Preprocessing | Implemented | JSONL flattening, coercion, deduplication, parquet and manifest; unit tests pass. Generated `data/processed/v1` and `v2` are local ignored artifacts, so reproducibility needs checking in Stage 2. |
| 6 — Contextual features | Partially complete | Feature code and parquet artifacts exist; tests pass. Baselines and inferred permission map are fit on the full dataset before the temporal split, so they are unsuitable as prospective evaluation or payment authorization. |
| 7 — EDA | Partially complete | EDA notebook and written findings exist. The notebook has no saved execution output; some documented label counts differ from current v3 data. |
| 8 — Classical ML | Partially complete | Rules and Isolation Forest code, tests, notebook, and model cards exist. The notebook's Isolation Forest ranking metrics use the score in the wrong direction; Gate C remains failed on its FPR ceiling. No durable fitted model or verified runtime scoring path. The broader original-phase LOF/One-Class SVM work is absent, though MVP 1 intentionally scoped to rules and Isolation Forest. |
| 9–29 | Not implemented | No autoencoder, sequence detector, risk/policy engine, live safety path, framework integration, human review, dashboard, database, deployment, or production evaluation appears in this checkout. These were future phases in the master plan. |

The original project is an **offline anomaly-detection prototype**. Its architectural documents describe a future control plane; the runtime safety control plane is not implemented yet. `README.md` still says “Phase 2 only,” while `docs/project_status.md` stops at Phase 4, so neither reflects the current Phase 8 code.

## Reusable components for PayPal

| Component | Reuse | Required adaptation later |
| --- | --- | --- |
| `src/agentsentinel/schema/trace.py` | Trace/event identity, metadata, strict Pydantic validation | Add payment-specific event and authorization models without breaking support traces; keep authority separate from telemetry. |
| `src/agentsentinel/schema/redaction.py` and `observability/collector.py` | Initial argument sanitization and local audit capture | Review sensitive-field coverage and secure storage before payment data; collector is observational, not a safety boundary. |
| `src/agentsentinel/data/synth_traces.py`, preprocessing, contextual features | Reproducible offline pipeline and tests | Add representative payment telemetry and prospective feature computation; keep payment policy independent of anomaly detection. |
| `detectors/rules.py`, `detectors/isoforest.py` | Advisory behavior signals for loops/retries/resource spikes | Fix evaluation and calibrate on relevant data; neither detector decides payment permission. |
| `tests/`, model cards, preregistration | Existing regression and evaluation structure | Add payment-specific schema, policy, gateway, idempotency, and failure tests in their respective stages. |

## Gaps and architectural/security findings

1. **No payment boundary exists.** There is no payment intent, authorization grant, policy engine, operation ID, durable idempotency record, PayPal tool, buyer approval, human review, or dashboard. The agent must never receive PayPal credentials or call the protected tool directly.
2. **Offline features leak future/test information.** `build_features()` computes grouped means/stds and a tool permission map over all rows; `temporal_split()` then splits the already computed feature file. Test records therefore influence their own and train-record baselines. `trace_event_count` also uses the completed trace and is unavailable before an early live tool call. Stage 2 should separate offline evaluation from features available at decision time.
3. **`permission_match` is not an authorization check.** `_build_tool_permissions_map()` derives required permissions from permissions declared on observed events. A new or unobserved tool gets an empty requirement; a self-declared permission can shape the learned map. Payment permissions must come from a trusted policy/configuration store and be checked before execution.
4. **Isolation Forest ranking metrics are reversed.** `predict_isolation_forest()` stores scikit-learn `decision_function`, where smaller values are more anomalous. The notebook passes that value directly to `average_precision_score` and `roc_auc_score` with anomalous = 1. A read-only rerun on v3 gives AP **0.195** in the recorded direction versus **0.739** with `-iso_raw_score`; AUROC **0.205** versus **0.795**. The model's fixed-threshold FPR still exceeds the registered 10% ceiling (reported 61.8%), so Gate C remains failed. These are synthetic event-level results, not evidence of payment safety.
5. **Redaction is insufficient for payment secrets.** The current key-based hash uses a fixed public default salt (`agentsentinel`), allowing offline guesses for low-entropy values. Key matching does not cover every possible payment credential or personal field, and malformed non-mapping input is serialized with `repr` in the rejection path. Payment token handling needs an allowlisted telemetry schema and explicit exclusion of raw credentials; hashes here should not be treated as secure token storage.
6. **Artifacts and documentation drift.** README/status are stale. The model cards/notebook report historic results without saved notebook outputs. EDA text reports 1,206 normal / 617 anomalous events, while current v3 contains 1,209 / 614. `get_isoforest_metadata()` names v2 while `load_features()` reads v3. `contextual.py` defaults to processed v1 while v3's manifest says it came from processed v2. A fresh pipeline run and provenance check belong in Stage 2.
7. **Reproducibility setup is incomplete.** `pyproject.toml` declares Python `>=3.11` and only Pydantic plus pytest, yet local tests passed on Python 3.10.11 with separately installed pandas, scikit-learn, and pyarrow. `requirements.txt` is empty. A clean install is not yet established.

## Stage 2 — exact next work

Verify the existing offline pipeline only: regenerate synthetic traces in a temporary location; validate schema and collector behavior; regenerate preprocessing/features with explicit provenance; check temporal split and leakage; rerun rules/Isolation Forest metrics with correctly oriented anomaly scores; reconcile notebook/model-card counts and version references; document limits of synthetic evaluation. Fix only reproduced bugs and add focused regression tests. Do not add payment fields, policy decisions, gateway code, or PayPal API calls in Stage 2.

Stage 1 is complete. Stage 2 awaits user approval under the requested one-stage-at-a-time workflow.
