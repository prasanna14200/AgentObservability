# Day 3 — PayPal MCP feasibility spike

Verified on **2026-10-12**. This investigation initialized PayPal's hosted Sandbox MCP server and listed tools. It did **not** call any MCP tool, create an order, capture a payment, install software, or change the existing REST client.

## Sources checked

| Official source | URL | Finding |
| --- | --- | --- |
| PayPal MCP server quickstart, updated 2026-06-30 | https://developer.paypal.com/ai-tools/mcp-server | Local Node server requires Node 18+, uses `@paypal/mcp`, `PAYPAL_ACCESS_TOKEN`, and `PAYPAL_ENVIRONMENT=SANDBOX`; hosted Sandbox endpoints `/sse` and `/http` are documented. |
| PayPal published MCP server README | https://github.com/paypal/paypal-mcp-server | Documents local installation, Sandbox configuration, and payment tools including `create_order`, `get_order`, and `pay_order`. |
| Published server package metadata | https://github.com/paypal/paypal-mcp-server/blob/main/package.json | Source version `1.8.1`, Node `>=18`, `@modelcontextprotocol/sdk`, and `@paypal/agent-toolkit`. Registry version at runtime was not independently checked. |
| Published server startup source | https://github.com/paypal/paypal-mcp-server/blob/main/src/index.ts | Parses `--tools=<comma-separated product.action>`; accepts `orders.create`, `orders.get`, `orders.capture`, and other actions. A selected set is passed to the toolkit for that local process. This is source review, not a tested authorization control. |
| MCP Python SDK example | https://github.com/modelcontextprotocol/python-sdk/blob/main/examples/snippets/clients/streamable_basic.py | Python MCP clients can initialize sessions and request `tools/list`. The installed SDK's actual API was checked locally. |

The PayPal documentation and published code establish capabilities offered by the project. The tool names below are from **our own Sandbox discovery response**, not an assumed catalog. No tool behavior was tested through MCP.

## Environment and commands executed

- Repository began clean on `main` at `9251330b6f17464336549741cdac583e44ee5366`; baseline suite: **68 passed**.
- `node --version` → `v22.12.0`; `npm.cmd --version` → `10.9.0`; `python --version` → `3.10.11`; installed Python `mcp` → `1.26.0`.
- Plain `npm` and `npx` PowerShell shims were blocked by this machine's execution policy. `npm.cmd cache ls @paypal/mcp` found no cached package. We did not use `npx -y`, install `@paypal/mcp`, or install any Python package.
- `python scripts/paypal_mcp_discover.py --transport sse` → Sandbox OAuth OK, MCP initialize OK, 46 tool names returned.
- `python scripts/paypal_mcp_discover.py --transport http` → Sandbox OAuth OK, MCP discovery failed with numeric `McpError` code `32600`. The probe suppresses response text, so the underlying cause is **unverified**; we cannot claim HTTP interoperability.

The experimental [discovery script](../../scripts/paypal_mcp_discover.py) reads the already ignored `.env`, obtains an in-memory Sandbox token through the existing REST client's OAuth method, and connects only to `mcp.sandbox.paypal.com`. Its only MCP methods are `initialize` and `list_tools`. It prints tool **names only**: no descriptions, schemas, tokens, exception messages, or response bodies. The installed Python MCP SDK is optional for this script and was not added to project dependencies. Unit tests replace it with fakes.

## Live Sandbox tool discovery

The hosted `/sse` endpoint returned **46 names**:

```text
accept_dispute_claim
activate_recurring_series
cancel_invoice_auto_reminder
cancel_recurring_series
cancel_sent_invoice
cancel_subscription
create_conditional_rules_for_invoice
create_invoice
create_order
create_product
create_recurring_series
create_refund
create_shipment_tracking
create_subscription
create_subscription_plan
delete_invoice
delete_recurring_series
generate_invoice_number
generate_invoice_qr_code
get_dispute
get_invoice
get_merchant_insights
get_order
get_recurring_series
get_refund
get_shipment_tracking
list_disputes
list_invoices
list_products
list_subscription_plans
list_transactions
pay_order
record_payment_for_invoice
record_refund_for_invoice
search_invoicing
send_invoice
send_invoice_reminder
setup_invoice_auto_reminders
show_product_details
show_subscription_details
show_subscription_plan_details
update_invoice_auto_reminder
update_invoicing
update_product
update_shipment_tracking
update_subscription
```

`create_order`, `pay_order`, `create_refund`, invoicing, subscription, and dispute actions can change financial state. Tool discovery alone grants no authority in AgentSentinel. The exact effect and authorization semantics of each MCP tool were **not exercised**. No MCP tool was called.

## Security and architecture decision

The intended payment path is:

```text
AI purchase assistant → AgentSentinel safety gateway → deterministic authorization policy
                      → protected payment adapter → PayPal Sandbox
```

Anomaly signals are advisory. Only trusted application workflows may establish buyer authorization. An agent-generated approval, MCP tool discovery, or model tool selection cannot authorize payment. The agent must not receive the PayPal token, REST credentials, a raw payment tool, or the ability to launch an unguarded MCP server. The future gateway must own operation identity, amount/currency/merchant checks, approval evidence, duplicate prevention, and reconciliation before invoking any adapter.

| Option | Evidence | Consequence |
| --- | --- | --- |
| **A. Existing REST wrapper** | Python-native; Day 2 verified one Sandbox create/approve/capture sequence; explicit request IDs and status reads. | Fits behind the future gateway with no new payment runtime. |
| **B. PayPal MCP** | Hosted Sandbox SSE initialization and Python tool listing worked; local Node server supports source-level `--tools` selection; hosted discovery exposed 46 tools. | Useful as a later optional adapter, but direct agent access could bypass the gateway. A local tool list is not an authorization policy. Remote per-tool restriction was not verified. |

**Recommendation: REST as primary; MCP as an optional adapter only after a separate gateway-controlled design and tests.** Do not connect the AI assistant directly to PayPal MCP. Keep the existing REST client unchanged. The current trace schema/collector covers customer-support tool calls, and the rules/Isolation Forest detect anomalies rather than authorize money movement. Payment-specific schema, protected execution, and policy remain future work requiring approval.

## Limitations and next prerequisites

- The local `@paypal/mcp` package was not installed or launched; `--tools` restriction is supported by published source but not locally tested. No remote tool restriction was established.
- The hosted `/http` transport failed in this probe with MCP error `32600`; root cause remains unverified. Hosted `/sse` worked with Python SDK `1.26.0`.
- Discovery did not exercise any payment tool, idempotency behavior, failure semantics, or token refresh through MCP.
- Existing `.env` remains ignored. The previously rotated, credential-bearing blob remains as an unreachable local Git object; Day 3 does not delete or rewrite Git objects. Secret audit scope and result are reported in the Day 3 handoff.
- Day 4 can proceed only on user approval. Day 3 did not implement the payment schema, gateway, AI purchase assistant, dashboard, or PayPal MCP adapter.
