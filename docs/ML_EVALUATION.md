# Validation-calibrated anomaly detector experiment

**Result: LIMITED EMPIRICAL PASS — task-group calibration measured 9.16% FPR
on the synthetic held-out test set, with a trace-bootstrap 95% interval that
extends to 12.68%.** The global-threshold experiment remains above target at
16.4%. This is not a production model qualification. Detector output remains
advisory and cannot authorize payment or override deterministic policy.

Reproduce from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_calibrated_detector.py
```

The script reads the tracked `data\synthetic\v3` JSONL files and does not
rewrite generated datasets or model artifacts.

## Dataset and label audit

| Unit | Normal | Anomalous | Total |
|---|---:|---:|---:|
| Traces | 500 | 120 | 620 |
| Events | 1,215 | 616 | 1,831 |

The synthetic trace labels are balanced by anomaly category at 40 traces each:
`tool_loop`, `retry_storm`, and `resource_spike`. Event counts differ because
the generator creates different trace lengths: 320 tool-loop, 200 retry-storm,
and 96 resource-spike anomalous events. This makes event-level prevalence
synthetic and unlike an independently sampled production distribution.

The generator is deterministic with seed `20260818`; its labels and features
are both produced from hand-authored scenarios. The three anomaly classes do
not represent an independently adjudicated or representative incident set.
All examples use the synthetic generator's single agent identity.

Reproduction metadata: model experiment version
`payment-advisory-iforest-task-calibrated-temporal-60-20-20-v3`, dataset
`synthetic-mvp1-v3`, Python 3.10.11, pandas 2.3.3, and scikit-learn 1.7.2.
The evaluation script emits these values, split cutoffs, and overlap counts
with its metrics.

## Split and leakage controls

This experiment uses a new trace-level chronological **60% train / 20%
validation / 20% test** split. All three pairwise trace-ID overlap
counts are zero. Cutoffs are:

- Training ends before `2026-01-02T03:36:00+00:00`.
- Validation ends before `2026-01-02T09:48:00+00:00`.
- Test begins at or after `2026-01-02T09:48:00+00:00`.

Contextual feature statistics and the inferred permission map are fit on
normal-labeled training events only. Isolation Forest is fit on normal-labeled
training traces only (random seed 42). Validation labels select the global thresholds
and per-task thresholds; test data is never used for threshold selection. The
global 10%-cap threshold is `0.04208268944906557`; the strict
zero-validation-FPR threshold is `1.1557259958596413`. The per-task thresholds
are `address_change: 0.051390802812970815`,
`delivery_status: 0.10229491504114485`, and
`order_lookup: 0.04208268944906557`. Each task threshold is independently
selected to maximize validation recall subject to a 10% validation FPR cap.
An unseen task group produces no advisory alerts.

This is a distinct follow-up protocol, not a replacement for the registered
80/20 Gate C result. It does not tune on test data.

## Metrics

Test support: 351 events (262 normal, 89 anomalous). The intervals below are
deterministic 1,000-replicate bootstrap percentile intervals resampling whole
traces (seed 42); they describe variability within this synthetic test sample,
not uncertainty over real payment behavior.

| Detector / operating point | PR-AUC | ROC-AUC | Precision | Recall | F1 | FPR | FNR | TP / FP / TN / FN |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Isolation Forest, validation FPR ≤10% | 0.716 | 0.794 | 0.583 | 0.674 | 0.625 | **0.164** | 0.326 | 60 / 43 / 219 / 29 |
| Isolation Forest, validation FPR =0% | 0.716 | 0.794 | 0.000 | **0.000** | 0.000 | **0.000** | 1.000 | 0 / 0 / 262 / 89 |
| Isolation Forest, per-task validation FPR ≤10% | 0.716 | 0.794 | 0.692 | 0.607 | 0.647 | **0.092** | 0.393 | 54 / 24 / 238 / 35 |
| Existing rules, fixed documented thresholds | 0.463 | 0.640 | 1.000 | 0.281 | 0.439 | 0.000 | 0.719 | 25 / 0 / 262 / 64 |

The per-task operating point is the first learned-detector result in this
experiment with an empirical overall test FPR below 10%: 24/262 normal events
(9.16%). Its validation FPR was 13/230 (5.65%). However, the trace-bootstrap
95% test FPR interval is **5.88–12.68%**, so this small synthetic test does not
establish that the true FPR is below 10%. The per-task test breakdown is also
uneven: `address_change` 17/129 (13.18%), `delivery_status` 2/73 (2.74%), and
`order_lookup` 5/60 (8.33%). This does not support a claim that every task
subpopulation meets the target.

The strict operating point achieved zero observed test false positives only by
predicting **no anomalies**: it detected 0 of 89 anomalous events. It therefore
does not meet the high-recall objective and is not a useful detector. Test
scores were not used to select this threshold.

Calibrated detector confusion matrix (actual rows, predicted columns):

|  | Predicted normal | Predicted anomalous |
|---|---:|---:|
| Actual normal | TN 219 | FP 43 |
| Actual anomalous | FN 29 | TP 60 |

For the 10%-cap operating point, calibrated Isolation Forest FPR trace-bootstrap
95% interval is **0.125–0.204** and recall interval is **0.594–0.732**.
Validation FPR was 0.100 (23/230 normal events), but the test FPR rose to 0.164
(43/262). The registered ≤10% maximum and the requested 0% target are both
**not achieved** by this operating point. Higher PR-AUC than the rules baseline
does not offset the unacceptable false-positive rate.

The strict operating point and rules baseline each observed 0 false positives
among **262** normal test events. Under an independent Bernoulli assumption,
the exact one-sided 95% upper bound is **1.14%** (`1 - 0.05^(1/262)`), not a
universal zero-FPR guarantee. Trace clustering and synthetic sampling further
limit that bound's interpretation. Rules recall is only 28.1%; strict Isolation
Forest recall is 0%.

| Anomaly category (one-vs-rest) | Test events | Detected | Recall | Precision among all anomaly alerts |
|---|---:|---:|---:|---:|
| `resource_spike` | 21 | 9 | 0.429 | 0.087 |
| `retry_storm` | 20 | 15 | 0.750 | 0.146 |
| `tool_loop` | 48 | 36 | 0.750 | 0.350 |

The precision column treats a predicted anomaly of another category as a
false positive for the listed one-vs-rest category. The detector itself is
binary and does not classify anomaly type.

With task-group calibration, per-category test performance is:

| Anomaly category (one-vs-rest) | Test events | Detected | Recall | Precision among all anomaly alerts |
|---|---:|---:|---:|---:|
| `resource_spike` | 21 | 9 | 0.429 | 0.115 |
| `retry_storm` | 20 | 13 | 0.650 | 0.167 |
| `tool_loop` | 48 | 32 | 0.667 | 0.410 |

Normal-event false-positive rates by task type under the **global** threshold:

| Task type | Normal events | False positives | FPR |
|---|---:|---:|---:|
| `address_change` | 129 | 23 | 0.178 |
| `delivery_status` | 73 | 15 | 0.205 |
| `order_lookup` | 60 | 5 | 0.083 |

Task-group calibrated normal-event false-positive rates:

| Task type | Normal events | False positives | FPR |
|---|---:|---:|---:|
| `address_change` | 129 | 17 | 0.132 |
| `delivery_status` | 73 | 2 | 0.027 |
| `order_lookup` | 60 | 5 | 0.083 |

The per-task calibration is an experiment, not evidence that these synthetic
task categories represent production workloads. One subgroup exceeds 10% on
test despite its validation-only threshold.

## Decision and next experiment

Keep the rules baseline and deterministic payment policy authoritative. The
Isolation Forest may only request review; it must not authorize, dispatch, or
turn a policy `BLOCK` into an allow.

Task-group calibration reduces the overall empirical test FPR below 10% while
retaining 60.7% recall, but the confidence interval crosses the target and one
task group remains above 10%. The test data is synthetic, small, and drawn from
one agent identity. The next valid experiment needs independently collected
and adjudicated traces, multiple agents and task distributions, preregistered
threshold selection, and an untouched external test set. The registered
original Gate C still fails; do not use the detector as a payment gate.
