# Day 6: pre-execution safety gateway prototype

Verified: 2026-10-13. This module uses fake adapters only. No PayPal create, capture, or MCP payment call was made. The existing REST client, its stable `PayPal-Request-Id` construction, and the offline detectors are unchanged.

## Boundary and flow

```text
AI payment proposal (PaymentIntent)
  -> trusted application authenticates caller and constructs AuthenticatedScope
  -> PaymentGateway reserves operation atomically
  -> PaymentPolicy reads application-owned AuthorizationRepository
  -> BLOCK / REVIEW stop
  -> ALLOW requires atomic AuthorizationConsumer.claim
  -> marked TestOnlyAdapter in Day 6; future protected adapter after durable controls
```

The gateway accepts an intent and a separate authenticated context. It revalidates both because Pydantic `model_copy` can bypass field validators. It cannot accept an authorization record or credentials as arguments. The policy obtains authorization only from its injected repository. Neither `status="issued"`, `record_origin="application_issued"`, nor an AI assertion creates trust. The real application must construct context from its authenticated session and keep repository, consumer, gateway, and adapter dependencies away from agent-facing code.

**Python classes, private attributes, inheritance, and type hints are not deployment security boundaries.** A process holding PayPal credentials or an unrestricted adapter could bypass this prototype. Real deployment must isolate credentials, restrict service permissions, and prevent direct adapter/MCP access. Day 6 has not established that isolation. A real authorization repository and consumer must authenticate principal ownership, verify issuer/provenance, bind exact request fields, and atomically recheck lifecycle/expiry during consumption. The Day 6 test-local store uses fixed test time and is not a production approval source.

## Interfaces and state

`OperationStore.reserve(operation_id, fingerprint)` must atomically insert `RESERVED` for a new operation, or return the existing state for the same request. A changed request reusing an operation ID yields `CONFLICT`. `finish` atomically changes the matching reservation to a terminal state. The included `InMemoryOperationStore` protects these actions with one process-local lock; it is a test prototype with no persistence, distributed coordination, crash recovery, or multi-process safety.

| State | Meaning | Repeated request |
| --- | --- | --- |
| NEW (implicit) | No operation record yet | Atomic reserve creates `RESERVED` |
| RESERVED | Policy/consumption/adapter may be in progress | Return `IN_PROGRESS`; no second call |
| COMPLETED | Fake adapter returned valid safe completion | Return the stored sanitized result |
| REJECTED | Hard failure or prerequisite unavailable | Return the stored result; no execution |
| REVIEW_REQUIRED | Valid request needs human review | Return the stored result; no execution |
| UNCERTAIN | Fake adapter may have acted, or finalization failed | Return the stored result or `IN_PROGRESS`; reconcile before any further action |

The reservation occurs before policy evaluation so a completed duplicate returns its existing result even after the authorization was consumed. A policy `BLOCK` produces `REJECTED`; `REVIEW` produces `REVIEW_REQUIRED`. Both stop before consumption and adapter execution. `ALLOW` remains an eligibility result, not an execution capability: the gateway then checks that the adapter is explicitly marked `TestOnlyAdapter` and calls `AuthorizationConsumer.claim`. A false or failed claim rejects execution. Only after a successful claim does the fake adapter run. An adapter exception or invalid response marks `UNCERTAIN`; the gateway never retries. A failed finalization after a possible side effect also returns `UNCERTAIN`; the in-memory reservation remains non-retryable. Later reconciliation must determine the provider outcome before any new action.

`GatewayResult` contains only operation ID, the policy decision when one exists, a closed safe status, and allowlisted gateway reason codes. Before a policy decision exists, such as a conflicting or in-progress reservation, `decision` is `null`. Raw adapter responses, exception messages, approval URLs, tokens, and credentials are not returned. The fake adapter returns only `AdapterOutcome(status="COMPLETED")`; all other payloads become uncertain. The existing REST client's create/capture idempotency keys remain separate and unchanged; this gateway does not call that client.

## Threat scenarios and evidence

Tests cover trusted ALLOW with one fake call; BLOCK, REVIEW, missing/fabricated authorization, field/scope/currency mismatches, inactive/expired records, lookup/claim/reservation failures, duplicate completed operations, changed requests reusing an operation ID, two concurrent submissions within one process, fake adapter timeout, malformed/raw adapter output, and refusal of an unmarked adapter. Existing trace, pipeline, PayPal REST, and MCP tests remain part of the full suite. Final counts and the secret-audit scope are recorded in the Day 6 handoff. Gate C's existing Isolation Forest failure is unchanged.

## Limitations and Day 7 prerequisites

The in-memory reservation and test-local authorization consumer are not atomic together, not durable, and not safe across processes or crashes. A crash after reservation can strand an operation; that fails closed but requires reconciliation. A crash after authorization consumption can leave an uncertain outcome. A real execution path requires a durable atomic operation/authorization store, authenticated context construction, trusted issuance/provenance, lifecycle checks at claim time, service-level credential isolation, and reconciliation for uncertain outcomes. Day 7 must not simply swap a real REST client into this prototype. Buyer approval UI, production authorization issuance, AI assistant, dashboard, and MCP payment adapter remain outside Day 6.
