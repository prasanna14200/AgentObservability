# Day 12 — AgentSentinel Security Dashboard

## Architecture

The local AgentSentinel FastAPI application now serves a Jinja2 Control Center. It reads the existing approval/operation SQLite database and Day 11 payment telemetry chain through SQLite read-only connections. It does not create dashboard-specific payment state or act as a payment gateway.

```text
Browser
  ├─ login with trusted operator or optional viewer credential
  ├─ short-lived signed HttpOnly session + double-submit CSRF token
  ▼
Existing local FastAPI application
  ├─ read-only overview / trace / alert queries ── fixed local SQLite sources
  ├─ read-only audit verification ── PaymentAuditChain.open_readonly()
  └─ operator-only forms ── existing ApprovalStore and kill-switch methods
```

The dashboard never displays merchant-purpose text, raw provider payloads, access tokens, API keys, authorization secrets, or prompt content. Jinja autoescaping is enabled for all rendered values. SQL statements are fixed, use bound parameters, and cap results: 50 traces per page, 500 events per trace, 1,000 alert-source events, 500 approval rows, and a bounded 512 KiB / 1,000-line failure-log tail. No request parameter can select a database or filesystem path.

## Routes and authentication

| Route | Access | Behavior |
|---|---|---|
| `GET /dashboard/login` | Public | Issues a pre-auth CSRF cookie and login form |
| `POST /dashboard/login` | CSRF required | Accepts `APPROVER_SECRET` as operator or optional `DASHBOARD_VIEWER_SECRET` as read-only viewer |
| `POST /dashboard/logout` | Authenticated + CSRF | Clears the signed session |
| `GET /dashboard` | Viewer or approver | Persisted overview and current live audit verification |
| `GET /dashboard/traces` | Viewer or approver | Searchable 1/7/30-day trace table with bounded pagination |
| `GET /dashboard/traces/{trace_id}` | Viewer or approver | Correlated chronological timeline |
| `GET /dashboard/alerts` | Viewer or approver | Deterministic enforcement, operational, and advisory alerts |
| `GET /dashboard/approvals` | Viewer or approver | Read-only queue for pending, approved, rejected, and expired requests |
| `POST /dashboard/approvals/{approval_id}/approve` | Approver + CSRF + confirmation | Rechecks record, exact stored fingerprint, expiry, and kill-switch state, then calls the existing atomic approval store |
| `POST /dashboard/approvals/{approval_id}/reject` | Approver + CSRF + confirmation | Rechecks record, exact stored fingerprint, expiry, and readable kill-switch state before rejection |
| `POST /dashboard/killswitch` | Approver + CSRF + confirmation | Uses the existing kill-switch store method and its audit event |
| `POST /dashboard/audit/verify` | Approver + CSRF + confirmation | Runs live read-only chain verification |
| `GET /dashboard/demo` | Viewer or approver | Deterministic synthetic walkthrough, isolated from persisted metrics |
| `GET /dashboard/static/dashboard.css` | Static asset | Serves the local stylesheet only |

The approver session is signed with the configured approver secret and expires after one hour. Cookies are HttpOnly and SameSite=Strict; the CSRF token is checked against its cookie for every browser POST, including login and logout. The optional viewer credential is disabled unless `DASHBOARD_VIEWER_SECRET` is configured with at least 32 characters. Viewers can inspect but cannot approve/reject, change the kill switch, or invoke manual audit verification. Existing JSON approval endpoints retain their bearer authentication and CSRF requirements.

## Data sources and display semantics

- **Operations, approvals, and kill switch:** the existing local database selected by `APPROVAL_DB_PATH` (default `data/paypal_sandbox/gateway_demo.sqlite`). Queries use read-only connections. A pending approval whose expiry timestamp has passed is displayed as expired without mutating its stored row.
- **Decisions and provider outcomes:** `payment_audit_chain` in `data/collected/payment_telemetry.sqlite`, using `POLICY_EVALUATED`, `PAYMENT_COMPLETED`, and `PAYMENT_UNCERTAIN` events. Decision and outcome cards cover the last 24 hours; total operations covers all persisted operations.
- **Trace explorer and alerts:** the same allowlisted Day 11 canonical telemetry events. Trace search is limited to 1, 7, or 30 days.
- **Telemetry ingestion failures:** the recent bounded tail of `data/collected/rejected/payment_telemetry.jsonl`, filtered to 24 hours. If that file does not exist, the dashboard shows “No data,” not zero.
- **Audit integrity:** a live check through `PaymentAuditChain.open_readonly()`. The page states the verification time and that the result is not persisted. No prior verification time is claimed because no stored verification-result record exists.

Missing tables, files, or relevant events are presented as “No data” where appropriate. A missing kill-switch row is identified as the gateway's default-disabled state, not as a persisted transition. SQLite/read failures do not enable operator controls or change gateway behavior.

