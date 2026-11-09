# AgentSentinel demo video script (under 3 minutes)

**Target length:** about 2 minutes 40 seconds. Record the offline mock
demonstration only. Do not show credentials, `.env`, a real buyer account, a
real payment, or a live PayPal Sandbox operation. This is a script, not
evidence that a video has been recorded or published.

| Time | Screen / action | Narration |
|---|---|---|
| 0:00–0:18 | Title and architecture diagram | “AgentSentinel is a production-shaped prototype for separating AI-agent payment proposals from payment authority. This demo uses only local fakes; it does not move real money or call PayPal.” |
| 0:18–0:40 | Show agent proposal and static catalog source, with no secrets | “The agent can propose an action, but it does not receive payment credentials or issue authorization. Catalog text is untrusted input. The trusted application supplies authenticated context and the validated intent.” |
| 0:40–1:15 | Run `.\.venv\Scripts\python.exe scripts\demo_side_by_side.py` | “The same poisoned proposal is sent through two controlled mock paths. The unprotected mock accepts it and records fake USD 9.00. AgentSentinel compares it with the original exact-bound authorization and blocks the merchant mismatch; fake money moved is zero.” |
| 1:15–1:40 | Show approval UI/dashboard decision reason or test output | “For actions that require review, the local approval workflow binds the human decision to the exact stored request. The kill switch fails closed for new admissions. It does not cancel an operation that was already admitted and in flight.” |
| 1:40–2:02 | Show audit verification and recovery documentation | “Payment telemetry is allowlisted and stored in a local hash chain. Recovery uses trusted evidence and does not automatically resubmit an uncertain provider operation. The local chain is tamper-evident, not externally anchored or tamper-proof.” |
| 2:02–2:24 | Show test and attack-suite summaries | “The local suite has 331 passing tests and 22 of 22 offline attack scenarios. These are controlled local tests, not a security certification or proof of external provider behavior.” |
| 2:24–2:40 | Show evaluation result and limitations | “The separate customer-support Isolation Forest experiment fails its preregistered false-positive ceiling: 56.9% versus 10%. We keep that detector advisory and do not use it to authorize payments. Order A also remains unresolved after provider GET responses returned 404.” |

## Commands for the recording

Run from the repository root in PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts\demo_side_by_side.py
.\.venv\Scripts\python.exe scripts\run_attack_suite.py
```

The first prints the controlled comparison table shown in the runbook. The
second should report **22/22 passed**. Do not record `scripts/demo_purchase.py`
or a Sandbox browser session as part of this offline walkthrough.
