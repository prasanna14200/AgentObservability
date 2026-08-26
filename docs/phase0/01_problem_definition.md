# AgentSentinel — Problem Definition

## 1. Project Overview

**Project Name:** AgentSentinel

**Technical description:**  
An OpenTelemetry-compatible behavioral observability and runtime safety control plane for AI agents that combines operational telemetry, contextual anomaly detection, sequence analysis, deterministic policies, explainable risk aggregation, and human-in-the-loop intervention.

The system answers three separate questions:

1. **Observability:** What happened during agent execution?
2. **Detection:** Was the observed behavior abnormal?
3. **Safety Control:** What should the system do about that behavior?

## 2. Problem Statement

Modern AI agents can perform multi-step tasks autonomously. Unlike a traditional single-response LLM application, an agent can repeatedly interact with external systems.

A typical execution is:

```text
User Request
    ↓
Agent
    ↓
LLM reasoning
    ↓
Tool call
    ↓
Tool result
    ↓
LLM reasoning
    ↓
Another tool call
    ↓
Final response
```

An agent may produce an abnormal or potentially dangerous execution trajectory even when individual tool calls appear valid in isolation.

Example:

```text
Normal:
search_customer → get_order → update_order → response

Potentially abnormal:
search_customer → get_order → read_sensitive_data
→ external_request → retry → retry → retry
```

The central problem is:

> How can an AI-agent system continuously observe agent behavior, identify contextual and sequential deviations from expected behavior, and make an explainable runtime safety decision before potentially dangerous tool actions are executed?

## 3. Target Failure Scenarios

### 3.1 Tool-call loop
```text
A → B → A → B → A → B
```

### 3.2 Retry storm
```text
failure → retry → failure → retry → failure → retry
```

### 3.3 Resource/token spike
Unexpected growth in tokens, latency, tool calls, or other operational resources.

### 3.4 Abnormal tool sequence
A sequence that is unusual for the current task or agent.

### 3.5 Privilege escalation
A transition from normal operations toward restricted or administrative tools.

### 3.6 Potential data-exfiltration chain
Sensitive-data access followed by an external communication action.

## 4. Why Observability Alone Is Insufficient

Observability answers:

> What happened?

It does not automatically answer:

> Was the behavior unusual?

or:

> Should the next tool call be allowed?

AgentSentinel therefore treats observability as the foundation for behavioral detection.

## 5. Why Rules Alone Are Insufficient

Rules are valuable and explainable:

```python
if retry_count > 5:
    block()
```

However, rules only detect conditions that developers have explicitly encoded.

AgentSentinel therefore investigates whether learned behavioral models can detect previously unspecified deviations.

This is a hypothesis to be experimentally evaluated, not a guaranteed claim.

## 6. Contextual Anomaly Detection

Anomaly is contextual, not absolute.

A large number of tool calls may be normal for a coding agent but unusual for a simple customer-support task.

Relevant context can include:

- agent identity
- task type
- tool permissions
- historical behavior
- current trajectory
- resource usage

Conceptually:

```text
Context + Current Behavior
        ↓
Behavioral Features
        ↓
Anomaly Detection
        ↓
Anomaly Signals
```

## 7. Core Runtime Flow

```text
AI Agent
   ↓
Tool Request
   ↓
Safety Interceptor
   ↓
Context Collection
   ↓
Feature Extraction
   ↓
Detection
   ↓
Risk Engine
   ↓
Policy Engine
   ↓
ALLOW / REVIEW / BLOCK
```

The safety layer is intended to intervene before selected tool actions execute.

## 8. What the Project Is Not

AgentSentinel is not:

- a replacement for an LLM
- a general-purpose agent framework
- a RAG system
- a Langfuse clone
- a generic analytics dashboard
- a simple rule-only guardrail
- a claim that Isolation Forest or Autoencoders are novel algorithms

The project focuses on the integrated engineering and experimental problem of combining observability, behavioral detection, risk aggregation, and runtime safety control.

## 9. MVP Boundary

Initial MVP:

- one agent type
- three tools
- structured trace schema
- synthetic normal traces, explicitly labelled synthetic
- three injected anomalies
- deterministic rules
- Isolation Forest
- one evaluation notebook

Initial anomaly types:

1. tool-call loop
2. retry storm
3. token/resource spike

Autoencoder and sequence-aware detection come later.

## 10. Research Hypothesis

> Can a multi-signal behavioral anomaly detection layer combining statistical feature-based detection, sequence-aware modeling, and deterministic policy detect novel agent execution deviations that rule-based guardrails and trajectory-level safety classifiers miss, while maintaining an acceptable false-positive rate and real-time latency?

This is a hypothesis and must be validated experimentally.

## 11. Limitations

- Synthetic traces may not represent production behavior.
- An anomaly does not necessarily mean malicious behavior.
- Models may suffer from distribution shift.
- False positives can block legitimate unusual behavior.
- False negatives can miss unsafe behavior.
- Prior art in agent safety is evolving rapidly.

## 12. Final Definition

AgentSentinel investigates a behavioral safety control plane for autonomous AI agents that combines telemetry, contextual behavioral baselines, statistical anomaly detection, sequence analysis, risk aggregation, policy enforcement, and tool-level runtime intervention. The system will be evaluated experimentally rather than assuming that learned detection is always superior to rules.
