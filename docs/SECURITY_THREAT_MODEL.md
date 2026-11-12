# AgentSentinel security threat model

**Assessment scope:** local repository implementation and offline tests. The
optional Render mock-only evaluator profile is evaluated separately below;
this is not a production security review.

This is a threat model for a local prototype, not a penetration-test
certificate or production security approval. Findings marked **VERIFIED** are
supported by inspected implementation and/or tests; **PARTIAL** means a
local control exists but the stated production boundary is unverified;
**MISSING** means no implementation/evidence was found in the reviewed
workspace.

## Protected assets

- Buyer intent: principal/scope, merchant, amount, currency, action, order,
  expiry, and one-time authorization state.
- Payment operation and dispatch state, stable idempotency key, provider
  identifiers, and reconciliation evidence.
- Approver identity, pending approval records, approval audit events, and
  kill-switch state.
- PayPal Sandbox credentials and provider responses.
- Telemetry, audit-chain history, and local checkpoint.
- Synthetic data, evaluation labels, model artifacts, and reported metrics.

## Trust boundaries

```text
Untrusted prompt, catalog, or agent proposal
                 |
                 v
Validated PaymentIntent (data only)
                 |
                 v
Trusted application entry point -- authenticated context
                 |
                 v
PaymentGateway -- deterministic policy -- SQLite payment/approval stores
                 |                  |
                 |                  +-- human approval and kill switch
                 v
Protected fake adapter / explicitly enabled manual Sandbox adapter
                 |
                 +-- fixed PayPal Sandbox host (manual opt-in only)
                 +-- LIVE adapter placeholder (always disabled; no dispatch)
                 +-- telemetry chain and local checkpoint

Local operator browser -- authenticated localhost dashboard -- read-only
telemetry snapshots / separately persisted approval actions
```

The agent must not be trusted with authorization issuance, approver identity,
the adapter, provider credentials, or direct database access. The current
`@guard` and Python object boundaries are not an OS/process sandbox: arbitrary
Python executing in the same process must be trusted. The operator dashboard
uses shared-secret authentication; local cookies default to non-Secure for
localhost HTTP. The restricted Render evaluator enables Secure cookies and
does not expose the general dashboard anonymously.

## Threat assessment

| Threat | Existing control and evidence | Status and residual risk |
|---|---|---|
| Prompt injection or malicious payment proposal | The agent returns a proposal only. The gateway revalidates the intent and checks trusted context, policy, and exact-bound authorization. Offline prompt-injection scenarios are among the attack tests. | **PARTIAL** — local proposal-to-gateway boundary is tested, but same-process Python is not isolated. |
| Agent forges or issues its own approval | Authorization issuance is owned by trusted approval service code; the agent-facing path does not receive an approval issuer or payment adapter. Approval binds the exact intent and authenticated principal/scope. | **PARTIAL** — application-level tests pass; production process permissions and identity-provider boundaries are absent. |
| Approval replay, expiry, or modified intent | Approval expiry, fingerprint binding, atomic authorization issuance/consumption, and terminal state are enforced in the SQLite workflow and covered by tests. | **VERIFIED LOCALLY** — does not establish distributed consistency or production identity. |
| Hard BLOCK bypass or advisory override | Policy evaluates hard constraints before advisory REVIEW; gateway does not dispatch BLOCK or REVIEW. Offline attack suite includes BLOCK precedence scenarios. | **VERIFIED LOCALLY** — not an independently audited production service. |
| Kill switch races with admission or dispatch | Kill switch is checked in serialized authorization-claim and dispatch-marker transactions. Once the marker commits, an already admitted provider operation is in flight and cannot be cancelled by a later switch activation. | **VERIFIED LOCALLY** — the documented in-flight boundary is inherent; local SQLite cannot revoke an already sent network request. |
| Duplicate operation, replay, crash, or provider timeout | Durable operation reservation and dispatch intent, stable idempotency association, conservative recovery, and no automatic retry of uncertain operations. Fake-verifier tests cover restart/reconciliation boundaries. Sandbox REST calls use the access token obtained from OAuth and fake-transport tests verify the authenticated request without network access. | **PARTIAL** — provider-specific delivery semantics, availability, actual Sandbox integration, and exactly-once execution are not proven. Unknown create-order IDs can remain uncertain. |
| SQLite row corruption, tampering, or rollback | Day 16 sidecar checkpoint detects database-only tail deletion, rollback, and row edits after checkpoint establishment. Day 17 validates storage types, event metadata, timestamps, and hashes; authenticated dashboard routes withhold invalid telemetry. Focused tests cover these cases. | **PARTIAL** — an attacker able to modify both database and sidecar can defeat local anchoring. Legacy history cannot be retroactively anchored; no remote immutable ledger exists. |
| Telemetry outage or malformed data changes payment authorization | Dashboard telemetry reads use a verified snapshot and degrade independently. Telemetry failure is not an authorization capability; attack-suite telemetry failure case blocks payment. | **VERIFIED LOCALLY** — production data-store failure modes are not established. |
| Unauthorized dashboard use, role escalation, CSRF, or session tampering | HMAC-signed expiring sessions, viewer/approver role checks, CSRF token matching, bounded form parsing, and tests for invalid login, viewer mutation, and CSRF are present. | **PARTIAL** — the public evaluator permits only a bounded, rate-limited scenario POST with server-defined data; operator authentication remains a prototype with no external identity provider or shared rate limiter. |
| Credential or sensitive provider data leakage | Provider client credentials are configured separately from agent proposals; telemetry and adapter results use bounded schemas and sanitized status. Latest secret scan found no high-confidence credential signatures in 191 worktree files and 404 reachable history blobs. | **PARTIAL** — a heuristic scan is not proof of absence. No production secret vault or rotation process is deployed. |
| Denial of service / brute-force login | Form body size, field count, trace pagination, and telemetry query result sizes are bounded. | **MISSING** — no shared authentication rate limiter, deployment-level request throttling, or load-test evidence was found. |
| Detector false positives or false negatives affect payment | Detector scores are advisory and cannot authorize or override deterministic BLOCK. | **PARTIAL** — the registered Isolation Forest fails Gate C (FPR 56.9%); task-group calibration selected on validation measures 9.16% synthetic test FPR, but its trace-bootstrap interval reaches 12.68% and `address_change` reaches 13.2%. Synthetic data is not representative; ML must not gate payments. |
| Local host, OS account, or filesystem compromise | SQLite and checkpoint files are local; deployment documentation warns that local controls are prototypes. | **MISSING** for production — file ACL design, encryption/key management, independent audit anchor, hardened host, and incident-response operation are not established. |

