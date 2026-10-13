# Day 7: manual Sandbox walking skeleton

Verified locally 2026-10-13. No live Sandbox request was made for this course correction. The successful Day 2 payment was a separate REST spike.

```text
Scripted proposal → trusted demo session + interactive user confirmation
                  → expiring, exact-bound authorization in SQLite
                  → payment policy (BLOCK / REVIEW / ALLOW)
                  → atomic operation reservation + one-time claim
                  → protected Sandbox REST adapter → PayPal Sandbox
                  → sanitized operation audit + read-only reconciliation
```

The adapter reuses the Day 2 Sandbox-only client, its USD 0.01–10.00 limit, and distinct stable `PayPal-Request-Id` keys for create and capture. USD amounts above 5.00 remain REVIEW. The operation is `CLAIMED` in SQLite before the HTTP call; ambiguous responses become `UNCERTAIN` and never trigger an automatic POST retry. Create and capture need separate operation IDs and authorizations. Advisory `request_review` can only turn ALLOW into REVIEW; BLOCK wins.

## Manual PowerShell run (USD 1.00)

Run these commands yourself in `D:\AgentObservability` with the existing ignored `.env` configured for Sandbox. The scripts read credentials only when you invoke them; `pytest` uses a spy transport. Choose fresh operation IDs. The prompts require typing `APPROVE` for the exact merchant, action, amount, and target. Do not give this script or `.env` to an agent.

```powershell
Set-Location D:\AgentObservability
python scripts/demo_purchase.py create --operation-id demo_create_001 --amount 1.00
python scripts/demo_purchase.py open-approval --operation-id demo_create_001
```

The first command prints a sanitized `ALLOW`, `COMPLETED`, order ID, and audit state if successful. The second opens a validated Sandbox buyer page without printing its URL. Sign in as the Sandbox buyer and approve manually. Then copy the order ID from the first command into these commands:

```powershell
python scripts/demo_purchase.py capture --operation-id demo_capture_001 --order-id ORDER_ID_FROM_CREATE --amount 1.00
python scripts/demo_purchase.py reconcile --operation-id demo_create_001
python scripts/demo_purchase.py reconcile --operation-id demo_capture_001
```

Capture checks the order is `APPROVED` and bound to the same merchant/USD 1.00 before asking for a separate confirmation. Successful output has a capture ID and `COMPLETED`; reconciliation uses only GET requests and reports order and capture statuses. On `UNCERTAIN`, run `reconcile` and inspect the Sandbox account if an order ID is unknown. **Do not rerun the POST under a new operation ID** until the outcome is verified. The ignored SQLite audit file is `data/paypal_sandbox/gateway_demo.sqlite`.

## Scope and limits

- Real: Sandbox REST adapter, durable local SQLite reservation/claim/audit, exact authorization binding, policy gating, read-only status checks. Offline tests exercise these against a spy transport with zero external calls.
- Demo-only: scripted proposal, one stubbed local user, interactive CLI consent, same-process credential holder, local SQLite file. No real authentication, agent runtime, dashboard, collector payment telemetry, or production permission boundary.
- SQLite protects local duplicate claims but cannot prove exactly-once network execution. An unknown create-order ID cannot be found using only PayPal's order GET; it remains uncertain pending manual provider review. No automated retry, webhooks, or crash-proof provider transaction protocol is claimed.
