# Day 13 — Failure Injection, Reliability, and Performance Engineering

## Scope

This Day 13 pass focuses on controlled failure injection and reliability verification for the local AgentSentinel gateway, approval service, audit chain, and telemetry pipeline. All scenarios are executed with temporary SQLite files, in-process test doubles, and offline mock transports only. No live PayPal calls, no new dependencies, no production credential exposure, and no weakening of deterministic BLOCK decisions.

## Safety invariants preserved

- Deterministic BLOCK remains authoritative.
- Anomaly or advisory signals never authorize a payment.
- No agent may approve its own operation.
- Uncertain provider outcomes are not retried blindly.
- Authorization and idempotency remain exact-bound and durable.
- Telemetry failures do not silently bypass enforcement.
- The kill switch is checked fail-closed in the same SQLite write transaction that consumes authorization and admits execution.
- Sensitive secret values are never exposed in logs or rendered responses.

## Implemented reliability tests

The Day 13 suite adds focused regression coverage for the following scenarios:

1. SQLite unavailable / reservation failure blocks execution.
2. Approval service backend failure does not issue authorizations and does not leak secrets.
3. Audit chain corruption produces a verification failure and a safe diagnostic.
4. Uncertain provider outcomes are not retried on duplicate submit.
5. Telemetry rejection does not relax block enforcement or execute a payment.
6. Concurrent same-operation requests execute only once and reuse the cached result.
7. Concurrent different operations competing for the same budget fail closed at the budget boundary.
8. Rejected or expired approvals cannot reach the gateway.
9. Kill-switch activation and execution admission are serialized through the shared SQLite database; activation-first blocks without consuming authorization, while admission-first is treated as in-flight.
10. Malformed dashboard telemetry is ignored without exposure or crash.
11. Manual demo authorization issuance checks the persisted kill switch in the same `BEGIN IMMEDIATE` transaction as insertion.

Files added:
- [tests/unit/test_day13_reliability.py](D:/AgentObservability/tests/unit/test_day13_reliability.py)
- [scripts/measure_day13_reliability.py](D:/AgentObservability/scripts/measure_day13_reliability.py)

## Performance measurements

Measured with offline synthetic inputs and temporary SQLite files on the local Windows 10 environment using Python 3.10.11.

Gateway decision latency (n=100)
- min: 19.165 ms
- median: 26.040 ms
- p95: 36.603 ms

Approval lookup latency (n=100)
- min: 1.961 ms
- median: 4.621 ms
- p95: 6.008 ms

Audit append latency (n=100)
- min: 9.613 ms
- median: 11.935 ms
- p95: 14.297 ms

Audit verify latency (n=25, 100-record chain)
- min: 10.565 ms
- median: 16.234 ms
- p95: 20.321 ms

Telemetry publish latency (n=100)
- min: 7.862 ms
- median: 11.867 ms
- p95: 16.684 ms

Concurrent duplicate execution check
- first request: COMPLETED
- duplicate request: UNCERTAIN (reconciliation required, not re-executed)
- fake execution count: 1
- PayPal transport call count: 0

## Final verification commands and results

Commands executed:

- .\.venv\Scripts\python.exe -m pytest -q
- .\.venv\Scripts\python.exe scripts/run_attack_suite.py
- .\.venv\Scripts\python.exe scripts/scan_secrets.py
- .\.venv\Scripts\python.exe scripts/verify_payment_audit.py --db <temporary valid chain>
- .\.venv\Scripts\python.exe scripts/measure_day13_reliability.py
- git -c core.whitespace=cr-at-eol diff --check

Observed results:

- Full pytest suite: 295 tests collected and passed (exit code 0).
- Attack suite: 22/22 scenarios passed, 0 PayPal calls.
- Secret scan: no high-confidence credential signatures or credential-shaped historical filenames found.
- Audit verification: VALID: 1 record checked for the safe temporary chain.
- Git diff check: passed.

## Kill-switch admission verification

The focused regression run passed. The ordering tests use `Event` synchronization around the actual gateway submit path:

- `test_kill_switch_activation_before_atomic_admission_blocks_without_claim`: the submit reaches the claim call after policy checks; the test commits switch activation before the SQLite claim transaction. Result: deterministic BLOCK, authorization remains `issued`, and adapter call count is 0.
- `test_execution_admission_wins_before_switch_and_is_not_cancelled`: the fake adapter pauses after claim commit; the test enables the switch while execution is in flight. Result: activation succeeds without a held DB transaction, the admitted operation completes, and adapter call count is exactly 1.
- Missing table/row and invalid-value tests verify fail-closed behavior at the gateway and inside the atomic claim; approval creation and authorization issuance also fail closed when switch state is missing.
- A SQLite trigger-induced claim write failure leaves authorization `issued` and does not invoke the adapter.
- Store-path mismatch is rejected during gateway construction.
- The manual issuer tests cover active switch, missing switch row/table, invalid value, and unreadable database; all reject without inserting an authorization. The disabled-switch path runs through the CLI with the confirmation function accepted, inserts one exact-bound authorization, and preserves the 10-minute expiry.

## Known limitations

- The benchmark intentionally uses temporary SQLite databases and in-process adapters; it does not represent a production database or provider deployment.
- This work is offline-only and is not a claim of production readiness.
- The concurrency guarantee applies when the payment operation store, authorization consumer, and approval kill-switch store resolve to the same SQLite file. Gateway construction rejects unverified or mismatched paths.
- `BEGIN IMMEDIATE` serializes switch activation with the authorization-claim transaction. If activation commits first, the claim observes the active switch and leaves authorization issued and the operation unclaimed. If claim commits first, authorization is consumed and the operation is admitted/in-flight; a later switch activation blocks subsequent admissions but cannot cancel this operation, including one whose adapter dispatch is still pending.
- Missing, unreadable, or invalid kill-switch state fails closed. No database transaction is held during telemetry publication or adapter/provider execution.
- The in-flight boundary does not promise cancellation of a request already admitted or dispatched. No actual PayPal request is sent or retried by these tests.
- The earlier Day 13 verification covered the full 289-test suite. The manual-issuer fix was separately verified with 295 tests collected and passing, 22/22 offline attack scenarios, and `git -c core.whitespace=cr-at-eol diff --check`; the manual-issuer tests use no provider transport.
- Day 13 intentionally stops before any Day 14 work.
