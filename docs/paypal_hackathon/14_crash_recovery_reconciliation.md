# Day 14: Crash recovery and reconciliation

## Scope and implementation

Day 14 adds a durable dispatch marker, transactional recovery, and conservative
reconciliation to the SQLite payment lifecycle. Recovery never issues or
consumes authorization, invokes an adapter, or submits a provider request.
No new package was added.

The lifecycle is:

1. `RESERVED`: the local operation exists; authorization has not been claimed.
2. `CLAIMED` + `NOT_DISPATCHED`: the exact-bound authorization was consumed, but
   the durable provider-dispatch marker has not committed.
3. `CLAIMED` + `MAY_HAVE_DISPATCHED`: the dispatch marker and its operation
   event committed before adapter invocation. The provider may or may not have
   received the request.
4. `COMPLETED`, `REJECTED`, or `UNCERTAIN`: terminal or manually reconcilable
   result. An uncertain result is never automatically submitted again.

The authorization claim atomically consumes the authorization and checks the
kill switch. The trusted gateway also checks the switch in the same SQLite
transaction that commits the dispatch marker. If the switch is enabled after
the claim but before that marker, dispatch is blocked; the consumed
authorization is not restored or retried. Once the marker commits, the
operation is in-flight and a later switch activation cannot cancel an adapter
call already beginning. A crash after the marker but before the adapter call
is still treated as possibly dispatched. SQLite cannot make a local commit
atomic with a remote HTTP request.

## Recovery behavior

`PaymentRecoveryWorker` performs one bounded pass (100 operations by default,
stale after five minutes) when the local `demo_purchase.py` command starts.
`SQLitePaymentStore.recover_stale_operations` uses `BEGIN IMMEDIATE` and
conditional state updates:

| Persisted stale state | Recovery result | Retry behavior |
| --- | --- | --- |
| `RESERVED` + `NOT_DISPATCHED` | `REJECTED` with `OPERATION_NOT_DISPATCHED` | Never retried |
| `CLAIMED` + `NOT_DISPATCHED` | `REJECTED` with `RECOVERY_NOT_DISPATCHED` | Authorization remains consumed; never retried |
| `CLAIMED` + `MAY_HAVE_DISPATCHED` | `UNCERTAIN` with reconciliation required | Never retried |
| Already terminal / uncertain | Not rewritten by the recovery pass | No automatic retry |

Each recovery state update and its `operation_events` entry are written in one
SQLite transaction. An event-insertion failure rolls back the state change.
Dispatch-marker state and its event are likewise committed together, and an
adapter is not invoked unless that marker transaction succeeds. Legacy
`CLAIMED`, `UNCERTAIN`, and `COMPLETED` rows without a marker are migrated
conservatively to `MAY_HAVE_DISPATCHED`; legacy `RESERVED` rows become
`NOT_DISPATCHED`.

Reconciliation is read-only with respect to the provider and conditional with
respect to the current persisted operation state, dispatch state, attempt, and
idempotency key. A stale verifier result cannot overwrite a concurrent state
transition. Repeated `STILL_UNCERTAIN` checks do not add duplicate transition
events. After dispatch may have occurred, `NO_PAYMENT` is accepted only from a
trusted verifier that provides provider-confirmed evidence and matches the
specific operation, attempt, and idempotency key. The current Sandbox verifier
does not treat a missing order lookup as proof of no payment; it remains
uncertain instead.

## Safety and failure boundaries

- Deterministic policy `BLOCK`, exact-bound authorization, budgets, and
  idempotency are unchanged.
- Authorization claim and dispatch-marker admission both check the kill
  switch through the same SQLite file and serialized transaction boundary.
  Recovery does not execute payments; a kill-switch activation racing either
  transaction is serialized by SQLite, and subsequent submissions are still
  blocked while active.
- Database failure, missing/invalid dispatch evidence, verifier failure, and
  mismatched operation/attempt/idempotency evidence fail closed.
