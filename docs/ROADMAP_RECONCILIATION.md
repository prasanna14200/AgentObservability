# AgentSentinel roadmap reconciliation and feature audit

This document reconciles the original roadmap in
[`agent-observability-master-prompt-v9.md`](../agent-observability-master-prompt-v9.md)
with repository code, tests, handoffs, and commands. It keeps three tracks
separate:

- **Master Phase N** means a numbered phase in the original 0–29 roadmap.
- **Hackathon Day N** means the PayPal hackathon implementation track.
- **12-stage checklist** means only the separate checklist in
  [`PROGRESS.md`](paypal_hackathon/PROGRESS.md).

Payment-specific Hackathon Days do not complete general Master Phases. In
particular, **Master Phases 9, 10, and 11 are not complete**. Master Phase 15
(Real-Time Pipeline), the autoencoder experiment, and the optional MCP
demonstration were not started as part of this final-stretch work.

## Master Phase 0–29 status

| Master Phase | Original objective | Evidence-based status |
|---:|---|---|
| 0 | Project definition, competitive positioning, architecture | **PARTIAL** — phase 0 definition and architecture artifacts exist; current competitor claims still require source-by-source refresh before being repeated externally. |
| 1 | Research and source registry | **PARTIAL** — research and a source registry exist; current availability, licenses, and prior-art capability claims were not reverified for this release. |
| 2 | Synthetic trace generation | **COMPLETE** — deterministic synthetic trace generation; current evaluation generates 620 traces. |
| 3 | Trace schema | **COMPLETE** — typed trace/payment schemas and validation tests exist. |
| 4 | Data collection and instrumentation | **PARTIAL** — local collector and payment event instrumentation exist; no general production ingestion deployment. |
| 5 | Preprocessing | **COMPLETE** — preprocessing and regression tests exist; evaluation regenerates processed v4 data. |
| 6 | Contextual feature engineering | **COMPLETE** — contextual features and leakage regression tests exist. |
| 7 | Exploratory data analysis | **COMPLETE** — EDA notebooks and supporting artifacts exist; notebooks are not part of this release's tested demo path. |
| 8 | Classical ML baselines | **COMPLETE** — rules and Isolation Forest run on a held-out synthetic evaluation. This does not imply Gate C passed. |
| 9 | Deep learning autoencoder | **NOT STARTED** — explicitly excluded. |
| 10 | ML vs DL vs rules experiment | **NOT STARTED** — no common-split DL comparison; the existing rules-vs-Isolation-Forest evaluation is not this phase. |
| 11 | Sequence anomaly detection | **NOT STARTED** — explicitly excluded. |
| 12 | Risk engine | **PARTIAL** — payment policy precedence exists; a general calibrated risk engine and comparison of aggregation strategies are not complete. |
| 13 | Safety/guardrail engine | **PARTIAL** — a payment-specific gateway prototype enforces authorization, policy, approval, and kill-switch controls; it is not a general framework-wide safety engine. |
| 14 | Observability middleware | **PARTIAL** — allowlisted payment telemetry and a local audit chain exist; general middleware coverage and production integration are not established. |
| 15 | Real-Time Pipeline | **NOT STARTED** — do not treat Hackathon Days as this phase. |
| 16 | LangGraph integration | **PARTIAL** — a Day 9 LangGraph purchase prototype routes proposals through the payment gateway demo; this is not general agent-framework integration. |
| 17 | MCP integration | **PARTIAL** — a feasibility/discovery spike exists; no optional MCP demo or payment tool execution was implemented here. |
| 18 | Human-in-the-loop | **PARTIAL** — local payment approval workflow exists; no production identity system or external review operation. |
| 19 | Dashboard | **PARTIAL** — authenticated localhost dashboard and decision-reason view exist; no hosted operations console. |
| 20 | Database design | **PARTIAL** — local SQLite stores and migrations/state handling exist; no production PostgreSQL design or deployed database. |
| 21 | Evaluation | **PARTIAL** — offline synthetic evaluations exist, but Gate C fails and the full four-detector comparison is not implemented. |
| 22 | Failure injection | **PARTIAL** — offline failure and attack tests exist; they do not prove provider-level behavior or production resilience. |
| 23 | Security | **PARTIAL** — local security tests, a value-suppressing secret scanner, and documented boundaries exist; no independent security assessment or production security certification. |
| 24 | MLOps | **NOT STARTED** — no model lifecycle, deployment, or drift-monitoring system. |
| 25 | Testing | **PARTIAL** — 331 pytest tests pass after offline fixture generation; this is not production or external-provider integration coverage. |
| 26 | Docker | **NOT STARTED**. |
| 27 | Deployment | **NOT STARTED**. |
| 28 | Optimization | **PARTIAL** — older local offline microbenchmarks are recorded, but the current benchmark script fails during approval setup because the persisted kill-switch row is unavailable. No current p50/p95 result is claimed, and there is no production/load optimization evidence. |
| 29 | Repository structure | **PARTIAL** — package, tests, scripts, docs, and artifacts are organized; a formal completion handoff for this phase is not evidenced. |

