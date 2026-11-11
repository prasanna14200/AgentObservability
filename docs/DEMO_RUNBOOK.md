# AgentSentinel offline demo runbook

This is the safe, reproducible demo path. It uses synthetic data, temporary
SQLite stores, local fakes, and a spy transport. It does **not** make a PayPal
Sandbox request, require `.env`, expose credentials, or download a model.

## 1. Fresh checkout and setup (PowerShell)

Requires Git and Python 3.10 or newer:

```powershell
git clone https://github.com/prasanna14200/AgentObservability.git
Set-Location AgentObservability
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,agent,day10]"
```

The three optional groups provide test tools, the LangGraph/Ollama agent
dependencies, and Day 10 FastAPI/Uvicorn/Jinja2 dependencies. No `.env` is
needed for the following commands. `scripts/evaluate_offline.py` creates
ignored processed data required by one full-suite feature test; run it before
pytest.

The verified public clone was at commit `60382aa` (the then-current
`origin/main`) and therefore contained the older README; this local
final-stretch README/runbook supplies the tested install command. Fresh-clone
verification initially omitted the `day10` extra and pytest collection failed
because `fastapi` was not installed. Installing the declared `dev`, `agent`,
and `day10` groups resolved that import failure. The clean clone also
confirmed that the evaluation command must precede pytest to create
`data\processed\v4\traces.parquet`; no generated fixture is tracked.

## 2. Run the offline baseline and tests

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_offline.py
.\.venv\Scripts\python.exe -m pytest -o addopts= -q
```

Observed clean-clone output on Python 3.10.11: evaluation exit 0, 620 traces /
1,831 events, and `"gate_c_pass": false`; pytest exit 0 with **331 passed** and
one Starlette/AnyIO deprecation warning. The evaluated customer-support
Isolation Forest has PR-AUC 0.706 and FPR 56.9%, above the preregistered 10%
ceiling; rules have PR-AUC 0.463 and FPR 0%. Gate C is a failure, not a
successful detector gate.

Versions recorded in that isolated environment: AgentSentinel 0.1.0, pytest
9.2.0, Pydantic 2.14.3, pandas 2.3.3, scikit-learn 1.7.2, PyArrow 25.0.1,
LangGraph 1.2.15, Ollama Python client 0.6.3, FastAPI 0.115.14, Uvicorn
0.32.0, and Jinja2 3.1.6. Dependency ranges and pinned optional extras are
declared in `pyproject.toml`; a future resolution may select newer compatible
versions for the unpinned base dependencies.

That FastAPI version is historical environment evidence. The current
`pyproject.toml` and `requirements-container.lock` pin FastAPI 0.135.0; this
work did not verify 0.115.14 in a clean environment or change either pin.

Latest checkout validation (not a fresh-clone test) used Python 3.10.11,
FastAPI 0.135.0, and Uvicorn 0.32.0. The full suite passed **489 tests** with
one directory-symlink test skipped because symlink creation was unavailable;
the offline attack suite passed **22/22** with zero PayPal calls. The
value-suppressing heuristic scan checked 205 worktree files and 472 reachable
history blobs and found no high-confidence credential signatures. No clean
environment test was performed against FastAPI 0.115.14.

## 3. Run security scenarios and comparison

```powershell
.\.venv\Scripts\python.exe scripts\run_attack_suite.py
.\.venv\Scripts\python.exe scripts\demo_side_by_side.py
```

Observed results: attack suite **22/22 passed**, zero PayPal calls. The demo
prints `CONTROLLED SIMULATION - no PayPal transaction or real money` and:

| Path | Decision | Status | Reason | Mock money moved |
|---|---|---|---|---:|
| Unprotected mock | ALLOW | COMPLETED | UNPROTECTED_MOCK_ONLY | USD 9.00 |
| AgentSentinel | BLOCK | REJECTED | AUTHORIZATION_MERCHANT_MISMATCH | USD 0.00 |

The unprotected route is intentionally confined to the comparison script and
accepts only its fake adapter. The protected path uses the trusted gateway and
the authorization for the original demo intent. This is a controlled
simulation, not a payment or a measure of real-world attack success.

The attack suite covers authorization tampering/replay/expiry, duplicate and
uncertain outcomes, limits and budgets, prompt injection, approval fingerprint
and kill-switch conditions, advisory precedence, telemetry failure, and audit
tampering. Spy transport count is zero for each attack attempt.

## 4. Local dashboard and seeded mock status

The existing authenticated `/dashboard/demo` page is a separate synthetic
walkthrough. To view it without using the default Sandbox database, start the
service with a newly named temporary approval database:

```powershell
$env:APPROVAL_DB_PATH = Join-Path $env:TEMP ("agentsentinel-dashboard-" + [guid]::NewGuid().ToString('N') + ".sqlite")
.\.venv\Scripts\python.exe run.py
```

Use the hidden-input approver prompt, sign in at
`http://127.0.0.1:8000/dashboard/login`, then open
`http://127.0.0.1:8000/dashboard/demo`. The temporary database is retained;
the command does not use or seed the existing Sandbox database.

