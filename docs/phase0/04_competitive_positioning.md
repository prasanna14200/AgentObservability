# AgentSentinel — Competitive Positioning and Prior-Art Record

## 1. Positioning Principle

AgentSentinel does **not** claim that:

- agent observability is new
- trajectory anomaly detection is new
- Isolation Forest is new
- Autoencoders are new
- deterministic guardrails are new
- human-in-the-loop agent control is new

The project investigates a specific integrated architecture and evaluates it experimentally.

## 2. Comparison Dimensions

We compare systems using these dimensions:

- observability/tracing
- evaluation
- prompt management
- anomaly detection
- sequence/trajectory analysis
- deterministic guardrails
- runtime tool interception
- risk aggregation
- human approval
- audit trail
- ML/DL behavioral models

## 3. Preliminary Competitive Matrix

| System | Primary role | Relevance to AgentSentinel | Key distinction to investigate |
|---|---|---|---|
| Langfuse | LLM/agent observability, tracing, evaluation, prompt management | Strong observability reference | Our target adds a behavioral safety/control layer |
| Arize Phoenix | AI/LLM observability, tracing, evaluation and related ML/AI analysis | Strong observability/evaluation reference | Need to distinguish our runtime behavioral intervention and risk policy architecture |
| PostHog | Product analytics with AI/LLM capabilities | Analytics reference | Not positioned as the core behavioral safety control plane |
| AgentDoG / related research | Agent trajectory analysis / safety research | Strong prior-art reference | Must compare trajectory-level detection with our proposed pre-tool interception architecture |
| AgentGuard-style systems | Deterministic agent guardrails | Safety reference | Compare learned behavioral detection against explicit rules |

## 4. Evidence Policy

Before making a final novelty claim, verify each competitor using:

1. official documentation
2. official repository
3. license information
4. research paper where applicable
5. current product documentation

Do not write "nobody does X" unless exhaustive evidence supports that statement.

Preferred wording:

> "The proposed combination was not found in the systems reviewed."

or:

> "The systems reviewed provide overlapping capabilities, but their architecture differs from the proposed runtime behavioral control plane."

## 5. Differentiation Hypothesis

The project's differentiation hypothesis is:

```text
Operational telemetry
       +
Contextual behavioral baseline
       +
Multiple anomaly detectors
       +
Sequence analysis
       +
Risk aggregation
       +
Policy enforcement
       +
Tool-level intervention
       +
Human approval
       +
Auditability
       +
Controlled evaluation
```

The value of this combination must be demonstrated experimentally.

## 6. Architectural Distinction

A particularly important dimension is the point at which intervention occurs.

Target design:

```text
Agent
 ↓
Tool request
 ↓
Safety interception
 ↓
Decision
 ↓
Tool execution
```

Some trajectory-analysis approaches may analyze a larger or completed trajectory before producing a response or decision.

These architectures should not be described as identical.

## 7. What We Must Prove

The project should eventually measure:

- rule-only detection
- feature-based ML detection
- deep-learning detection
- sequence-aware detection
- combined detection
- false-positive rate
- false-negative rate
- latency
- computational cost
- explanation quality
- intervention success

## 8. Competitive Research Status

**Status:** Preliminary positioning only.

A refreshed web/repository/paper review is required before the major evaluation phase and before making final claims.

## 9. Source Record

When the competitive review is performed, record:

| Claim | Source | Date checked | Evidence | Interpretation |
|---|---|---|---|---|
| Langfuse capabilities | Official docs/repository | TBD | TBD | TBD |
| Phoenix capabilities | Official docs/repository | TBD | TBD | TBD |
| PostHog AI capabilities | Official docs/repository | TBD | TBD | TBD |
| AgentDoG capabilities | Paper/repository | TBD | TBD | TBD |
| AgentGuard-related project | Repository/docs | TBD | TBD | TBD |

This table is intentionally included so future research has an auditable trail.






















# Evidence-Based Competitive Review

Research date: 13 August 2026

## Evidence Table

| System | What was verified | License | Evidence source | Relevance to AgentSentinel | Remaining distinction |
|---|---|---|---|---|---|
| Langfuse | LLM/agent tracing, observability, evaluation, prompt management | MIT core; EE components separately licensed | Official GitHub + documentation | Strong overlap in tracing/evaluation | AgentSentinel focuses on behavioral anomaly detection + runtime safety intervention |
| Arize Phoenix | AI observability, tracing, evaluation, datasets/experiments, OpenTelemetry/OpenInference | Elastic License 2.0 | Official documentation + GitHub | Strong overlap in observability/evaluation | AgentSentinel focuses on behavioral anomaly detection + runtime tool intervention |
| PostHog | Product analytics and AI/LLM-related observability capabilities | MIT outside EE | Official GitHub / official documentation | Analytics/observability overlap | AgentSentinel is designed as a behavioral safety control plane |
| AgentDoG | Agent safety/security guardrail using trajectory-level contextual monitoring and diagnosis | Research project; verify repository license separately | arXiv paper | Very important prior art | Must compare its trajectory-level approach with our proposed pre-tool interception architecture |
| Trajectory Guard | Sequence-aware anomaly detection for agent trajectories using a recurrent autoencoder | Research paper | arXiv | Very important overlap with our ML/DL anomaly idea | Shows that sequence-aware learned anomaly detection is already prior art; our novelty cannot be "ML anomaly detection exists" |




# Revised Differentiation Statement

The competitive review shows that AgentSentinel is not differentiated simply because it uses machine learning for anomaly detection.

Existing systems and research already cover important parts of this problem:

- Langfuse provides LLM/agent observability, tracing, and evaluation.
- Arize Phoenix provides AI observability, tracing, evaluation, datasets, and experiments.
- PostHog provides product analytics with AI/LLM-related capabilities.
- AgentDoG provides contextual agent trajectory monitoring and safety diagnosis.
- Trajectory Guard investigates learned sequence-aware anomaly detection for agent trajectories.

Therefore, the project does NOT claim that ML-based agent anomaly detection is novel.

Instead, AgentSentinel investigates an integrated runtime safety architecture combining:

1. OpenTelemetry-compatible agent telemetry
2. contextual behavioral feature extraction
3. deterministic safety rules
4. classical anomaly detection
5. deep-learning anomaly detection
6. sequence-level behavioral analysis
7. multi-signal risk aggregation
8. pre-tool-call safety interception
9. human approval for uncertain/high-risk actions
10. audit logging
11. controlled head-to-head evaluation

The contribution is therefore an engineering and experimental hypothesis:

> Can multiple complementary behavioral signals be combined into a practical runtime control plane that detects and safely handles abnormal agent behavior while maintaining acceptable false-positive rates, latency, explainability, and operational cost?

This hypothesis must be validated experimentally.

The project should not use absolute claims such as "nobody does this" or "no existing system has ML anomaly detection."