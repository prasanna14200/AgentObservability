# AgentDoG — Competitive / Prior-Art Research Record

**Research date:** 13 August 2026

## What it is
AgentDoG is a diagnostic guardrail framework for AI agent safety and security.

The 2026 paper describes contextual monitoring across agent trajectories and diagnosis of unsafe actions and seemingly safe but unreasonable actions. It also describes provenance/transparency and multiple model sizes.

AgentDoG 1.5 extends the work and describes a training-free online guardrail for real-time safety moderation.

## Critical correction
Earlier project notes described AgentDoG as primarily rule/pattern based. That is no longer a safe claim. The published work describes learned guardrail models and contextual trajectory monitoring.

## Relevance
AgentDoG is major prior art for agent safety, trajectory monitoring, learned safety moderation, online guardrails and diagnostic explanations.

## Proposed distinction to investigate
AgentSentinel must not claim novelty merely from using ML for agent safety.

The proposed distinction is an integrated control plane combining operational telemetry, behavioral features, multiple anomaly detectors, risk aggregation, explicit policies, pre-tool-call interception, human approval and auditability.

This distinction must be tested against AgentDoG implementations and papers.

## Sources
- https://arxiv.org/abs/2601.18491
- https://arxiv.org/abs/2605.29801