Advisory alerts are explicitly separated from deterministic enforcement. Rules advisories are computed on a bounded set of recent traces; stored detector-version/score events are displayed when present. No runtime payment Isolation Forest model is persisted, so the dashboard does not invent an Isolation Forest result for persisted traffic. Advisory scores never change policy decisions or authorize execution.

## Safe offline demo walkthrough

Open **Offline demo** from the navigation. It is labeled `OFFLINE DEMO`, uses seed `20261028`, generates synthetic events on request, and keeps demo values separate from the persisted overview. It does not import data, create authorizations, change the persistent kill switch, instantiate a payment gateway, or call PayPal. The synthetic Isolation Forest is fit in memory from generated normal traces only; it is illustrative, not a production model.

| Requested walkthrough | Dashboard evidence | Limitation |
|---|---|---|
| Normal authorized mock payment | Synthetic normal completed-event trace | It is not an actual authorization-backed mock execution. No authorization is created and no mock request is executed. |
| Merchant mismatch blocked | Synthetic `AUTHORIZATION_MERCHANT_MISMATCH` BLOCK trace | Offline event illustration only |
| Amount mismatch blocked | Synthetic `AUTHORIZATION_AMOUNT_MISMATCH` BLOCK trace | Offline event illustration only |
| Human approval required | Synthetic REVIEW, `APPROVAL_REQUESTED`, and expiry events | No persistent approval is created; it is not shown as a live pending queue entry |
| Kill switch enabled and subsequent action blocked | Synthetic enable → `KILL_SWITCH_ACTIVE` BLOCK event sequence | Persistent switch is not touched |
| Uncertain provider outcome | Synthetic `PAYMENT_UNCERTAIN` trace | No provider request is made |
| Advisory anomaly | Existing rules and payment Isolation Forest adapters applied to synthetic repeat-dispatch events | Synthetic-only advisory, not fraud evidence |
| Audit verification | A temporary synthetic hash chain is verified and discarded | Does not verify or alter the persisted chain |

## Security boundaries

- The dashboard is not a payment gateway and has no provider client or execution adapter.
- Approval actions require the existing trusted approver credential, an approver-role session, CSRF token, explicit confirmation, exact request fingerprint, current pending state, unexpired request, and a fresh kill-switch read. The existing atomic approval store issues the application authorization; the payment gateway still enforces policy, budgets, and authorization consumption.
- Rejection also requires exact fingerprint, current state, expiry, and a readable kill-switch control record. A rejection remains allowed while the switch is enabled because rejection does not authorize payment; a failed state read blocks the action.
- Kill-switch transitions use the existing persisted control and audit event path. A read or write failure returns an error and does not present a success response.
- Audit verification is read-only and tamper-evident, not tamper-proof. External anchoring, recovery from a privileged local administrator, and detection of a valid-tail truncation are not implemented.
- The default module launch binds to `127.0.0.1:8000`. Do not expose it through a public interface, reverse proxy, or tunnel during Day 12.

## Launch commands (PowerShell)

Set secrets through a local secret manager or secure shell environment; do not commit them or paste them into source files. For an interactive masked prompt:

```powershell
$env:APPROVER_SECRET = [System.Net.NetworkCredential]::new('', (Read-Host 'Approver secret (32+ characters)' -AsSecureString)).Password
# Optional read-only login:
$env:DASHBOARD_VIEWER_SECRET = [System.Net.NetworkCredential]::new('', (Read-Host 'Viewer secret (32+ characters)' -AsSecureString)).Password
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m agentsentinel.approval_service
```

Browse to `http://127.0.0.1:8000/dashboard/login`. The default database remains the existing local approval/gateway database; no live PayPal configuration is required.

## Validation evidence and known limitations

Validation on 2026-10-29: the full pytest suite collected and passed **265 tests**, including the dashboard authentication, viewer/operator separation, CSRF, empty stores, persisted metric counts, pagination/filtering, timeline correlation, escaping, secret non-disclosure, fingerprint/expiry, approval actions, kill-switch audit, audit validity/tampering, alert classification, and offline demo isolation. The offline attack suite passed **22/22** scenarios. The value-suppressing secret scan checked 171 worktree files and 267 reachable history blobs and reported no high-confidence credential signatures or credential-shaped historical filenames. The audit verifier passed against an isolated temporary database (**0 records**, because no test chain was supplied to that CLI run); dashboard tests separately verified live valid and tampered chains. `git diff --check` passed. No browser screenshot was captured; automated route and rendered-HTML tests are the available local UI evidence.

The existing Day 11 synthetic payment evaluation remains separate from customer-support Gate C, which remains FAIL at 56.9% Isolation Forest FPR against a registered 10% ceiling. The local dashboard does not provide production authentication, deployment hardening, external audit anchoring, or a calibrated runtime payment model. It does not claim production PayPal execution.