- A `NOT_DISPATCHED` result is a terminal classification, not permission to
  retry. A future retry would need a separate explicit trusted authorization
  and idempotency policy.
- `operation_events` are transactional local records, not an external
  tamper-proof anchor.
- All Day 14 tests use temporary SQLite databases, fake adapters, and fake
  verifiers. No live PayPal request was made.

## Developer integration: `@guard`

`from agentsentinel import guard` accepts only a `TrustedGatewayEntryPoint`.
The decorated function is a proposal builder: it returns a `PaymentIntent`,
which the decorator sends through the entry point; the entry point obtains
authenticated context and calls the existing `PaymentGateway.submit` path.
`GatewayResult` is returned unchanged. Policy, authorization claim, kill-switch,
dispatch marker, adapter, and reconciliation behavior remain in the existing
gateway.

The proposal function must not perform payment or other external side effects.
The decorator is an integration convenience, not a Python sandbox. Arbitrary
Python code in the same process must be trusted; private-object introspection
is not prevented. Payment execution must always go through
`TrustedGatewayEntryPoint`, and the provider adapter must not be exposed to
untrusted agent code. The adapter is not passed as an argument or returned by
the decorated function, but application deployment must still keep the gateway
and credentials outside agent-controlled code. See the five-line
[quickstart](./14_quickstart.md).

## Verification evidence

Environment: repository `.venv` on Windows, Python invoked through
`.venv\Scripts\python.exe`; SQLite temporary files under pytest's temporary
directory.

The focused Day 14 suite covers 26 test cases, including:

- Crashes after reservation, after claim, after dispatch marking, and after a
  fake provider accepts but before local result persistence.
- Timeout, verifier unavailability, mismatched operation/attempt/key,
  repeated reconciliation, and `NO_PAYMENT` evidence requirements.
- Concurrent recovery workers, kill-switch activation serialized against
  recovery, recovery database-write failure, recovery-event insertion failure,
  and conservative legacy migration.
- Restart behavior for `RESERVED`, both `CLAIMED` dispatch states, and
  `COMPLETED`, `REVIEW_REQUIRED`, `UNCERTAIN`, and `REJECTED`.
- Dispatch-marker event failure prevents adapter invocation.
- Guard integration tests cover successful execution, missing authorization,
  deterministic policy block, kill switch, review-required behavior, provider
  uncertainty/idempotent replay, adapter non-exposure in the callable API, and
  a successful run from a copied source tree with a clean working directory.

Recorded validation:

| Command | Result |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest -o addopts= -q tests\unit\test_payment_recovery.py` | 26 passed |
| `.venv\Scripts\python.exe -m pytest -o addopts= -q` | 321 passed; 1 third-party Starlette deprecation warning |
| `.venv\Scripts\python.exe scripts\run_attack_suite.py` | 22/22 offline scenarios passed; each reported zero PayPal calls |
| `.venv\Scripts\python.exe scripts\scan_secrets.py` | Scanned 177 worktree files and 306 reachable history blobs; no high-confidence credential signatures or credential-shaped historical filenames |
| `.venv\Scripts\python.exe scripts\verify_payment_audit.py --db <temporary-db>` | Valid empty chain; 0 records checked |
| `git -c core.whitespace=cr-at-eol diff --check` | Passed |

## Limitations

The dispatch marker intentionally creates a conservative uncertainty window:
if the process crashes after its commit but before the adapter invocation, the
system cannot prove that no request was sent. Recovery therefore does not
resubmit. Similarly, a remote provider acceptance followed by a local crash
requires trusted read-only reconciliation; if evidence is unavailable or
ambiguous, the operation remains uncertain for manual handling. Recovery is a
bounded local startup pass, not a continuously running service, and external
provider behavior and production performance were not tested.

The crash-recovery implementation in this report is included in commit
`1cd74ca` (`main`); a read-only `git ls-remote origin refs/heads/main` check
confirmed that commit was present on `origin/main` when the Developer
Experience continuation began. The `@guard` integration and quickstart are
separate local changes until explicitly reviewed and committed.