**Paused:** the demo runner fails closed until the Windows demo-data security
boundary can be independently verified. An existing directory, static
symlink/reparse-point checks, or operator assertion is not sufficient. There
is currently no supported configuration that enables seeding. Invoking the
runner without `--reset` exits before directory creation, database
initialization, audit writes, payment operations, credential generation, or
service startup. `--reset` also fails closed without filesystem mutation.

```powershell
.\.venv\Scripts\python.exe scripts\run_demo_dashboard.py
```

No directory permission configuration currently overrides this fail-closed
decision. Do not use the script to seed the dashboard until a safe Windows
directory boundary is independently established and the runner is explicitly
updated to support it.
The dashboard's existing `/dashboard/demo` synthetic walkthrough remains
separate and unchanged in behavior.

## 5. Optional local LLM run

Only run if Ollama and the model are already installed locally. Do not run
`ollama pull` as part of this workflow. The script has a preflight check and
does not download models:

```powershell
.\.venv\Scripts\python.exe scripts\run_local_llm_trials.py --model qwen2.5:1.5b
```

Recorded result: five proposals followed the poisoned catalog instruction;
all five were blocked by `AUTHORIZATION_MERCHANT_MISMATCH`, and fake money
moved was USD 0.00. The run logged token counts only, not prompt or output
text. This one-model, five-trial observation is not a general security claim.

## 6. Optional local checks

```powershell
.\.venv\Scripts\python.exe scripts\scan_secrets.py
.\.venv\Scripts\python.exe scripts\measure_day13_reliability.py
```

The scanner is a heuristic supplemental check over worktree files and
reachable Git blobs; it does not prove that no secret exists. The latency
script is intended to use temporary SQLite and fake adapters, but the current
run fails during approval setup with `RuntimeError: kill switch state
unavailable`. The Day 13 report contains historical p50/p95 measurements; do
not describe them as freshly reproduced or as production performance.

## PayPal Sandbox exclusion

Do not run `scripts/demo_purchase.py` during the offline demo. That is a
separate manually operated Sandbox flow. The source implements PayPal REST
OAuth, Orders create/read/capture, and capture read endpoints; enabling it
requires trusted Sandbox configuration and explicit manual buyer approval.
Order A's provider outcome remains unresolved after later read-only GET
responses returned 404. Order B is a separate capture that was independently
verified. See [Day 7 operations](paypal_hackathon/07_end_to_end_gateway.md)
and [Order A troubleshooting](paypal_hackathon/07_sandbox_404_troubleshooting.md).

## Security and interpretation notes

- The agent proposes; trusted application code supplies authenticated context,
  authorization, policy, the approval store, and the adapter.
- Payment telemetry and detector advisory wiring are optional. Detector
  signals may escalate an allowed action to REVIEW; they cannot authorize or
  override BLOCK.
- The Day 14 decorator is not a sandbox. Same-process Python code must be
  trusted; private-object introspection is not prevented. Never provide
  provider adapters or credentials to untrusted agent code.
- The kill switch blocks new admissions. It cannot cancel a request already
  admitted and in flight.
- Local SQLite/audit behavior does not prove exactly-once execution at the
  external PayPal provider.
