# Day 11 — Payment Observability and Tamper-Evident Audit

## Architecture

```text
Agent proposal
   │
   ▼
Trusted entry point ── optional trusted detector advisor (signals only)
   │
   ▼
Payment gateway ── deterministic policy / authorization / kill switch ── mock or approved adapter
   │                         │
   ├── payment lifecycle events
   └── approval and kill-switch events
                  │
                  ▼
         TraceCollector.capture_payment_event
                  │
                  ▼
      SQLite payment event + hash-chain ledger
```

The existing `TraceCollector`, strict trace model, redaction helpers, rules engine, and Isolation Forest implementation remain in use. Payment events use the separate `payment-event-v1` schema and a separate SQLite ledger; the customer-support trace dataset and its feature artifacts are unchanged.

Each event carries an event ID, schema version, UTC timestamp, trace/span/parent-span IDs where available, operation and intent IDs, optional approval ID, action, event type, gateway decision, safe reason codes, measured duration, provider outcome category, and optional detector score/version. Proposal → policy → dispatch → outcome spans use stable IDs derived from the trace and operation. The audit sequence is the authoritative event order when timestamps coincide. An approval shares the same trace and operation IDs as its payment; its approval ID is included on approval events.

The schema is an allowlist: it contains no amount, merchant, purpose, raw provider response, URL, credential, token, or unnecessary personal data. Invalid events are rejected without storing their payload. Safe rejection codes are kept in memory, logged, and written to a minimal rejection JSONL record. A collector outage is reported by the publisher and does not change policy, approval, authorization, kill-switch, or dispatch decisions. The existing deterministic controls remain authoritative.

## Detector integration

`PaymentFeatureAdapter` maps only event counts and timing into the existing 13-feature contract. It does not expose payment values or agent-supplied authorization data. The existing rules are surfaced as `payment-rules-v1`; the existing Isolation Forest implementation is wrapped as `payment-iforest-synthetic-v1`. Both return versions, safe reason codes, and scores. A trusted `PaymentDetectorAdvisoryProvider` can request REVIEW; unavailable detector input yields no advisory and still leaves deterministic policy evaluation in place. A hard BLOCK remains BLOCK even if a caller supplies a review signal.

The Isolation Forest fit below is a separate, in-memory fit using only the payment synthetic normal training split. This is necessary because the existing customer-support baseline features are not a calibrated payment-domain model. It does not retrain, replace, or change the prior baseline or its threshold.

## Synthetic evaluation

Command: `python scripts/evaluate_payment_observability.py`
Seed: `20261028`; total 210 synthetic traces (100 normal, 110 anomalous: 10 in each of 11 scenarios). The first 70 normal traces trained the payment-only Isolation Forest; evaluation used 30 normal and 110 anomalous traces (140 total). PR-AUC below is scikit-learn average precision. Confusion matrices are TP/FP/TN/FN; runtime is local inference time for the 140 evaluation traces.

| Detector | Precision | Recall | F1 | PR-AUC | FPR | TP / FP / TN / FN | Runtime total / trace |
|---|---:|---:|---:|---:|---:|---|---:|
| Payment rules | 1.000000 | 0.181818 | 0.307692 | 0.824675 | 0.000000 | 20 / 0 / 30 / 90 | 214.09 ms / 1.53 ms |
| Payment Isolation Forest | 0.830986 | 0.536364 | 0.651934 | 0.859177 | 0.400000 | 59 / 12 / 18 / 51 | 2,603.70 ms / 18.60 ms |

Measured locally on 2026-10-29 with the isolated project environment; runtime varies by host. This deliberately anomaly-heavy synthetic evaluation is not a production estimate and is not an enforcement test. Its precision-recall values are prevalence-sensitive. The payment Isolation Forest's 40% FPR is not a passing operating point. Separately, the existing customer-support baseline remains Gate C **FAIL**: the corrected v4 report records Isolation Forest PR-AUC 0.706 and FPR 56.9% versus rules PR-AUC 0.463 and FPR 0%; the registered FPR ceiling is 10%. These results are not combined.

## Hash-chain audit

New payment events and audit entries are appended in one SQLite `BEGIN IMMEDIATE` transaction. Sequence allocation is serialized across concurrent writers. The chain starts at 64 zeroes; each SHA-256 hash commits to the sequence, previous hash, canonical sorted compact JSON event, timestamp, event type, safe actor role, and record ID. A duplicate stable event ID with unchanged substantive content is idempotent; conflicting content is rejected. Run `python scripts/verify_payment_audit.py` to check continuity and hashes; it prints only record counts, the first broken sequence, and a safe reason code.

The Day 10 `operation_events` and `approval_events` rows remain unchanged and are not silently backfilled. The new chain covers payment telemetry recorded after its introduction. This is tamper-evident, not tamper-proof: a local database administrator can alter data and recompute the full chain; deleting a middle row is detected, but truncating a valid tail cannot be detected without an externally retained checkpoint. Local-machine compromise, backups, and external anchoring are outside this prototype.

## Validation and limitations

The payment test module covers event schema/redaction, trace/span correlation, idempotency, collector failures, deterministic fixtures, both detectors, advisory-only policy behavior, hash success/edit/reorder, concurrent append, legacy-row preservation, and offline execution. On 2026-10-29, the full suite collected and passed 246 tests; the two focused audit tests (`test_audit_chain_success_edit_and_reordering_detection` and `test_concurrent_audit_append_has_unique_ordered_chain`) passed, as did focused telemetry-failure and advisory-vs-BLOCK checks. The extended attack suite retains all 19 existing scenarios and adds three Day 11 cases; all 22 passed. All payment and evaluation paths use local SQLite and in-process fakes; no live PayPal call, new package, or model download is involved.

The fixtures are controlled demonstrations, not real PayPal behavior. The synthetic training and test samples are small, intentionally scenario-shaped, and not representative of a deployment distribution. Detector thresholds are advisory and not calibrated for production. The audit chain has no remote anchor and only protects new telemetry; existing legacy audit rows retain their prior integrity properties.
