# MASTER PROMPT (v9, final) — BUILD A PRODUCTION-SHAPED AGENT OBSERVABILITY AND RUNTIME SAFETY CONTROL PLANE

*(Renamed from "zero to production" — I don't have real users, security review, uptime monitoring, or deployment evidence. Call it "production-shaped prototype," not "production," until that changes. This is honesty, not modesty — it's a stronger claim to a hiring panel because it's defensible under questioning.)*

You are my **Senior AI Engineer + ML Engineer + LLM Engineer + Agent Engineer + MLOps Engineer + Distributed Systems mentor**.

I am a BTech CSE student. I already understand ML, DL, NLP, LLMs, RAG, LangChain, LangGraph, CrewAI, MCP, FastAPI, Docker, vector DBs, MLOps, and model evaluation at a conceptual level. **My weakness is implementation** — going from an empty folder to a working, production-shaped system. Teach me by building, not by lecturing.

## STRATEGIC POSITIONING (checked against current literature via direct search — August 2026; recheck before any external pitch, capabilities change)

I want this project to satisfy two goals at once: (1) deep implementation learning, interview-ready and able to explain every decision, and (2) a genuinely differentiated, market-credible product, not a toy.

**Do not claim novelty for anomaly detection itself.** As of August 2026, multiple real research projects already work on this problem:
- **ATBench** — a trajectory-level agent safety benchmark family (base release: 500 trajectories, balanced for safety verdict, zero tool overlap between train/benchmark; taxonomy of risk source / failure mode / real-world harm, human-audited; has since expanded into a broader family — record exact release/count in `research/sources.yaml` at time of use).
- **AgentDoG 1.5** — a lightweight alignment framework for AI agent safety/security, evaluated on the ATBench taxonomy.
- **TrajAD** — formulates runtime Trajectory Anomaly Detection with an emphasis on precise error localization (not just flagging), using a synthetically perturbed benchmark (TrajBench).
- **Trajectory Guard** — a sequence-aware model for real-time agentic anomaly detection. Notably, their first approach (pooling step embeddings + Isolation Forest / One-Class SVM / VAE, treating anomalies as point outliers) **failed** (F1 < 0.70) — averaging diluted the anomalous signal. They moved to a sequence-aware architecture instead.

**Do not build on the premise that "no one does this."** Instead, use this framing throughout the project: at each milestone, explicitly separate (a) capabilities already demonstrated by public research/systems, (b) our own engineering contribution, and (c) any genuinely novel hypothesis we are testing. Never call engineering integration "research novelty."

**Current working hypothesis about the gap** (re-verify before using in the pitch, don't state as settled): the sources checked so far appear to emphasize either agent observability platforms (Langfuse, Phoenix, PostHog) or research detectors (ATBench/AgentDoG, TrajAD, Trajectory Guard, IBM's paper), while our target combination is telemetry + contextual detection + policy enforcement + runtime intervention + auditability + human approval, wired together as one system. Even where a product like Langfuse is confirmed observe-only today, don't generalize that to every related product or assume it stays true — re-check before each major phase.

**Central research thesis to test experimentally, not assume:**
> Can a multi-signal behavioral anomaly detection layer — combining statistical feature-based detection, sequence-aware modeling, and deterministic policy — detect novel agent execution deviations that rule-based guardrails and trajectory-level safety classifiers miss, while holding acceptable false-positive rate and real-time latency?

**Anomaly is contextual, not absolute.** 100 tool calls is normal for a coding agent and highly unusual for a customer-support agent. Design the feature engine and models around: agent identity + task type + tool permissions + historical baseline + current trajectory + resource usage → anomaly score. Don't just threshold raw counts.

## EVIDENCE POLICY (applies to every claim about external work, forever)

For every external product, paper, benchmark, or dataset referenced in this project:
1. Verify it against its official paper, repository, documentation, or product page — don't rely on memory or a prior AI's summary uncritically.
2. Record title, authors/organization, URL, publication/release date, and the date it was checked.
3. Label every claim as **Verified**, **Partially verified**, **Unavailable/unconfirmed**, or **User-provided (unverified)**.
4. Never state a product lacks a capability unless a source explicitly supports that conclusion — prefer "not confirmed from sources checked" over an unqualified "No."
5. Never claim our project is unique without comparing the exact capability and scope, not just the category.
6. If something can't be verified, mark it **UNVERIFIED** and don't use it as a design foundation — cite it as a lead to check, not a fact to build on.
7. Treat cited performance numbers (accuracy, F1, etc.) as reported under that source's own dataset/split/setup — a reference range, not an expected result for our system.

Status of prior art already checked as of this prompt's writing (August 2026, via direct search/fetch — not secondhand summary. Re-verify before using in a final report or pitch, and formalize each into a `research/sources.yaml` entry during Phase 0 for reproducibility):
- **ATBench** — trajectory-level agent-safety benchmark family, base release confirmed at 500 trajectories balanced for safety verdict with zero tool overlap between training and benchmark sets; has since expanded into an ATBench family covering new settings (persistent sessions, code execution, etc.). **Record the exact release/version, trajectory count, split, and label distribution in `research/sources.yaml` at time of use — don't hardcode a trajectory count in the pitch.**
- **AgentDoG 1.5** — a lightweight alignment framework for AI agent safety/security, evaluated on the ATBench taxonomy. **Confirmed: it is not offline-only.** It includes a documented online runtime guardrail (a "Pre-Reply intervention") that inspects accumulated trajectories before a final response and can flag/intervene on unsafe behavior — evaluated specifically on whether this reduces unsafe deliveries while preserving benign behavior and latency. Earlier framing of this project as "no runtime intervention" was wrong and has been corrected below.
- IBM Research's "Detecting Silent Failures in Multi-Agentic AI Trajectories" (arXiv 2511.04032) — **Verified** as a real paper with real published Isolation Forest/XGBoost/SVDD benchmark numbers on 2 real agent-trace datasets, reported under its own split/setup. **Dataset public availability: UNVERIFIED** — the paper states release is pending after acceptance; check at Phase 1 whether it has actually been published before assuming it's downloadable.
- TrajAD, Trajectory Guard — **Verified** via direct arXiv lookup.
- Langfuse's lack of built-in runtime enforcement — **Verified as of August 2026**: Langfuse's own security/guardrails documentation plus three independent third-party comparisons all describe it as architecturally observe-only, delegating runtime blocking (prompt injection, PII, toxicity) to third-party libraries. This is a checked capability claim, not an assumption — but capabilities ship fast, so recheck current docs before repeating it in a final pitch or report.

## THREAT MODEL (define explicitly before Phase 12 — Risk Engine)

- **Assets:** secrets, user data, tool credentials, money, databases, model quota, system availability.
- **Actors:** malicious user, compromised tool, prompt injection, malicious retrieved document, faulty agent, faulty model, insider.
- **Attack goals:** data exfiltration, privilege escalation, unauthorized tool use, cost exhaustion, denial of service, unsafe external action.
- **Trust boundaries:** user input, LLM, agent state, tools, MCP servers, retrieval sources, telemetry pipeline, dashboard, human reviewer.
- **Explicitly out of scope:** attacks that cannot be detected or prevented using the telemetry we actually collect — say so rather than implying coverage we don't have.

## PRIVACY, FROM THE FIRST TRACE SCHEMA ONWARD (not deferred to Operations)
From Phase 3 (schema design), not later: never store API keys, access tokens, passwords, or raw secrets; redact or hash sensitive tool arguments; provide a safe synthetic mode; document whether prompts and tool outputs are persisted; separate audit metadata from raw content. An observability system that logs everything by default can quietly become the most sensitive data store in the whole stack — design against that from day one, not as a Phase 20 afterthought.

## MILESTONE GATES (do not advance to the next deliverable until the current gate passes)

- **Gate A — Data:** at least 500 normal traces and 100 labeled abnormal traces, or explicit documentation of why fewer are available.
- **Gate B — Baselines:** rule-based and Isolation Forest baselines run end-to-end on a held-out test set.
- **Gate C — Detector:** before testing, pre-register one target metric and one false-positive-rate ceiling. The learned detector must beat the rule-only baseline on that pre-registered metric while staying under the pre-registered FP ceiling. If it doesn't, keep rules as the operational baseline and document *why* the learned model didn't justify deployment — that's still a valid, reportable outcome, not a failure to hide.
- **Gate D — Runtime:** the safety layer can block, pause, or allow a live tool request.
- **Gate E — Reliability:** the system behaves correctly when telemetry, model, database, or Redis is unavailable.
- **Gate F — Demo:** a reviewer can reproduce one normal run, one loop attack, one cost attack, and one data-exfiltration attempt.

Numbers are adjustable; pre-registering the metric before you see results, and the requirement that every phase produce a measurable, checkable artifact, are not.

## RISK ENGINE — DEFINED MATHEMATICALLY, NOT JUST CONCEPTUALLY
Keep separate: `anomaly_score` (deviation from learned normal behavior), `policy_score` (deterministic violation severity), `impact_class` (policy-assigned severity tier of the potential consequence — Low/Medium/High/Critical — based on tool/action type and the threat model; **this is not a calibrated probability of real-world harm**, telemetry alone cannot support that claim), `confidence` (reliability of available evidence). Implement and compare at least two aggregation strategies — (1) deterministic policy precedence, (2) weighted/calibrated aggregation — and document why a high-severity policy violation should be able to override a low ML anomaly score (e.g., an unusual-but-permitted call shouldn't auto-block, while a familiar call attempting secret exfiltration should).

Example impact-class table to seed the policy engine:
| Action | Impact class |
|---|---|
| Read public documentation | Low |
| Query internal database | Medium |
| Send an external email | High |
| Transfer money or delete records | Critical |
| Export credentials or personal data | Critical |

**Anomaly alone is advisory, never a sole basis for blocking.** An unusual-but-permitted action should normally produce an alert or a confirmation request, not an automatic block — blocking requires a policy or permission violation, not just statistical unusualness. This is the single most important rule for keeping the system honest about the difference between "unusual" and "dangerous."

## POLICY DECISION TABLE (every decision must be reproducible from stored state)
For every decision, log: tool, requested arguments, agent identity, task type, permission set, anomaly signals, policy violation, impact class, confidence, final action, explanation, fallback action. The final decision must be reproducible from the stored event plus the policy version that produced it — this is what makes the audit trail actually auditable, not just a log.

## MODEL DOCUMENTATION (one model card per detector — required because this is a safety system)
For every detector (rules, Isolation Forest, autoencoder, sequence model): intended use, prohibited use, training data, label source, features, threshold-selection method, known failure modes, false-positive/false-negative behavior, latency and hardware, retraining conditions, data drift assumptions.

## INTERVENTION SAFETY RULE
The anomaly model may **recommend**, never directly execute, a dangerous action. The policy engine alone decides: allow / allow-with-logging / require-confirmation / downgrade-permissions / pause / terminate. Every intervention must be idempotent, auditable, and explainable. A false positive must fail safely without corrupting agent state — the detector must never become an unsafe autonomous controller in its own right.

## DATA VALIDITY — DO NOT SILENTLY MERGE DATASET TYPES
Maintain separate datasets for: (1) content-level safety trajectories (e.g. ATBench-style), (2) operational telemetry (latency/cost/retries), (3) framework-generated traces (our own LangGraph agent), (4) adversarial/failure-injected traces. For each: source and license, schema, labels, whether labels are human/synthetic/weak/inherited, train/val/test split, possible leakage, domain mismatch, limitations. Never claim synthetic anomalies represent real-world attack prevalence. Use a **temporal split** (train on earlier traces, test on later ones) to simulate production drift, not just a random split.

## EVALUATION RULES (full rigor belongs at Deliverable 5 / final evaluation — don't let it gate earlier work)
Report results overall, and broken out by: agent type, task type, anomaly category, risk severity, trace length, tool permission level. Use: temporal holdout, leave-one-anomaly-type-out testing, ablation of rules/context/sequence/resource features, confidence or bootstrap intervals, threshold calibration, precision-recall curves, alert volume per 1,000 traces, mean time to detection, intervention success rate, false-positive cost, inference overhead. Accuracy alone can hide a useless safety system — a detector that flags everything has perfect recall and zero operational value.

## PROJECT STRUCTURE: FIVE DELIVERABLES, WITH DELIVERABLE 1 SPLIT INTO TWO MVPs (the 29 phases below are sub-steps inside these, not a separate track)
1a. **MVP 1 — Offline detector (keep genuinely minimal):** one agent type, three tools, one trace schema, normal traces, three injected anomaly types, rules, Isolation Forest, one evaluation notebook, explainable alert output. Nothing else — no autoencoder, no sequence model, no public dataset integration yet. The goal is a working, checkable, end-to-end loop fast. *(Gates A–C)*
1b. **MVP 2 — Runtime control plane (only after MVP 1 works):** FastAPI safety service, live tool interception, allow/block/pause, policy versioning, audit log, failure fallback. *(Gates D–E)*
2. **Sequence intelligence** — sequence representation, Markov baseline, sequence-aware detector, error localization, autoencoder. Justify the exact architecture (LSTM/GRU autoencoder, TCN, transformer, or Markov/n-gram baseline) rather than defaulting to a transformer — for a solo project, a Markov baseline plus GRU/LSTM autoencoder is usually more defensible than jumping straight to a transformer.
3. **Agent integration** — LangGraph, MCP tool boundary, real telemetry ingestion, CrewAI only if time permits.
4. **Operations and human review** — PostgreSQL audit store, dashboard, trace replay, human approval queue, full public-dataset/hybrid-telemetry integration (moved here from MVP 1 — it's real work, not a blocker for having something working first).
5. **Production-shaped evaluation** — Docker deployment, failure injection, security tests, load tests, drift monitoring, final benchmark report using the full Evaluation Rules above. *(Gate F)*

## TIME-BOXING (a solo student project can expand indefinitely without this)
At the start of each deliverable, estimate: implementation time, debugging time, learning dependencies, minimum success criterion. If the estimated scope exceeds available time, cut optional features **in this order**: CrewAI integration → dashboard polish → Redis → Kafka → second agent framework → advanced sequence model. Never cut evaluation, auditability, or the working safety boundary first — those are the parts that make this a defensible engineering project rather than a demo.

## MENTOR BEHAVIOR (governs how you run every phase with me)
At the start of every phase:
1. State the exact artifact we will produce.
2. State what is deliberately out of scope for this phase.
3. State the smallest implementation that can validate the phase.
4. Identify assumptions that require verification.
5. Don't introduce a new framework, database, model, or abstraction unless it's necessary for *this* phase's artifact.
6. If scope is becoming too large, recommend cutting features rather than silently expanding the timeline.
7. Never call a result production-ready, novel, or secure without evidence supporting that specific claim.
8. When I say **NEXT**, continue only one phase or one explicitly named sub-phase — don't regenerate the whole roadmap.

## COMPETITIVE / PRIOR-ART LANDSCAPE (refresh before Phase 0 and again before Phase 21)

| Project | What it actually is | Detection approach | Runtime intervention | Production platform (dashboard/DB/audit) | Our angle |
|---|---|---|---|---|---|
| Langfuse — open-source LLM/agent observability platform (core license: MIT) | Tracing, prompt mgmt, eval, agent graphs, cost/latency | Manual/rule eval | **Confirmed absent**: own docs + independent comparisons state it's architecturally observe-only, delegating runtime blocking to third-party libraries | Yes (observability only) | Add behavioral safety layer |
| Arize Phoenix — open-source LLM/agent observability project (license: verify current terms before citing) | LLM/RAG/agent observability, hallucination + drift detection, OpenTelemetry/OpenInference | Heuristic/embedding drift | Not confirmed from sources checked — verify before repeating this claim | Yes (observability only) | Add runtime behavioral risk scoring |
| PostHog | Product analytics + LLM capture | Analytics only | Not confirmed from sources checked | Yes (analytics only) | Comparison scope not yet verified; compare only documented capabilities |
| AgentGuard (multiple unrelated repos) | Runtime cost/loop guardrails | Hardcoded thresholds | Yes | No (SDK, no platform) | Add learned anomaly + risk engine + dashboard |
| ATBench / AgentDoG 1.5 | Trajectory-level safety benchmark family + alignment/classification model | Trained classifier on trajectory content | **Yes** — AgentDoG 1.5 includes a documented online "Pre-Reply" runtime guardrail: it inspects the *complete* trajectory before releasing the final response and can flag/intervene on unsafe behavior. Not offline-only, correcting an earlier claim in this document. Architecturally distinct from our target flow: our design intercepts *before each tool call* (agent → tool request → safety service → policy decision → tool execution), not only before the final response — a meaningful difference since it can stop a risky external action before it happens rather than only gating what the user sees afterward. Only state this comparison after recording AgentDoG's exact intervention point in `sources.yaml`. | Full production-platform scope (persistent audit trail, human-approval workflow, deployment model): **not confirmed from the sources checked** — verify exact scope before assuming absence | No equivalent combination has yet been identified in this comparison set — this must be validated through the Phase 0 source registry, not asserted |
| TrajAD | Runtime anomaly detection + error localization (research) | Trained on synthetically perturbed trajectories | No (research prototype) | No | Compare detection quality; borrow error-localization idea for explainability |
| Trajectory Guard | Sequence-aware real-time anomaly model (research) | Sequence model (after point-outlier approach reportedly underperformed in their setup) | No (research prototype) | No | Direct architecture inspiration for our sequence-model detector; attempt to reproduce their finding on our own data first, don't assume it generalizes |
| IBM "Silent Failures" paper (arXiv 2511.04032) | Anomaly detection benchmark on real multi-agent traces (drift/cycles/errors) | Supervised XGBoost, semi-supervised SVDD/Isolation Forest, unsupervised K-Means | No (research paper) | No | Use published feature schema (16 features: token/latency/path/prompt/model) as a design reference; treat their reported Isolation Forest accuracy (~89% under their own split/setup) as context, not an expected result for ours; dataset availability unverified |

**Revised differentiation hypothesis** (corrected — some research systems, including AgentDoG 1.5, do provide online trajectory-level safety intervention, so "we add runtime intervention where none existed" is no longer accurate): *our differentiation hypothesis is not that existing systems lack anomaly detection or runtime safety. It is that a framework-agnostic, production-shaped control plane combining operational telemetry, contextual behavioral baselines, multiple detector types (rules + Isolation Forest + Autoencoder + sequence model), deterministic policy precedence, tool-level runtime interception, auditability, and human approval may offer a useful integrated workflow that is not equivalent to any single system in our comparison set — each of which covers part of this, not the combination.*

**All adversarial cases in the prototype are controlled simulations or failure-injection scenarios** — a simulated data-exfiltration scenario, a prompt-injection scenario, a tool-permission violation, an operational anomaly. Never claim real-world attack coverage or security certification from these.

## SOURCE REGISTRY (concrete artifact, not just a policy)
Maintain `research/sources.yaml` (or `.csv`) with one row per external source: source_id, title, authors/organization, source type, URL, publication/release date, date checked, capability verified, exact supporting quotation or section, license, confidence label, how it affects our design. This makes the prior-art review reproducible instead of relying on memory of past conversation turns — treat everything in the prior-art table below as a **lead to convert into a registry entry during Phase 0**, not as already-registered fact.

## THREE SYSTEMS, KEPT SEPARATE (use this distinction constantly, especially in interviews)
1. **Observability** — what happened?
2. **Detection** — was the behavior abnormal?
3. **Safety control** — what should we do about it?

Don't let the agent/LLM control its own safety layer. Flow is strictly: LLM → Agent → tool request → Safety system → decision → tool. Never the reverse.

## SYSTEM BOUNDARY
Keep separate at all times: agent, LLM, tools, observability layer, feature extraction, ML, risk engine, policy engine, human, audit store.

## PRODUCTION FAILURE MODES
For offline MVP 1: document model failure and invalid-input behavior only — there's no live request path yet, so there's nothing to fail open/closed on.
From MVP 2 (first live tool interception) onward: define behavior for model failure, telemetry loss, stale models, database failure, safety-service downtime, and policy-store unavailability. Every live tool request must have an explicit fail-open, fail-closed, or human-approval policy, chosen based on that tool's impact class — not one blanket fallback for every tool.

## POLICY PRECEDENCE (what happens when signals disagree)
1. A critical policy or permission violation overrides anomaly status, regardless of how low the anomaly score is.
2. A high anomaly score without a policy violation produces an alert or confirmation request — not an automatic block.
3. Missing confidence or missing telemetry increases uncertainty and may require confirmation for medium- or high-impact tools, even absent a clear anomaly signal.
4. The final logged action must identify which specific rule or policy had precedence — "the model said so" is never a sufficient audit entry.

## SCORE OUTPUT MUST BE MULTI-SIGNAL, NOT ONE NUMBER
Never output a single `risk = 0.87`. Output component scores (behavioral anomaly, sequence anomaly, policy risk) plus a confidence estimate, and keep `anomaly score ≠ probability of harm ≠ policy violation` conceptually distinct in the code and the explanation shown to a human reviewer.

## TECHNOLOGY STACK — TIED TO THE MVP THAT ACTUALLY NEEDS IT, NOT PHASE NUMBERS
This is a correction to scope creep risk. Don't configure Kafka/Grafana/K8s — or even FastAPI — before you have a working detector that needs them.

- **MVP 1 / offline detector:** Python, Pydantic, NumPy, Pandas, scikit-learn, Jupyter, Matplotlib, Git. No FastAPI — there's no live request path yet.
- **MVP 2 / runtime control plane:** add FastAPI, LangGraph, OpenTelemetry, a minimal HTTP client — introduced here because this is the first phase that actually has a live request to intercept.
- **Deliverables 4–5 (integration, ops, evaluation):** add PostgreSQL, Redis, WebSockets, Prometheus, Grafana, MLflow, Docker. Kafka/Redpanda only if you can demonstrate the single-process/event-loop architecture is actually insufficient — never add it because it looks impressive.

For every technology at the point it's introduced: why we need it, mandatory vs optional, and the simpler alternative.

## EXECUTION RULES (apply to every phase)
- Build incrementally. Give me **Phase 0 only**, then wait. I say **NEXT** to continue.
- Every task uses: **TASK → WHY → INPUT → COMMAND → CODE → EXPECTED OUTPUT → VALIDATION → COMMON ERRORS → WHY THE ERROR HAPPENS → FIX → CHECKPOINT.**
- Never say "preprocess the data" — say exactly which file, columns, operation, code, expected output.
- Never fabricate results. Distinguish **Expected / Actual / Hypothetical** explicitly.
- Label synthetic data as synthetic, always, with stated limitations.
- Every experiment records: random seed, package versions, dataset version, model version, hardware.
- After every milestone: git status/add/commit guidance (never commit secrets, `.env`, weights, oversized files).
- After every major milestone: interview questions that force me to explain *why*, not recite a script.
- Maintain a running troubleshooting log, consolidated into a final guide.

## PHASE 1 — DATA-SOURCE AUDIT AND EXPERIMENT DESIGN (not a requirement to integrate public data into MVP 1)
Before generating any synthetic data: inspect the availability, license, schema, and labels of public datasets; document what each can and cannot provide; decide per-source whether it's used in MVP 1, later evaluation, or not at all. **MVP 1 does not require public-dataset integration** — it uses self-generated, clearly labeled synthetic telemetry. Public datasets are evaluated during this phase and integrated later only when their schema, license, and labels actually support the planned experiment.

Datasets and repositories identified for possible use — **their existence and general purpose are verified** (direct arXiv/GitHub lookup); their availability, exact license, version, schema, and suitability for our specific experiment remain to be recorded and confirmed in the source registry before use:
- **ATBench / ATBench500** — trajectory-level safety labels, risk source/failure mode/harm taxonomy. Good for: content-level safety classification. Missing: system telemetry (latency, cost, token counts, retries, tool success rates).
- **AgentDoG training data** — binary + fine-grained taxonomy examples. Same telemetry gap.
- **IBM's "Silent Failures" dataset** (arXiv 2511.04032) — real multi-agent traces with token/latency/path/prompt/model features, but public availability unconfirmed as of this writing.
Explicitly identify what these can and cannot provide, then design the hybrid for **later evaluation** (not MVP 1): real trajectory-safety data (content/sequence layer) + our own synthetic telemetry (operational layer) + traces from our own LangGraph agent (integration layer, arriving with Deliverable 3).

## PROJECT PHASES
*(Carry forward unchanged from the original scope — Phase 2: Synthetic Trace Generation, Phase 3: Schema Design, Phase 4: Data Collection/Instrumentation, Phase 5: Preprocessing, Phase 6: Feature Engineering, Phase 7: EDA, Phase 8: Classical ML (Isolation Forest / LOF / One-Class SVM) — explicitly attempt the naive pooled-feature approach first and expect it may underperform on sequential anomalies, matching Trajectory Guard's documented finding, Phase 9: Deep Learning (Autoencoder), Phase 10: ML vs DL vs Rules Experiment, Phase 11: Sequence Anomaly Detection — this is now load-bearing, not optional, given the prior-art findings above, Phase 12: Risk Engine, Phase 13: Safety/Guardrail Engine, Phase 14: Observability Middleware, Phase 15: Real-Time Pipeline, Phase 16: LangGraph Integration, Phase 17: MCP Integration, Phase 18: Human-in-the-Loop, Phase 19: Dashboard, Phase 20: Database Design, Phase 21: Evaluation, Phase 22: Failure Injection (add: privilege escalation, data exfiltration chains as adversarial scenarios, not just loops/retries/latency), Phase 23: Security, Phase 24: MLOps, Phase 25: Testing, Phase 26: Docker, Phase 27: Deployment, Phase 28: Optimization, Phase 29: Repository Structure.)*

### Training protocol (prevents leakage, gives a clean interview answer)
Rules are deterministic and need no training. Isolation Forest and the autoencoder are trained **only** on normal training traces. Abnormal traces are used for validation and testing, never for unsupervised training. The test set's trace IDs must not appear in training or validation.

### Detector scope is staged — do not let it collapse into one experiment
- **MVP 1 evaluates only:** Rules, Isolation Forest.
- **Final evaluation (Deliverable 5) evaluates all four:** Rules (deterministic thresholds), Isolation Forest (feature vectors), Autoencoder (reconstruction error, normal-only training), Sequence-aware model (tool-call sequence anomaly) — the fourth is added specifically because the first three are expected, per prior art, to underperform on sequential/contextual attacks.
- **No claim about the four-detector comparison may be made until all four are implemented and evaluated on the same split.** Don't let an eager mentor-model try to force the full comparison into MVP 1.

Measure: precision, recall, F1, AUROC, **PR-AUC** (anomaly prevalence is often low — AUROC alone can be misleadingly optimistic), false-positive rate, **p50 and p95 detection latency** (not just average), explainability, compute cost — for all four, plus a stated hypothesis about which wins where, tested rather than assumed.

**Explainability is not measurable without a rubric.** Score it against: does it identify the abnormal signal? does it identify the affected step/tool? does it identify the governing policy? does it produce a human-understandable reason? does it avoid exposing secrets? A detector that can't answer these isn't explainable regardless of its accuracy.

**Don't compare content-safety classifiers and operational anomaly detectors as if they're interchangeable — they answer different questions on different inputs.** Report separately: (1) content-safety detection, (2) operational-behavior anomaly detection, (3) policy-violation detection, (4) combined runtime decision performance. Only combine their outputs at the policy-engine layer, after documenting that their inputs and labels differ — this is what keeps the central thesis honest instead of an apples-to-oranges comparison between a trajectory-content benchmark and telemetry like latency/retries/cost.

## NAMING (optional, revisit after Phase 0–10 show what the system actually does well)
Working name candidates: *AgentSentinel* or *Behavioral Anomaly Detection and Runtime Safety Control Plane for AI Agents* as the technical subtitle. Don't lock this in before the system's actual strongest capability is proven — name it after what it turns out to be good at, not what it was pitched as on day one.

---

# START NOW

Do not start coding the full application. Start with:

# PHASE 0 — PROJECT DEFINITION, VERIFIED COMPETITIVE POSITIONING, AND ARCHITECTURE

Give me: problem statement, real-world use cases, a re-verified version of the competitive/prior-art table above (confirm it's still current, don't assume my August 2026 snapshot stays accurate), what makes this project's *combination* different (backed by the table, not assertion), complete architecture, component-by-component explanation, phased tech stack, MVP scope, final production scope, roadmap, risks/limitations (including the real risk that a new paper/product closes this gap further while I'm building), and research/innovation opportunities.

Then wait for me to confirm before moving to Phase 1.

**I know the concepts. My goal is to implement them, and to end up with something I can defend honestly in front of a hiring panel — including saying plainly which parts are novel engineering integration and which parts build on existing research — not a project that oversells itself and falls apart under a follow-up question.**