## Safety invariants to preserve

1. An agent proposal is data, never approval or authorization.
2. `BLOCK` is final; advisory signals can only escalate an otherwise eligible
   operation to `REVIEW`.
3. Approval is human-authenticated, expiring, exact-intent-bound, and consumed
   once.
4. The kill switch blocks new admission/dispatch-marker transactions but does
   not claim to cancel already in-flight provider calls.
5. A timeout or ambiguous provider response is `UNCERTAIN`; do not automatically
   retry. Require trusted, exact-operation reconciliation.
6. Invalid audit data fails closed for verification. Never skip rows,
   re-anchor automatically, or display unverified telemetry as zero activity.
7. Telemetry and anomaly detection do not grant payment authority.

## Evidence and release boundary

## Public evaluator boundary

The Render Blueprint is limited to a Free Docker Web Service with no persistent
disk and no PayPal credentials. `AGENTSENTINEL_PUBLIC_DEMO=true` exposes only
the evaluator UI and bounded scenario-result endpoints. The only allowed
anonymous write is `POST /evaluator/executions`, whose strict schema accepts one
of three server-defined scenario identifiers. Each call is globally rate
limited to 10/minute and executes only fixed local mock tools. All other
non-GET methods are rejected; operator dashboard and approval routes remain
authenticated, and API docs are disabled. The evaluator stores synthetic
execution evidence in ephemeral SQLite. It does not invoke payment adapters or
PayPal. This intentionally public evaluator is not evidence that the payment
control plane is safe for public access.

The approval and evaluator databases can disappear when the Free service
restarts, redeploys, or spins down. No user or payment data should be stored
there. The service has not been deployed from this workspace; see
[Render deployment](RENDER_DEPLOYMENT.md) for limitations and manual steps.

Earlier local verification on 2026-11-10 passed the ML, test, attack, and
heuristic secret-scan checks listed in the historical report. On 2026-11-12,
the full pytest suite passed **501 tests** with one platform-dependent skip;
the Docker image built and its Render-style startup, health, readiness, demo,
Secure-cookie, and mutation-denial paths were smoke-tested. The secret scanner
checked 209 worktree files and 498 reachable history blobs without reporting
high-confidence credential signatures. These are local checks, not penetration
testing, Render deployment, or provider verification. No live PayPal request
was made.

Production deployment is **BLOCKED** until the project has a reviewed process
and identity boundary between agent and payment authority, production-grade
approver authentication, shared rate limiting, a supported persistent payment
store and backup/restore test, independently administered audit anchoring,
provider reconciliation evidence, and successful ML quality gates (or an
explicit decision to exclude ML from payment decisions). The local SQLite
prototype must not be exposed as a production payment service.

## Provider modes and LIVE-disabled adapter boundary

`MOCK` is local-only; `SANDBOX` is pinned to
`https://api-m.sandbox.paypal.com` and requires its existing explicit execution
opt-in. `LIVE` is recognized as a provider mode but is not an executable
payment mode. Its separate offline client uses the fixed
`https://api-m.paypal.com` host for read-only lookups against preloaded
responses. It accepts no caller-supplied transport and its internal response
tape has no network capability. The application supplies no LIVE credentials.
The LIVE payment adapter always denies execution, and the gateway
denies LIVE before adapter dispatch; approval, authorization, recovery, and
reconciliation paths also reject LIVE.

Offline response-tape tests cover LIVE host and OAuth request construction,
rejection of caller-supplied transports, bearer-header use with synthetic
test values, sanitized errors, and read-only response validation. These tests
are not PayPal interoperability evidence.
No LIVE payment operation, Sandbox verification, or production control is
verified by this architecture work.

Before any future LIVE enablement, require a separately approved security
design, production credential and approver identity governance, independently
reviewed payment authorization controls, durable audit and reconciliation
evidence, incident and recovery procedures, and explicit operational
readiness approval. This phase implements no setting or credential combination
that enables LIVE payment execution.

**LIVE adapter architecture implemented does not mean LIVE payment execution
is enabled or production-ready.**
