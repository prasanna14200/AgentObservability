# AgentSentinel — System Boundary

## 1. Purpose

This document defines what is inside and outside AgentSentinel so that the project does not blur the responsibilities of the agent, observability layer, ML models, safety engine, tools, storage, and human reviewer.

## 2. External Components

These are outside the core AgentSentinel implementation:

```text
User
LLM provider
External APIs
External SaaS
External databases
```

They may generate data or receive tool actions, but they are not implemented as part of the safety core.

## 3. Agent Layer

The agent is responsible for:

- receiving the task
- maintaining agent state
- reasoning/planning
- selecting tools
- requesting tool execution
- consuming tool results

Planned framework:

**LangGraph**

The agent is not the safety authority.

## 4. Tool Layer

Tools perform external actions.

For MVP 1:

```text
search_customer()
get_order()
update_order()
```

The tool layer represents the point where an agent decision can have an external effect.

## 5. Observability Layer

Responsibility:

> What happened?

Captures:

- trace ID
- agent ID
- task ID
- timestamp
- event type
- tool name
- tool input/output metadata
- latency
- status
- retries
- token/resource information where available

The observability layer should be compatible with OpenTelemetry concepts without copying another platform's implementation.

## 6. Detection Layer

Responsibility:

> Was this behavior abnormal?

Initial components:

```text
Rules
Isolation Forest
```

Later:

```text
Autoencoder
Sequence anomaly detection
```

## 7. Risk Engine

Responsibility:

> How risky is the current situation?

The risk engine should not treat anomaly score as identical to harm probability.

Possible inputs:

```text
behavior anomaly
sequence anomaly
policy signal
tool sensitivity
context
model confidence
```

## 8. Policy/Safety Layer

Responsibility:

> What should happen?

Possible decisions:

```text
ALLOW
REVIEW
BLOCK
```

The safety layer should be capable of intercepting selected tool calls before execution.

## 9. Human Layer

Human review is used for uncertain or high-risk decisions.

```text
Agent
 ↓
Safety decision
 ↓
REVIEW
 ↓
Human
 ↓
ALLOW / BLOCK
```

The human is the final authority for the human-in-the-loop workflow.

## 10. Storage Layer

Storage will eventually contain:

- traces
- model outputs
- risk decisions
- policy decisions
- human approvals/rejections
- audit records

PostgreSQL is the planned relational store unless later evaluation justifies another choice.

## 11. Dashboard

The dashboard is responsible for visibility, not detection itself.

It should eventually show:

- agent runs
- traces
- anomalies
- risk signals
- blocked actions
- human approvals
- audit events

## 12. Boundary Summary

```text
                 USER
                   ↓
                 AGENT
                   ↓
             TOOL REQUEST
                   ↓
        ┌─────────────────────┐
        │   AGENTSENTINEL     │
        │                     │
        │ Detection           │
        │ Risk                │
        │ Policy              │
        │ Audit               │
        └──────────┬──────────┘
                   ↓
                 TOOL
                   ↓
            External System
```

The central safety boundary is between the agent's requested action and execution of the external tool.
