# Trajectory Guard — Competitive / Prior-Art Research Record

**Research date:** 13 August 2026

## What it is
Trajectory Guard is a sequence-aware model for real-time anomaly detection in agentic AI.

The paper introduces a Siamese Recurrent Autoencoder with a hybrid objective combining task-trajectory alignment and sequential validity.

## Verified relevance
The work explicitly targets agent trajectory anomalies, contextual misalignment, structural incoherence, sequential structure and real-time safety verification.

## Critical implication
We must NOT claim that using an Autoencoder for agent anomaly detection is unique. It is already prior art.

## Proposed distinction
Trajectory Guard is an important model-level baseline for later experiments.

AgentSentinel instead investigates a system-level architecture combining telemetry, rules, classical anomaly detection, deep anomaly detection, sequence signals, risk engine, policy engine, runtime intervention, human approval and audit.

## Source
- https://arxiv.org/abs/2601.00516
