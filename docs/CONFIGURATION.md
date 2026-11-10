# Configuration and deployment boundary

AgentSentinel currently has local-prototype configuration, not a production
deployment profile. The application does not load a general `.env` file
automatically. Supply variables to the process or use the explicitly
documented local Sandbox command that reads the ignored `.env` file.

## Variables used by the current code

| Variable | Consumer | Required / default | Security boundary |
|---|---|---|---|
| `APPROVER_SECRET` | Approval API and dashboard session signing | Required for configured approval authentication; minimum 32 characters. `run.py` generates a temporary local value for its own dashboard process. | Do not put a real secret in source control, command history, or agent context. Use an approved secret manager before any hosted deployment. |
| `DASHBOARD_VIEWER_SECRET` | Optional read-only dashboard login | Optional; if configured, must be at least 32 characters to grant viewer login. | Viewer role cannot perform approval or kill-switch mutations. This is not an identity-provider integration. |
| `APPROVAL_DB_PATH` | `create_app()` local SQLite path | Optional; defaults to `data/paypal_sandbox/gateway_demo.sqlite`. | Prototype-only local storage; not approved as a production payment database. |
| `PAYPAL_ENVIRONMENT` | Manual Sandbox CLI configuration | Must be exactly `sandbox` when using the Sandbox CLI. | The client rejects live mode. Offline tests and demos do not use PayPal transport. |
| `PAYPAL_CLIENT_ID` | Manual Sandbox CLI configuration | Required together with the other Sandbox settings for a manually run Sandbox operation. | Sandbox credential only; never provide it to agent code or commit it. |
| `PAYPAL_CLIENT_SECRET` | Manual Sandbox CLI configuration | Required together with the other Sandbox settings for a manually run Sandbox operation. | Sandbox credential only; never provide it to agent code or commit it. |
| `PAYPAL_MERCHANT_ID` | Manual Sandbox CLI configuration | Required; validated as a bounded merchant identifier. | Must match the explicitly configured Sandbox merchant. |
| `OLLAMA_MODEL` | Optional local purchase-agent experiment | Optional; default `qwen2.5:1.5b`. | Requires an already installed local model. The project does not download one. |

`PAYPAL_ACCESS_TOKEN` is not a documented user setting; the Sandbox client
obtains tokens from its configured Sandbox credentials. `run.py` explicitly
removes PayPal-related variables from its dashboard child process.

## Safe local use

The offline demo needs no `.env`, PayPal credential, Ollama server, or model
download. Follow the [offline demo runbook](DEMO_RUNBOOK.md). For Sandbox CLI
experiments, use the manual instructions in
[`07_end_to_end_gateway.md`](paypal_hackathon/07_end_to_end_gateway.md) only
after separately authorizing Sandbox use. No Sandbox action is part of the
default test or demo path.

## Production configuration status

No production deployment configuration is provided. In particular, this
repository does not currently establish:

- HTTPS termination and `Secure` session cookies (the local dashboard sets
  `secure=False` for its localhost HTTP workflow).
- Production identity, key rotation, or centralized secret management.
- Shared authentication rate limiting or resource controls.
- An approved persistent payment datastore, migration/backup policy, or
  tested consistent backup/restore of SQLite data and the audit sidecar.
- Process isolation between untrusted agent code and payment authority.
- A remote independently administered audit checkpoint.
- A health/readiness contract that checks dependencies and fails accurately
  when the payment store is unavailable. The current `/health` endpoint only
  returns a process-level `ok`.

Do not expose the current service publicly or deploy its SQLite prototype as a
production payment service. A deployment profile should follow—not precede—a
reviewed trust-boundary and storage design.
