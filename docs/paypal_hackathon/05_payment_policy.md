# Day 5: deterministic payment policy

Verified: 2026-10-13. This stage evaluates payment eligibility only. It performs no PayPal calls, approval issuance, authorization consumption, or payment execution.

## Decision flow

`src/agentsentinel/payment_policy.py` accepts a validated `PaymentIntent` and an `AuthenticatedScope` supplied by a trusted application entry point. It checks the scope against the intent, the USD-only adapter currency and amount limits, and the supported action. If those hard checks pass, it calls `AuthorizationRepository.find(context, intent_id, operation_id)`. A missing record or any lookup exception produces `BLOCK`. A returned record is checked for exact intent, operation, authenticated scope, merchant, amount, currency, action, and target-order binding. It must be `issued`, already in effect, and unexpired according to the injected trusted clock. **The repository must be application-controlled and must enforce authenticated principal binding.** Day 5 provides only a protocol and a test-local in-memory double; it does not supply a production repository or a way for an agent to create trusted records.

The evaluator has no parameter for a client-supplied `PaymentAuthorization`. An agent can still fabricate a record with `record_origin="application_issued"` and `status="issued"`, but those fields are unverified claims. The test demonstrates that such a record does not help when the trusted repository returns nothing. A dishonest or incorrectly wired repository could still defeat this design; Day 6 must bind the repository and authenticated context inside a protected gateway unavailable to the agent. The real repository/issuance boundary must verify `issuer_id` and record provenance; this policy cannot verify them from a serialized model.

Hard failures produce `BLOCK`. If no hard check fails, amounts strictly above the configured threshold produce `REVIEW`; otherwise the result is `ALLOW`. **BLOCK overrides REVIEW; REVIEW overrides ALLOW.** REVIEW does not permit execution. `PolicyConfig.review_above_usd` defaults to exact `Decimal("5.00")` and must stay within USD 0.01–10.00. The USD 0.01–10.00 bounds match the existing Sandbox REST spike and cannot be raised through policy configuration. EUR and JPY are valid schema currencies but blocked by this policy until an adapter supports them. The policy version is `payment-policy-v1`.

| Example | Result | Safe reason |
| --- | --- | --- |
| Trusted matching issued record, USD 3.00 | ALLOW | `ELIGIBLE` |
| Trusted matching issued record, USD 6.00 | REVIEW | `REVIEW_AMOUNT_THRESHOLD` |
| USD 6.00 with wrong authorized merchant | BLOCK | `AUTHORIZATION_MERCHANT_MISMATCH` |
| No trusted record | BLOCK | `AUTHORIZATION_MISSING` |
| Repository error or timeout | BLOCK | `AUTHORIZATION_LOOKUP_FAILED` |
| EUR request | BLOCK | `UNSUPPORTED_CURRENCY` |

Every `PolicyDecision` carries a stable correlation `decision_id`, operation ID, outcome, allowlisted reason codes, version, aware evaluation time, and fixed `authorization_lookup` audit status (`skipped`, `found`, `missing`, or `error`). The ID is derived from the evaluated request identity, authenticated context, time, and result. It is neither a bearer capability nor a globally unique execution ID; identical reevaluations at the same clock instant can yield the same ID. Audit output contains no raw repository exceptions, credentials, request descriptions, or PayPal data.

## Threats, assumptions, and limits

- An AI-generated authorization object is not passed to the evaluator. `record_origin`, `issuer_id`, and `status` are never treated as cryptographic proof or buyer consent. The trusted repository is the provenance boundary.
- The `AuthenticatedScope` is an input contract, not proof of authentication by itself. A future gateway must create it from a real authenticated application session and keep the repository dependency out of the AI-facing interface. The repository must verify principal/scope ownership and operation binding; the Day 5 test double only checks an explicitly configured fixture context and provides no production security property.
- A record marked `revoked`, `consumed`, or `expired` is blocked. An issued record before `issued_at` or at/after `expires_at` is blocked. If the injected clock is invalid, evaluation raises and issues no ALLOW decision.
- An `ALLOW` result is an eligibility evaluation, **not** a one-time execution capability. This module has no atomic authorization consumption, operation store, replay protection, concurrency control, or double-spend prevention. Day 6 and later reliability work must implement those controls before an adapter can act.
- Existing anomaly rules and Isolation Forest remain unchanged. Their signals can later request REVIEW but cannot authorize a payment or override a BLOCK. The recorded Isolation Forest Gate C failure remains unchanged.
- The schema validates supported actions, and the policy also blocks an unvalidated unsupported action. The current REST adapter and PayPal credentials are untouched.

## Verification and Day 6 prerequisite

Baseline: 113 tests passed. New tests cover ALLOW, REVIEW, BLOCK, each binding dimension, expiry and lifecycle, lookup errors/timeouts, unsupported currencies/actions, threshold boundaries, precedence, safe reasons, a forged agent record, and absence of PayPal transport calls. Final suite and secret-audit results are recorded in the Day 5 handoff.

Day 6 must build a protected pre-execution gateway that owns the authenticated context, injects a trusted authorization repository, rechecks decisions immediately before any payment call, and prevents direct agent access to the REST adapter. This Day 5 module must not be wired directly to agent-controlled dependencies.
