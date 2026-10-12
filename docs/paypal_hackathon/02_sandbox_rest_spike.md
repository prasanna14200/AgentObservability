# Day 2 — PayPal Sandbox REST spike

Verified 2026-10-12 against PayPal's [REST setup](https://developer.paypal.com/api/get-started/), [Orders flow](https://developer.paypal.com/api/rest/integration/orders-api), [request headers and idempotency](https://developer.paypal.com/api/make-api-requests/), and [Payments v2 capture lookup](https://developer.paypal.com/api/payments/v2). This is isolated manual tooling. It is not Stage 3's payment schema, policy engine, safety gateway, or an agent-accessible payment tool.

## Result and safety status

- **Real Sandbox result:** the user manually created one Sandbox order, opened the browser approval flow, and captured after reporting the order as `APPROVED`. The user reported capture `COMPLETED` and reconciliation order/capture `COMPLETED`. A later **read-only** API check independently returned `COMPLETED` for the order and capture, with identifiers matching the local journal. The buyer's browser action and the earlier `APPROVED` state were user-reported, not independently observed by this review.
- **Mock:** the full flow and failures were exercised with a fake HTTP transport, including exact method/path/body/header construction, pending approval, capture, capture status, failed responses, and uncertain timeouts.
- **Security recovery:** local edits had put credential-shaped values and a commented Sandbox login in tracked `.env.example`. Those bytes were copied to ignored `.env`, and `.env.example` was restored to placeholders. No such values entered a `main` commit in this work. The affected blob was `416bf3828ce19946013d9154e2ece93f8b3c7bee` at `.env.example` in the local snapshot ref `refs/codex/turn-diffs/captures/1791783156100/79ea5d41-514b-4c2e-bc6c-f963abf95d71/base`. On reinspection, that ref had already disappeared; it was **not manually deleted**, and the blob is not reachable from any current ref, branch, or remote-tracking ref. The blob still exists as a local unreachable object. The user confirmed the exposed REST app secret was rotated and disabled and the affected Sandbox account password was reset. Do not paste credential values in chat, issues, logs, or commits.
- **Audit scope:** `scripts/scan_secrets.py` checks ordinary worktree files and all blobs reachable from current Git refs, including local Codex snapshot refs. It verifies `.env` is ignored and deliberately does not inspect its expected local credential contents. It does not scan unreachable objects or guarantee absence of other secrets. The re-run scanned 101 worktree files and 147 reachable blobs with no high-confidence findings. The old blob still exists in local Git storage, so this clean reachable-history result must not be described as total erasure.

## Design

`src/agentsentinel/paypal_sandbox.py` uses Python's standard `urllib` only. The base URL is fixed to `https://api-m.sandbox.paypal.com`; production cannot be configured. OAuth exchanges the Sandbox REST app client ID and secret with `POST /v1/oauth2/token` using HTTP Basic and `grant_type=client_credentials`. Subsequent calls send the resulting bearer token in memory. Exceptions and command output contain only bounded status and identifier fields; raw PayPal bodies, tokens, payer information, and approval URLs are not printed or logged.

The spike accepts **USD 0.01–10.00** as a two-decimal string and converts it through `Decimal`. It requires an explicit Sandbox merchant ID, uses that ID as the create-order payee, and checks the fetched order's merchant, amount, currency, and `APPROVED` state before a capture POST. This is a narrow demo limit, not a general payment currency implementation. The order details endpoint is `GET /v2/checkout/orders/{order_id}`. The post-capture status endpoint is `GET /v2/payments/captures/{capture_id}`; the capture ID comes from `purchase_units[].payments.captures[]`.

`PayPal-Request-Id` is derived deterministically from merchant ID, operation type, and caller-supplied operation ID. Create and capture use distinct IDs. The ignored `data/paypal_sandbox/operations.json` records operations before POST and prevents duplicate CLI requests after a timeout, crash, or uncertain response. Reconcile with a GET before taking any further action. This local file is a single-user spike journal, not a concurrent or durable transaction store; PayPal's idempotency retention is finite. Do not delete it to force a retry.

## Windows PowerShell setup

