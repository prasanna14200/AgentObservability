# Day 7 Sandbox order retrieval troubleshooting

Checked 2026-10-13. This note records evidence for the existing create operation; it does not authorize a retry or a capture.

## What the states mean

The gateway marks a **create operation** `COMPLETED` after the Sandbox create-order response is accepted and its order ID is stored. That is completion of the `create_order` action only. Buyer approval and capture are separate states and require separate evidence. The CLI now reports `order_creation_state`, `buyer_approval_state`, and `capture_state` separately. A provider GET 404 leaves the local create acknowledgement intact and the later buyer/capture states unverified.

## Evidence for the existing order

- Read-only inspection of the ignored SQLite file found `RESERVED → CLAIMED → COMPLETED` for `demo_create_20261013a`, with a recorded attempt, idempotency key, and provider order ID. The create operation finished at 2026-10-13 05:01:29 UTC. The CLI did not persist PayPal's HTTP 201 or the initial GET response body.
- The user reports create HTTP 201 and an immediate GET HTTP 200. That GET observation was not stored, so it is user-reported evidence.
- Later user-run `open-approval` and `reconcile` received `GET_HTTP_404` for the recorded order ID. No provider order status could be verified from those calls.
- The attempted capture command used a literal placeholder order ID and stopped at GET 404 before issuance. The local database has **no** `demo_capture_20261013a` operation or authorization. There is no local evidence of buyer approval or capture.

PayPal documents Orders v2 GET at `/v2/checkout/orders/{id}` and says a 404 can mean an incorrect/missing resource **or insufficient caller access**. Its documentation also says an order normally remains `CREATED` for a limited window, but it does **not** establish that this 404 was caused by expiration. The original app/account relationship, current provider state, and reason for the 404 remain unverified. Sources checked 2026-10-13: [Show order details](https://developer.paypal.com/sdk/orders/v2/orders-get/), [Resource not found](https://developer.paypal.com/api/errors/resource-not-found), [Orders API use cases](https://developer.paypal.com/api/rest/integration/orders-api/api-use-cases/advanced/).

## Read-only diagnosis and manual recovery

Run only this command for the existing operation. It opens SQLite in read-only mode and makes no payment POST. The REST client may POST to OAuth to obtain an access token, then uses GET for the order and any known capture. It prints only local phase labels, merchant match as a boolean, HTTP status, safe provider status, and bounded error codes. It never prints credentials, approval links, or raw responses.

```powershell
Set-Location D:\AgentObservability
python scripts/demo_purchase.py diagnose --operation-id demo_create_20261013a
```

If the order remains inaccessible, privately verify that the `.env` Sandbox REST app belongs to the same Sandbox business account as the configured merchant ID. Check the Sandbox merchant activity for this order and any capture; if inconclusive, ask PayPal Sandbox support to resolve the resource or permission state. Do not paste credentials, access tokens, approval URLs, or raw provider responses into chat. Preserve the old SQLite record and operation ID. Do not replay its create or issue a capture against an unverified order.

A **fresh independent** USD 1.00 Sandbox demo is safe only after the old order and any capture are accounted for in the Sandbox merchant account or by PayPal support; the configured app and merchant match; the environment is Sandbox; and fresh create and capture operation IDs are chosen. For the new order, require an immediate successful GET with matching merchant/USD amount, a validated approval link, manual buyer approval, a subsequent `APPROVED` GET, and a new exact-bound capture authorization. Any missing or ambiguous check stops the flow. Never clear the old database or reuse the old operation ID to force a retry.

## Offline verification and security hold

The full suite passed **215 tests** on 2026-10-14. The new fake-client tests cover a successful create followed by GET 404, an initial GET 200 followed by GET 404, separate approval/capture states, and a diagnostic command that does not open a writable operation store. The implementation tests made no live PayPal request; subsequent read-only GETs are recorded below.

The pre-existing modified `.env.example` contained a real PayPal client-secret assignment and account login comments. It was replaced locally with a placeholder-only template. A comparison performed without printing values shows that the current ignored `.env` secret differs from the exposed blob. On 2026-10-14, the user confirmed the exposed client secret was disabled and the affected Sandbox password reset; PayPal-side rotation cannot be independently verified here. The affected blob still exists as an unreachable local Git object; current reachable history no longer includes it. The full heuristic audit passed on 2026-10-14 (127 worktree files, 195 reachable history blobs), but cannot prove complete erasure from local Git storage. No Git reference was deleted by this troubleshooting work.

## Subsequent read-only order check

On 2026-10-14, a sanitized PayPal GET for the original create operation still returned HTTP 404. A separate local create operation, `demo_create_20261013b`, has an order ID beginning with the browser checkout token prefix reported by the user. Before capture, its PayPal GET returned HTTP 200 and `APPROVED` with the expected USD amount and merchant binding and no capture. The full browser token was not available for exact comparison, and the buyer's browser action was not independently observed.

At 2026-10-13 17:18 UTC, SQLite recorded one `RESERVED` -> `CLAIMED` -> `COMPLETED` capture operation, `demo_capture_20261013b`, with a consumed exact-bound authorization and one saved capture ID. On 2026-10-14, read-only PayPal order GET returned `COMPLETED` and listed that capture once. A separate capture GET returned `COMPLETED`; capture amount, currency, merchant, and order association matched the local operation. This independently verifies the capture outcome. Repeating the same CLI command returned the stored result according to the user. The one local claim, one listed capture, and duplicate-path code support no second payment execution, but no wire-level HTTP log proves the exact number of POST attempts. The original order's provider outcome remains unresolved and requires merchant-account history or PayPal support review.
