# Day 15: Approval-aware payment execution

## Status and scope

Day 15 connects the existing purchase proposal, policy, SQLite safety store,
human approval service, fake/Sandbox adapter boundary, telemetry audit chain,
and dashboard. It does not replace the Day 7–9 gateway or the Day 14 recovery
store, and it does not claim production readiness. Acceptance status is based
on the recorded commands and results below.

No live PayPal call, capture, credential read, model download, or new package is
part of this implementation or its tests.

## Architecture and trust boundaries

```text
Purchase agent proposal
        |
        v
TrustedGatewayEntryPoint ---- authenticated context provider
        |                     internal advisory provider (review only)
        v
PaymentGateway ---- PaymentPolicy ---- SQLite authorizations
        |                  |            hard BLOCK is absolute
        |                  +------------ REVIEW creates no execution capability
        v
ApprovalStore (same SQLite file)
  durable request + exact intent snapshot + human approval
  atomic authorization issuance + kill switch + approval audit
        |
        v
ApprovedPaymentExecutor (trusted application only)
        |
        v
PaymentGateway -> atomic authorization claim -> kill-switch-checked dispatch marker
        |
        v
Protected fake adapter / explicitly enabled Sandbox adapter
        |
        +-> SQLite operation state, dispatch marker, reconciliation
        +-> minimized telemetry -> SQLite hash chain -> read-only dashboard
```

The purchase agent receives a proposal result only. It does not receive
`AuthenticatedScope`, an authorization repository, `ApprovalStore`, provider
credentials, or the payment adapter. The trusted entry point obtains identity
from its injected context provider. Advisory signals can request review but
cannot change BLOCK or create authorization.

`ApprovalStore` and `SQLitePaymentStore` must resolve to the same SQLite path
for an approval-aware gateway. Creation of an approval and its local approval
audit event is one transaction. Human approval, exact authorization issuance,
and the approval status/event transition are one SQLite transaction. Payment
authorization consumption and dispatch-marker updates are separately
transactional, with no external provider call inside a database transaction.

## Approval-to-execution lifecycle

1. The agent proposes an intent; the trusted entry point supplies authenticated
   context and any internal advisory review signal.
2. The deterministic policy checks scope, currency, action, hard amount
   limits, and authorization. Hard BLOCK is never converted into REVIEW.
3. If the result is REVIEW, the service persists a pending request including
   the full validated intent snapshot, authenticated principal/scope,
   SHA-256 request fingerprint, reason codes, and expiry. No provider dispatch
   or payment authorization occurs at this stage.
4. An authenticated approver views the persisted request and confirms its
   fingerprint. Approval expiration, kill-switch state, and terminal status
   are checked before issuance.
5. In one SQLite transaction, the request becomes APPROVED and the application
   issues an exact authorization bound to intent, operation, scope, merchant,
   amount, currency, action, target order, approval ID, and original review
   reasons. Repeated approval is idempotent.
6. `ApprovedPaymentExecutor` reads only the approved persisted snapshot and
   its corresponding authorization. It does not accept a replacement intent.
7. The gateway re-evaluates policy. A matching approval can satisfy only the
   original REVIEW reasons; mismatched fields or any hard BLOCK prevent
   execution.
8. The payment store atomically consumes the authorization and checks the kill
   switch. The dispatch-marker transaction checks the switch again before
   recording that a provider request may be sent.
9. The protected adapter is invoked once. A definite sanitized `FAILED`
   outcome is persisted as terminal rejection and emits `PAYMENT_FAILED`.
   An exception, timeout, malformed result, or ambiguous response remains
   `UNCERTAIN`; it is never automatically retried.
10. Completion/failure/uncertainty and reconciliation state are persisted.
    The dashboard reads actual audit-chain events and SQLite operation state;
    it does not synthesize payment outcomes.

### Kill-switch concurrency boundary

Authorization claim and dispatch-marker admission are separate SQLite
transactions. If the switch is activated after claim but before the dispatch
marker, the marker transaction rejects dispatch; the authorization remains
consumed and the operation is not retried. Once the marker commits, the
operation is in-flight and a later switch activation cannot cancel an adapter
call already beginning. A local SQLite commit cannot be atomic with a remote
provider request. See the corrected boundary in
[Day 14 recovery documentation](./14_crash_recovery_reconciliation.md).

## Telemetry and dashboard

The persisted event chain includes:

- `PAYMENT_PROPOSED`, `POLICY_EVALUATED`, and `PAYMENT_REVIEW_REQUIRED`.
- `APPROVAL_REQUESTED`, `APPROVAL_APPROVED`, and the compatible
  `APPROVAL_GRANTED` alias.
- `PAYMENT_DISPATCHED`, `PAYMENT_COMPLETED`, `PAYMENT_FAILED`,
  `PAYMENT_UNCERTAIN`, and `PAYMENT_REJECTED`.
- `PAYMENT_BLOCKED` remains emitted alongside `PAYMENT_REJECTED` for existing
  readers.

