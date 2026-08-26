# AgentSentinel — Competitive Comparison Matrix

**Research date:** 13 August 2026

## Methodology
This matrix distinguishes verified capability, overlap, proposed distinction and claims requiring further verification. No "nobody does this" claim is used.

| System | Observability / tracing | Agent trajectory analysis | Learned safety/anomaly models | Runtime / online safety | Evaluation | Main relevance |
|---|---|---|---|---|---|---|
| Langfuse | Strong | Yes, through agent/LLM tracing | Verify feature-by-feature | Not the primary positioning used here | Strong | Observability/evaluation reference |
| Arize Phoenix | Strong | Strong agent tracing/evaluation | Multiple evaluation capabilities | Online trace evaluations exist | Strong | Observability/evaluation reference |
| PostHog | Strong product analytics | AI/LLM capabilities | Not the primary agent-anomaly baseline here | Not the primary positioning used here | Strong analytics | Product analytics reference |
| AgentDoG | Trajectory/context monitoring | Strong | Yes | Online guardrail work exists | Strong research benchmark | Major agent-safety prior art |
| Trajectory Guard | Trajectory-focused | Strong | Yes, recurrent autoencoder | Explicitly targets real-time detection | Strong research evaluation | Major learned anomaly-detection baseline |
| AgentSentinel | Planned | Planned | Planned | Planned pre-tool interception | Planned head-to-head evaluation | Integrated behavioral safety control plane |

## What this means
The project is NOT novel simply because it contains tracing, anomaly detection, autoencoders, trajectory analysis, agent safety or guardrails.

The project hypothesis concerns the practical integration of:

```text
Agent telemetry
     ↓
Behavioral features
     ↓
Rules + multiple learned detectors
     ↓
Sequence analysis
     ↓
Risk aggregation
     ↓
Policy
     ↓
Pre-tool-call intervention
     ↓
Human review
     ↓
Audit
```

## Required future benchmark
Before final novelty claims, compare against:
1. rules-only baseline
2. Isolation Forest
3. Autoencoder
4. sequence-aware / Trajectory Guard-style baseline
5. AgentDoG-style safety baseline where technically reproducible
6. combined AgentSentinel pipeline

## Evidence sources
- https://github.com/langfuse/langfuse
- https://github.com/Arize-ai/phoenix
- https://github.com/PostHog/posthog
- https://arxiv.org/abs/2601.18491
- https://arxiv.org/abs/2605.29801
- https://arxiv.org/abs/2601.00516
- https://arxiv.org/abs/2602.06443

## Status
Phase 0 prior-art snapshot. Refresh before Phase 21 evaluation and before final public novelty claims.
