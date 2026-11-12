# Configuration and deployment boundary

AgentSentinel currently has local-prototype configuration, not a production
deployment profile. The application does not load a general `.env` file
automatically. Supply variables to the process or use the explicitly
documented local Sandbox command that reads the ignored `.env` file.

## Variables used by the current code

| Variable | Consumer | Required / default | Security boundary |
|---|---|---|---|
| `APPROVER_SECRET` | Approval API and dashboard session signing | Required for authentication; minimum 32 characters. `run.py` uses this process value or prompts twice with hidden input for local startup. | Do not put a real secret in source control, command history, or logs. Use an approved secret manager before hosted deployment. |
| `DASHBOARD_VIEWER_SECRET` | Optional read-only dashboard login | Optional; if configured, must be at least 32 characters to grant viewer login. | Viewer role cannot perform approval or kill-switch mutations. This is not an identity-provider integration. |
| `APPROVAL_DB_PATH` | `create_app()` local SQLite path | Optional; defaults to `data/paypal_sandbox/gateway_demo.sqlite`. | Prototype-only local storage; not approved as a production payment database. |
| `AGENTSENTINEL_PUBLIC_DEMO` | Render mock-only evaluator | Optional; defaults to `false`. | When `true`, `/evaluator` and its fixed scenario/result endpoints are public; all other writes are denied and API documentation is disabled. |
| `AGENTSENTINEL_SECURE_COOKIES` | Dashboard cookie configuration | Optional; defaults to `false` for local HTTP. | Set to `true` behind HTTPS; required by the public demo mode. |
| `AGENTSENTINEL_EVALUATOR_DB_PATH` | Public evaluator execution evidence | Optional; defaults beside the approval DB. | SQLite store for predefined synthetic executions; set under ephemeral storage for Render. |
| `PAYPAL_ENVIRONMENT` | Payment gateway and manual Sandbox CLI | Optional; defaults to `MOCK`. Set to `MOCK`, `SANDBOX`, or `LIVE`. | The gateway never infers a mode from credentials. The manual Sandbox CLI requires explicit `SANDBOX`; Sandbox credentials also require that mode, plus the existing execution opt-in. `LIVE` is recognized but disabled. No mode falls back to another. |
| `PAYPAL_CLIENT_ID` | Manual Sandbox CLI configuration | Required together with the other Sandbox settings for a manually run Sandbox operation. | Sandbox credential only; never provide it to agent code or commit it. |
| `PAYPAL_CLIENT_SECRET` | Manual Sandbox CLI configuration | Required together with the other Sandbox settings for a manually run Sandbox operation. | Sandbox credential only; never provide it to agent code or commit it. |
| `PAYPAL_MERCHANT_ID` | Manual Sandbox CLI configuration | Required; validated as a bounded merchant identifier. | Must match the explicitly configured Sandbox merchant. |
| `OLLAMA_MODEL` | Optional local purchase-agent experiment | Optional; default `qwen2.5:1.5b`. | Requires an already installed local model. The project does not download one. |

Provider mode is selected once from `PAYPAL_ENVIRONMENT`. MOCK uses only
trusted test adapters and makes no provider request. SANDBOX uses the fixed
`https://api-m.sandbox.paypal.com` host and still requires the separate
application execution opt-in. The offline-only LIVE status client uses the
fixed `https://api-m.paypal.com` host and a built-in response tape; it accepts
no caller-supplied transport and cannot make network requests. It supports
only a read-only order lookup against preloaded test responses. The LIVE payment adapter always raises
`LIVE_EXECUTION_DISABLED`; the gateway rejects LIVE adapters before dispatch,
and no environment variable enables execution.

Sandbox credentials use `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` only
when the selected mode is `SANDBOX`. The offline LIVE client accepts explicit
configuration objects for fake-transport tests only; it does not read
environment variables, and no LIVE credentials are required by the
application. Do not copy Sandbox credentials into a future LIVE configuration.

The manual `scripts/paypal_sandbox_spike.py` keeps its execution journal at
`data/paypal_sandbox/operations.json`. Its `reconcile` command verifies the
order and optional capture with read-only Sandbox requests, then stores a
row in the separate `sandbox_reconciliation_evidence` table in the
application's `APPROVAL_DB_PATH` SQLite file (default:
`data/paypal_sandbox/gateway_demo.sqlite`). These records are post-facto
evidence only; they do not create or alter gateway operations,
authorizations, policy decisions, or dispatch evidence. The authenticated
`GET /dashboard/api/transactions` endpoint reads both gateway operations and
manual evidence (displayed with source `manual_sandbox_evidence`) from that
configured SQLite file. The overview refreshes this
bounded list and its counts/totals on load, on request, and every 15 seconds.
Manual Sandbox evidence is visibly distinguished from gateway operations.

