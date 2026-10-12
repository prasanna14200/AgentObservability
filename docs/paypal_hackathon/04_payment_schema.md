# Day 4: payment data contracts

Verified: 2026-10-12. Scope: schema and tests only. The existing Sandbox REST client, customer-support traces, collector, synthetic data, detectors, Gate C results, and MCP discovery script are unchanged.

## Models

`src/agentsentinel/schema/payment.py` defines three separate, immutable Pydantic v2 records and a `Money` value object:

| Model | Fields | Meaning |
| --- | --- | --- |
| `Money` | `amount`, `currency` | Exact positive `Decimal`; explicit USD/EUR two-decimal and JPY zero-decimal allowlist. |
| `PaymentIntent` | `intent_id`, `operation_id`, `trace_id`, `scope_id`, `money`, `merchant_id`, `purpose`, `action`, optional `target_order_id`, `requested_at` | An untrusted proposal. `capture_order` needs a target order; `create_order` forbids one. `scope_id` is a claimed correlation value until a trusted workflow resolves it. |
| `PaymentAuthorization` | `authorization_id`, `issuer_id`, `scope_id`, `intent_id`, `operation_id`, `merchant_id`, `money`, `action`, optional `target_order_id`, `issued_at`, `expires_at`, `status`, optional `closed_at`, `record_origin` | Shape of an application-issued record. Its content is only a claim when deserialized from arbitrary input. `binds_to()` compares request fields; it does not confer permission. |
| `PaymentExecutionMetadata` | `operation_id`, `provider`, `attempt_id`, `status`, `observed_at`, optional provider order/capture IDs, optional allowlisted failure category/code | Sanitized operation telemetry. It has no provider response body, approval URL, instrument, token, or credentials. |

Identifiers are bounded ASCII strings; merchant identifiers additionally allow `:`. The purpose is at most 160 printable characters and rejects URLs, email addresses, and common credential markers. All timestamps require timezone information. Unknown fields and arbitrary nested payloads are rejected. Validation errors suppress input values in their string form. **Do not log raw requests or `ValidationError.errors()`**: Pydantic's structured errors can contain raw input. The bounded purpose filter is a defense in depth measure, not a guarantee against every possible secret; callers must sanitize at ingestion.

Amounts reject floats, nonfinite values, zero/negative values, scientific-notation strings, values over 999,999,999, and excessive currency precision. Decimal strings round-trip through JSON without binary float conversion. This is a schema allowlist, not a claim that the existing adapter supports EUR or JPY: the Day 2 Sandbox adapter remains USD-only with its own limits. Currency precision follows the [PayPal currency codes reference](https://developer.paypal.com/api/codes/currency/), checked 2026-10-12; the schema deliberately supports only USD, EUR, and JPY for now.

Status fields use closed sets. An open authorization has no `closed_at`; a revoked, consumed, or expired one needs a closing time. Expiry must follow issuance. A completed capture needs a capture ID; pre-capture statuses cannot have one. An uncertain order-create outcome may lack an order ID; capture uncertainty requires one. Failure/uncertain states require a safe failure category, and reason codes are allowlisted.

## Trust boundary

```text
AI proposed PaymentIntent
  -> future safety gateway
  -> future deterministic policy obtains PaymentAuthorization from a trusted server-side repository
  -> future protected REST adapter
  -> PayPal Sandbox
```

**Parsing a `PaymentAuthorization` never verifies its issuer, record origin, buyer consent, or authenticity.** An agent can fabricate a structurally valid record, including `record_origin="application_issued"` and `status="issued"`; the test demonstrates this. Those fields are assertions in data, not evidence. Future policy must retrieve an authorization through an authenticated application-controlled repository, bind it to the exact intent, operation, scope, merchant, amount, currency, action, and target order, then check lifecycle and expiry using a trusted clock. It must reject any record supplied by an agent or other untrusted request body. No trusted issuance, signature, database, or permission-checking method exists in Day 4.

Sanitized examples (illustrative IDs, no real transaction):

```json
{"intent_id":"intent_001","operation_id":"operation_001","trace_id":"trace_001","scope_id":"scope_001","money":{"amount":"12.34","currency":"USD"},"merchant_id":"merchant_001","purpose":"Notebook","action":"create_order","target_order_id":null,"requested_at":"2026-10-12T10:00:00Z"}
{"authorization_id":"authorization_001","issuer_id":"issuer_001","scope_id":"scope_001","intent_id":"intent_001","operation_id":"operation_001","merchant_id":"merchant_001","money":{"amount":"12.34","currency":"USD"},"action":"create_order","target_order_id":null,"issued_at":"2026-10-12T10:00:00Z","expires_at":"2026-10-12T10:05:00Z","status":"issued","closed_at":null,"record_origin":"application_issued"}
```

These examples are data only and cannot authorize a payment.

## Compatibility and deferred work

The existing `TraceRecord` still describes the three customer-support tools and does not include payment fields. Payment telemetry can later be correlated via `trace_id` and `operation_id` after a deliberate collector design review; no trace or collector changes were made. The REST client remains primary. MCP remains an optional future adapter behind the same gateway. The Isolation Forest Gate C failure remains unchanged.

Day 5 prerequisites: define trusted authorization issuance/retrieval, scope authentication, trusted-clock expiry checks, lifecycle transitions, exact binding and deterministic policy decisions. Later work must add a gateway and protected execution with idempotency/reconciliation. No payment policy, gateway, execution, PayPal API call, assistant, approval UI, dashboard, database, or detector change was made here.

## Verification

Baseline before Day 4: 70 tests passed. Day 4 adds parametrized test cases covering valid and invalid amounts, currencies, precision, IDs, actions, timestamps, expiry and lifecycle consistency, binding mismatches, extra fields, sensitive marker rejection, safe error strings, execution states, JSON round trips, and separation from legacy traces. Final suite and secret-audit results are recorded in the Day 4 handoff.