1. Sign in to [PayPal Developer](https://developer.paypal.com/api/get-started/) and confirm a Sandbox business merchant and personal buyer in [Sandbox Accounts](https://developer.paypal.com/sandbox-testing/accounts). Verify sign-in to each in separate browser sessions.
2. Under **Apps & Credentials → Sandbox**, copy the REST app client ID and secret. Find the Sandbox business account's merchant ID in its account details. Use credentials from the same merchant app/account.
3. From `D:\AgentObservability`, run `Copy-Item .env.example .env` only if `.env` does not already exist. Edit **`.env` only**. Set `PAYPAL_ENVIRONMENT=sandbox`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, and `PAYPAL_MERCHANT_ID`. The merchant ID is the business account ID, not its email. Do not put the buyer password into this file. Confirm `git check-ignore .env` lists `.env`.
4. Install the project using the existing repository setup instructions, or set `$env:PYTHONPATH = 'src'` for direct imports. The command below runs from the repository root and needs no extra HTTP package.

## Explicit real Sandbox smoke test (run manually after setup)

Each command is a separate, intentional action. The test suite never runs them. Keep the journal file between commands. Use fresh operation IDs for a new purchase; the same ID refers to the same operation.

```powershell
python scripts/paypal_sandbox_spike.py create --operation-id demo-order-001 --amount 1.00
python scripts/paypal_sandbox_spike.py show --operation-id demo-order-001
python scripts/paypal_sandbox_spike.py open-approval --operation-id demo-order-001
```

`open-approval` opens a validated Sandbox PayPal URL in the default browser without displaying its approval token. **The human buyer** signs in with the Sandbox personal account and explicitly approves the order. The script cannot approve it. After the buyer returns, check the state:

```powershell
python scripts/paypal_sandbox_spike.py show --operation-id demo-order-001
```

Only after the command shows `APPROVED`, intentionally run the capture command:

```powershell
python scripts/paypal_sandbox_spike.py capture --operation-id demo-order-001 --capture-operation-id demo-capture-001
python scripts/paypal_sandbox_spike.py reconcile --operation-id demo-order-001
```

`reconcile` fetches the order and, when a capture ID exists, calls Payments v2 to show the capture's actual status. A timeout or uncertain capture result stops the command and leaves a journal entry. Never rerun the capture POST blindly; use `reconcile` and inspect the Sandbox merchant activity. If the order remains `APPROVED` with no capture after an uncertain response, the spike intentionally does not decide whether another capture is safe. Later stages need a durable, concurrent operation state machine and an explicit recovery policy.

## Actual verification

Baseline before changes: 49 tests passed. The user manually ran the Sandbox sequence on 2026-10-12 and reported `APPROVED` before capture, `COMPLETED` capture, then `COMPLETED` order and capture on reconciliation. The ignored local journal has one create record with an order ID and one capture record in `RESPONSE_RECEIVED` state with a capture ID linked to that order. It records no status history or buyer action. A separate read-only `GET /v2/checkout/orders/{id}` and `GET /v2/payments/captures/{id}` returned `COMPLETED` for both. The response order/capture IDs matched the journal; the order's embedded capture ID, merchant ID, USD currency, and amount also matched. No raw API response, payment identifier, approval URL, or credential value was recorded in this report. The user-confirmed browser approval and transient pre-capture `APPROVED` state were not independently observed.

The user confirmed the exposed Sandbox REST app secret was rotated and disabled, the affected Sandbox account password was reset, and the replacement secret was placed in ignored `.env`. Local checks confirmed `.env` is ignored, all four required variable names are present, and `SandboxConfig.from_values` accepts the configuration without printing values. Final test and audit results are recorded in the Day 2 handoff. The old credential-bearing blob remains unreachable in local Git storage; no destructive cleanup was done.

## Next stage boundary

Stage 3 requires payment-specific schema and telemetry rules, separate from this spike. This one manual Sandbox transaction proves the REST path can work, but it does not validate concurrency, durable recovery, repeated transactions, production behavior, or payment policy. No runtime interception, payment policy, gateway, PayPal MCP, or AI-agent credential access was added here.
