# Offline attack suite results

Run: `python scripts/run_attack_suite.py`. Spy calls count only the attack attempt; setup calls are excluded. No live provider requests were made.

| Scenario | Expected decision | Actual decision | Reason code | PayPal calls | Pass/fail |
|---|---|---|---|---:|---|
| amount above authorization | BLOCK | BLOCK | AUTHORIZATION_AMOUNT_MISMATCH | 0 | PASS |
| merchant mismatch | BLOCK | BLOCK | AUTHORIZATION_MERCHANT_MISMATCH | 0 | PASS |
| missing authorization | BLOCK | BLOCK | AUTHORIZATION_MISSING | 0 | PASS |
| forged agent authorization | BLOCK | BLOCK | AUTHORIZATION_MISSING | 0 | PASS |
| replayed authorization | BLOCK | BLOCK | AUTHORIZATION_INACTIVE | 0 | PASS |
| expired authorization | BLOCK | BLOCK | AUTHORIZATION_EXPIRED | 0 | PASS |
| duplicate capture request | CACHED | CACHED | EXECUTED_SANDBOX | 0 | PASS |
| adapter timeout | UNCERTAIN | UNCERTAIN | PROVIDER_OUTCOME_UNCERTAIN | 0 | PASS |
| loop limit | BLOCK | BLOCK | LOOP_LIMIT_EXCEEDED | 0 | PASS |
| per-day budget | BLOCK | BLOCK | BUDGET_EXCEEDED | 0 | PASS |
| per-merchant budget | BLOCK | BLOCK | BUDGET_EXCEEDED | 0 | PASS |
| prompt injection | BLOCK | BLOCK | AUTHORIZATION_MERCHANT_MISMATCH | 0 | PASS |
| REVIEW amount threshold | REVIEW | REVIEW | REVIEW_AMOUNT_THRESHOLD | 0 | PASS |
| advisory escalation | REVIEW | REVIEW | REVIEW_ADVISORY_SIGNAL | 0 | PASS |
| safety dependency failure | BLOCK | BLOCK | OPERATION_RESERVATION_FAILED | 0 | PASS |
| approval fingerprint mismatch | BLOCK | BLOCK | approval request fingerprint mismatch | 0 | PASS |
| approval kill switch active | BLOCK | BLOCK | kill switch active | 0 | PASS |
| approval cannot override block | BLOCK | BLOCK | KILL_SWITCH_ACTIVE | 0 | PASS |
| approval stale expiry | BLOCK | BLOCK | approval expired | 0 | PASS |
| advisory anomaly cannot override block | BLOCK | BLOCK | AUTHORIZATION_AMOUNT_MISMATCH | 0 | PASS |
| telemetry failure cannot authorize | BLOCK | BLOCK | AUTHORIZATION_MISSING | 0 | PASS |
| tampered audit record detected | BLOCK | BLOCK | RECORD_HASH_MISMATCH | 0 | PASS |