Events include bounded IDs, policy version, decision, safe reason codes,
amount/currency, provider outcome category, timestamps, actor role, and stable
span relationships. Approval events are children of the approval request;
dispatch is a child of policy evaluation; completion, failure, and uncertainty
are children of dispatch. The dashboard trace detail also reads the persisted
operation state, dispatch marker, and whether a verified reconciliation event
exists. Raw provider payloads, access tokens, secrets, and intent purpose text
are not included in payment telemetry.

`PAYMENT_FAILED` is emitted only for an adapter's explicit sanitized
`AdapterOutcome(status="FAILED")`. Exceptions and timeouts are not classified
as definite failures because the provider may already have accepted the
request. The existing PayPal Sandbox adapter is manual-only and treats
ambiguous/non-completed outcomes conservatively; no Sandbox request was used
to validate Day 15.

## API and local dashboard

Approval API endpoints:

| Endpoint | Purpose | Required protection |
| --- | --- | --- |
| `GET /approvals` | List stored approvals | Bearer approver credential |
| `GET /approvals/{approval_id}` | Read one approval | Bearer approver credential |
| `POST /approvals/{approval_id}/approve` | Approve exact request and issue authorization | Bearer plus matching CSRF header/cookie |
| `POST /approvals/{approval_id}/reject` | Reject pending request | Bearer plus matching CSRF header/cookie |
| `GET /killswitch` | Read persisted switch | Bearer approver credential |
| `POST /killswitch` | Change switch | Bearer plus matching CSRF header/cookie |
| `GET /audit` | Read local approval audit events | Bearer approver credential |

The browser dashboard is served at `/dashboard/login`, with overview, traces,
alerts, approvals, and offline-demo pages under `/dashboard`. The approver
credential is process-local configuration. A separate viewer credential is
optional and only grants read-only dashboard access.

For a local UI walkthrough, start the existing service with a temporary
`APPROVER_SECRET` in the current PowerShell process and visit
`http://127.0.0.1:8000/dashboard/login`. Keep credentials out of `.env`, source
files, command-line arguments, and persistent Windows environment settings.
Use `/dashboard/demo` for its clearly labeled offline walkthrough; it does not
create a payment or claim persisted provider outcomes.

## Local fake-adapter demonstration

Run the end-to-end integration test from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest -o addopts= -q tests\integration\test_day15_approval_execution.py::test_review_approval_dispatch_and_dashboard_show_persisted_lifecycle
```

The test uses temporary SQLite files, a deterministic context, a local fake
adapter, and FastAPI `TestClient`. It performs REVIEW, persists and approves
the exact request, dispatches once through the fake adapter, verifies the
hash-chain trace/span parents, and checks the dashboard approval and trace
pages. Related tests cover changed intent fields, hard BLOCK, duplicate
approval/execution, expiration, switch races, definite fake failure, timeout,
restart, and non-retry behavior. The test does not call PayPal.

Do not use the manual `scripts/demo_purchase.py create` or `capture` commands
for this offline demonstration; those commands are outside the fake-adapter
test path and may make Sandbox requests.

## Test evidence

Validation from the repository root using the existing `.venv`:

| Check | Result |
| --- | --- |
| Focused Day 15 integration suite | `13 passed`, 1 third-party Starlette/AnyIO deprecation warning |
| `.\.venv\Scripts\python.exe -m compileall -q src\agentsentinel` | Passed |
| `.\.venv\Scripts\python.exe -m pytest -o addopts= -q` | `348 passed`, 1 third-party Starlette/AnyIO deprecation warning |
| `.\.venv\Scripts\python.exe scripts\run_attack_suite.py` | `22/22` offline scenarios passed; no PayPal calls |
| `.\.venv\Scripts\python.exe scripts\scan_secrets.py` | Scanned 186 worktree files and 337 reachable history blobs; no high-confidence credential signatures or credential-shaped historical filenames |
| `git -c core.whitespace=cr-at-eol diff --check` and staged equivalent | Passed |

Plain `git diff --check` reports CR-at-end-of-line as trailing whitespace in
tracked CRLF files and in two pre-existing generated metadata changes. The
CRLF-aware check passes; no line-ending normalization or generated-data edits
were made to suppress those diagnostics. The worktree also contains unrelated
pre-existing user changes, which were preserved. No commit or push was made.

## Limitations

- The service and dashboard are localhost-oriented prototypes; there is no
  production identity provider, deployment boundary, key management, or
  multi-tenant operations model.
- SQLite provides local transaction guarantees, not distributed transactions
  with a remote payment provider.
- A timeout or crash after the dispatch marker remains uncertain until trusted
  reconciliation has sufficient provider evidence; no automatic retry occurs.
- `PAYMENT_FAILED` currently represents an explicit safe adapter outcome. The
  current Sandbox adapter has not been changed to classify provider responses
  as definite failures.
- The tests prove local behavior with fakes, not PayPal Sandbox/Live behavior,
  production availability, or production readiness.