### Master gates

| Gate | Result | Evidence / limitation |
|---|---|---|
| A — Data | **PASS for the synthetic baseline** | The corrected evaluation has 620 traces / 1,831 events (500 normal and 120 anomalous). Synthetic labels do not establish real-world prevalence. |
| B — Baselines | **PASS** | Rules and Isolation Forest run on the held-out synthetic split. |
| C — Detector | **FAIL** | Current reproduced rules PR-AUC 0.463 / FPR 0%; Isolation Forest PR-AUC 0.706 / FPR 56.9%. Registered FPR ceiling is 10%. Rules remain the safer operational baseline. |
| D — Runtime | **PARTIAL** | Local payment gateway can block, review, or allow a guarded fake/manual Sandbox operation. No general production runtime deployment. |
| E — Reliability | **PARTIAL** | Offline tests cover local database, kill switch, audit, duplicate, uncertainty, and recovery cases. Provider/network guarantees remain outside local SQLite. |
| F — Demo | **PARTIAL** | Side-by-side poisoned-proposal demo and 22-case offline attack suite run. The complete original normal/loop/cost/exfiltration demo set is not shown as a single end-to-end user workflow. |

## Hackathon feature audit

“Demo-critical” identifies functionality or limitations that must be clear for
an honest demonstration; “optional” means it is not required for the basic
offline protected-vs-unprotected demo. These are not claims of production
readiness.

| # | Feature | Status | Priority | Verified scope and limitation |
|---:|---|---|---|---|
| 1 | Human approval queue, approve/reject/expiry | **DONE** | Demo-critical | Durable local queue, exact request binding, authenticated approver/CSRF, expiry, and atomic authorization issuance are tested. |
| 2 | Kill switch | **DONE** | Demo-critical | Fail-closed local control; it blocks new admissions. It cannot cancel an operation already admitted/in flight. |
| 3 | Hash-chained audit log and verification | **DONE** | Demo-critical | Local SQLite SHA-256 chain verifies new payment telemetry; tamper-evident, not tamper-proof or externally anchored. |
| 4 | Payment telemetry | **PARTIAL** | Optional | Allowlisted event schema, collector, publisher, and separate audit chain exist. Publisher wiring is optional; not every gateway deployment emits it. |
| 5 | Advisory detector integration | **PARTIAL** | Optional | Rules/Isolation Forest adapters and an injectable advisor can request REVIEW. Wiring is optional; signals cannot relax BLOCK or authorize payments. |
| 6 | Dashboard and “why decided” panel | **DONE** | Demo-critical | Local authenticated dashboard shows stored decision and safe reason codes; no public hosting or production identity provider. |
| 7 | Attack suite | **DONE** | Demo-critical | 22/22 offline spy scenarios pass; no PayPal calls. Scenarios are controlled simulations, not real-world coverage. |
| 8 | Side-by-side demo | **DONE** | Demo-critical | Poisoned proposal moves fake USD 9.00 on the unprotected mock and USD 0.00 through AgentSentinel, which blocks it. |
| 9 | Real LLM mode | **DONE (LOCAL ONLY)** | Optional | Five Ollama runs using the already installed `qwen2.5:1.5b` model were blocked; no hosted model support or general robustness claim. Model downloads are not part of setup. |
| 10 | Day 14 guard and quickstart | **DONE** | Demo-critical | Guard delegates through `TrustedGatewayEntryPoint`; docs explicitly state same-process Python trust limitations. |
| 11 | CI, tests, and secret scanning | **PARTIAL** | Demo-critical | Local pytest and secret scan pass. GitHub Actions has no configured workflows/runs; do not claim hosted CI. |
| 12 | Latency p50/p95 | **PARTIAL** | Optional | Historical offline local measurements are documented, but a fresh run of the current script fails during approval setup; no current reproducible p50/p95 claim is made. |
| 13 | Failure injection | **PARTIAL** | Demo-critical | Local injected DB, adapter uncertainty, concurrency, kill-switch, and recovery cases are covered; external provider failure semantics remain unverified. |
| 14 | Order A documentation | **DONE (UNRESOLVED OUTCOME)** | Demo-critical | Reconciliation record reports later GET 404; the provider outcome remains unresolved and is not represented as a successful capture. |

