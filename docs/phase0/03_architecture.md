# AgentSentinel — Architecture

## 1. Architectural Goal

The architecture separates:

1. agent execution
2. observability
3. detection
4. risk aggregation
5. policy enforcement
6. human intervention
7. auditability

## 2. High-Level Architecture

```text
                           USER
                             │
                             ▼
                    ┌────────────────┐
                    │   AI AGENT     │
                    │   LangGraph    │
                    └───────┬────────┘
                            │
                      Tool Request
                            │
                            ▼
              ╔══════════════════════════╗
              ║   SAFETY CONTROL PLANE   ║
              ║                          ║
              ║ Context Collector        ║
              ║        ↓                 ║
              ║ Feature Engine           ║
              ║        ↓                 ║
              ║ ┌──────┼───────┐         ║
              ║ ↓      ↓       ↓         ║
              ║Rules  IForest  Sequence  ║
              ║ └──────┼───────┘         ║
              ║        ↓                 ║
              ║ Risk Engine              ║
              ║        ↓                 ║
              ║ Policy Engine            ║
              ╚════════╪═════════════════╝
                       │
              ┌────────┼─────────┐
              ▼        ▼         ▼
            ALLOW    REVIEW     BLOCK
              │        │
              │        ▼
              │      HUMAN
              │        │
              └────────┘
                   │
                   ▼
                 TOOL
                   │
                   ▼
              TOOL RESULT
                   │
                   ▼
         ┌──────────────────────┐
         │ OBSERVABILITY LAYER  │
         │                      │
         │ Traces / Metrics /   │
         │ Logs                 │
         └──────────┬───────────┘
                    ▼
               Data Store
                    │
             ┌──────┴──────┐
             ▼             ▼
         Dashboard      Audit Log
```

## 3. Tool-Level Intervention

The intended intervention point is:

```text
Agent
 ↓
Tool Request
 ↓
Safety Interceptor
 ↓
Detection
 ↓
Risk
 ↓
Policy
 ↓
ALLOW / REVIEW / BLOCK
 ↓
Tool
```

This is distinct from a system that only analyzes a completed trajectory after all actions have occurred.

## 4. Runtime Sequence

### Normal

```text
Agent → request tool
      → safety check
      → ALLOW
      → tool executes
      → result
      → trace recorded
```

### Suspicious

```text
Agent → request tool
      → safety check
      → anomaly detected
      → REVIEW
      → human decision
      → ALLOW or BLOCK
```

### High risk

```text
Agent → request tool
      → safety check
      → high risk
      → BLOCK
      → audit event
```

## 5. Detection Pipeline

```text
Raw Events
   ↓
Trace Schema
   ↓
Feature Extraction
   ↓
 ┌───────────────┬─────────────────┬────────────────┐
 │               │                 │
Rules      Isolation Forest   Sequence Detector
 │               │                 │
 └───────────────┴─────────────────┘
                 ↓
             Risk Engine
                 ↓
            Policy Engine
```

## 6. Data Plane vs Control Plane

### Data plane

The agent and tools perform normal work.

### Control plane

AgentSentinel observes and controls selected actions.

This separation is important because the safety system should influence execution without becoming the agent itself.

## 7. Failure Handling

The architecture must define fallback behavior when:

- ML model is unavailable
- telemetry is incomplete
- database is unavailable
- feature extraction fails
- safety service times out

A failure to observe must not automatically be interpreted as evidence of safety.

## 8. Design Principle

The architecture should remain lean until measured requirements justify additional infrastructure.