Offline tests exercise the fixed LIVE host, OAuth request construction,
bearer-header handling with synthetic credentials and tokens, rejected
transport injection, sanitized failures, and read-only response filtering.
They do not establish PayPal
LIVE or Sandbox interoperability, payment execution, or production readiness.
Any future LIVE enablement would require a separately approved design,
independent security review, production credential governance, operational
authorization and monitoring, tested reconciliation and incident procedures,
and an explicit authorization change. No enablement switch is implemented.

**LIVE adapter architecture implemented does not mean LIVE payment execution
is enabled or production-ready.**

`PAYPAL_ACCESS_TOKEN` is not a documented user setting; the Sandbox client
obtains tokens from its configured Sandbox credentials. `run.py` removes
PayPal-related variables from the dashboard child process. It never prints
the approver secret. The service listens only on `127.0.0.1:8000`.

The service provides `/health` as a process liveness response. `/ready`
returns `200 {"status":"ready"}` only when the approval-store kill-switch row
is readable and a dashboard approver secret is configured with at least 32
characters; otherwise it returns `503 {"detail":"service not ready"}`. The
response does not expose authentication configuration details. A viewer
secret is optional and is not part of readiness. `/ready` does not validate
telemetry, PayPal connectivity, or a hosted database, and is not sufficient
production readiness evidence.

## Safe local use

The offline demo needs no `.env`, PayPal credential, Ollama server, or model
download. Follow the [offline demo runbook](DEMO_RUNBOOK.md). For Sandbox CLI
experiments, use the manual instructions in
[`07_end_to_end_gateway.md`](paypal_hackathon/07_end_to_end_gateway.md) only
after separately authorizing Sandbox use. The spike reconciliation command
also performs read-only Sandbox GET requests and updates only the separate
manual-evidence table:

```powershell
python scripts/paypal_sandbox_spike.py reconcile --operation-id <operation-id>
```

No Sandbox action is part of the default test or demo path.

## Local Docker profile

`compose.yaml` is a loopback-only SQLite demonstration profile. It uses
`requirements-container.lock` with package hashes, a digest-pinned
Python 3.12 AMD64 base image, a non-root user, a read-only container
filesystem, a read-only Docker secret, and a named volume mounted at
`/app/data`. Generate `secrets/approver_secret` using the README PowerShell
instructions; `secrets/` is Git-ignored. The container does not receive PayPal
configuration.

This profile does not use PostgreSQL and does not provide backups. The Docker
image build and Render-style container startup were verified locally; Compose
was not independently run. Review
[`POSTGRESQL_MIGRATION_PLAN.md`](POSTGRESQL_MIGRATION_PLAN.md) before treating
any container profile as a production deployment.

## Hosted mock-only evaluator

The optional Render configuration is a public **mock-only evaluator**, not a
hosted payment control plane. It uses a generated internal `APPROVER_SECRET`,
sets `AGENTSENTINEL_PUBLIC_DEMO=true` and
`AGENTSENTINEL_SECURE_COOKIES=true`, and places the approval SQLite file under
`/tmp`. The evaluator at `/evaluator` executes only fixed synthetic scenarios;
`POST /evaluator/executions` is the only allowed public write and stores
execution evidence in a separate SQLite file under `/tmp`. Other dashboard and
approval routes remain authenticated and their write methods are denied in
public mode. No PayPal credentials are needed or configured. See
[Render deployment](RENDER_DEPLOYMENT.md).

Render's Free service filesystem is ephemeral; the database is reinitialized
after a restart, deploy, or spin-down. The demo has no persistence requirement.
The Free service is intended for evaluation and may sleep when idle. The
repository configuration does not create a persistent disk or paid service.

This does not establish production deployment controls, including:

- HTTPS termination and `Secure` session cookies (the local dashboard sets
  `secure=False` by default for its localhost HTTP workflow; the Render mock
  profile explicitly enables Secure cookies).
- Production identity, key rotation, or centralized secret management.
- Shared authentication rate limiting or resource controls.
- An approved persistent payment datastore, migration/backup policy, or
  tested consistent backup/restore of SQLite data and the audit sidecar.
- Process isolation between untrusted agent code and payment authority.
- A remote independently administered audit checkpoint.
- Hosted health/readiness checks for the full dependency set. Local `/ready`
  checks only the approval database's kill-switch state; `/health` remains a
  process-level liveness response.

Do not expose the general dashboard or deploy the SQLite prototype as a
production payment service. Only the restricted synthetic walkthrough is
intended for public evaluator access.