## Hackathon Day evidence

The current handoffs classify Hackathon Day 9 as an offline purchase-agent
prototype, Day 10 as local human approval and kill-switch controls, Day 11 as
payment observability/advisory detectors, Day 12 as a localhost dashboard, Day
13 as local failure-injection/reliability work, and Day 14 as guard integration
and crash recovery. Those labels do not imply completion of the similarly
numbered Master Phases or entries in the 12-stage checklist.

The recorded Day 9 five-trial local Ollama experiment followed poisoned catalog
text in all five proposals; the gateway blocked each with
`AUTHORIZATION_MERCHANT_MISMATCH`, and fake money moved was USD 0.00. The
side-by-side scripted demo has the same block outcome. Neither is evidence of
general prompt-injection resistance.

## Hackathon baseline and submission-period changes

The `pre-hackathon-baseline` tag points to
`d75579747ac42e5cf6960b2c00f1fdcc4408b43d`, the pre-period baseline recorded in
[`hackathon_scope.md`](hackathon_scope.md). The scope record
states the submission period began 2026-10-01 09:15 Pacific / 16:15 UTC and
ends 2026-11-12 at 12:00 Pacific Time; the official rules were last checked
there on 2026-10-12, so recheck current rules before submitting.

The baseline already contained offline synthetic traces, schemas, collection,
preprocessing, features, EDA, rules, Isolation Forest, evaluation notebooks,
tests, and model cards. Post-baseline work added payment contracts and policy,
the gateway and Sandbox adapter, purchase agent, approval queue and kill
switch, payment telemetry and audit chain, dashboard, failure/recovery
handling, and Day 14 guard integration. The hackathon scope record and actual
commits/handoffs are the provenance source; the presence of these
payment-specific additions does not mean the later general Master Phases are
complete.

## Release evidence and limitations

- Fresh clone from public `origin/main`, isolated Python 3.10.11 environment,
  and install with `.[dev,agent,day10]` succeeded. No `.env` was present.
- Running `scripts/evaluate_offline.py` first generated the ignored fixture
  required by one test. Then full pytest passed **331 tests** with one
  third-party deprecation warning; the mock demo passed.
- The offline attack suite passed **22/22**; the value-suppressing secret scan
  reported no high-confidence credential signatures or credential-shaped
  historical filenames.
- The Day 13 report contains its historical offline latency measurements. A
  final-stretch run of the current benchmark script failed with
  `RuntimeError: kill switch state unavailable` during approval setup; the
  historical measurements were not presented as a fresh reproduction.
- `LICENSE` is tracked; `.gitignore` covers `.env`, build outputs, processed
  data, and `src/agentsentinel.egg-info/`. Five previously tracked generated
  egg-info files were removed from the worktree and are included in the
  cleanup; no notebook or generated dataset is part of the intended change.
- No live PayPal call, new model download, or credential exposure is part of
  these verification steps.
- The repository is a local prototype. Day 7 Order A remains unresolved,
  Gate C fails, the detector advisor and telemetry publisher are optional, no
  hosted CI is configured, and the audit chain has no external anchor.
- Public submission still requires the entrant to create and publish a
  sub-three-minute video and complete any required Devpost form fields.
