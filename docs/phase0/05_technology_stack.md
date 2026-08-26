# AgentSentinel — Technology Stack

## 1. Stack Selection Principle

Technology should be introduced only when it solves a demonstrated problem.

The project should avoid adding Kafka, Kubernetes, Redis, Grafana, or other infrastructure merely to make the architecture look impressive.

## 2. Core Development Stack

| Technology | Purpose | Required | Simpler alternative |
|---|---|---:|---|
| Python | Main implementation language | Yes | None |
| NumPy | Numerical operations | Yes | Python lists for tiny experiments |
| Pandas | Trace/feature data | Yes | CSV module for trivial cases |
| scikit-learn | Classical anomaly detection | Yes | Custom implementation is not justified |
| PyTorch | Autoencoder/deep learning | Later | scikit-learn for classical models |
| Jupyter | Experiments/EDA | Yes | Python scripts |
| Matplotlib | Visualization | Yes | None for core plots |
| Pydantic | Schema validation | Yes | Dataclasses for small local objects |
| Git | Version control | Yes | None |

## 3. Agent Integration

### LangGraph

Purpose:

- provide a realistic agent execution environment
- model multi-step stateful agent workflows
- create realistic tool-call trajectories

Status: **Later integration**

We do not need LangGraph to build the first anomaly detector.

## 4. Tool Protocol

### MCP

Purpose:

- expose tools using a standardized tool protocol
- demonstrate interoperability with agent/tool ecosystems

Status: **Later integration**

MCP should be introduced after the detector and safety middleware work independently.

## 5. API Layer

### FastAPI

Purpose:

- expose trace ingestion
- expose anomaly/risk decisions
- expose safety APIs
- support dashboard/backend communication

Status: **Later**

Initial ML experiments can run without FastAPI.

## 6. Data Storage

### PostgreSQL

Purpose:

- durable traces
- risk decisions
- audit records
- human decisions

Status: **Later**

Initial experiments can use CSV/Parquet/local files.

## 7. Caching / State

### Redis

Potential uses:

- short-lived agent state
- low-latency risk context
- rate limits
- real-time coordination

Status: **Optional until required**

Simpler alternative:

- in-process memory
- PostgreSQL
- local state

## 8. Real-Time Communication

### WebSockets

Potential uses:

- live dashboard updates
- human approval requests
- streaming safety events

Status: **Later**

## 9. Telemetry

### OpenTelemetry

Purpose:

- standardize telemetry concepts
- improve interoperability
- avoid inventing a completely isolated tracing model

Important boundary:

> We reference and integrate standards/concepts; we do not vendor or fork another observability platform.

## 10. Monitoring

### Prometheus

Purpose:

- infrastructure/service metrics
- latency
- throughput
- errors

Status: **Production phase**

### Grafana

Purpose:

- operational visualization
- service and detector monitoring

Status: **Production phase**

## 11. ML Experiment Tracking

### MLflow

Purpose:

- model versions
- experiment tracking
- metrics
- artifacts

Status: **MLOps phase**

Initial experiments can use structured local metadata.

## 12. Streaming

### Kafka / Redpanda

Potential purpose:

- durable asynchronous event streaming
- high-volume telemetry
- decoupled consumers

Status: **Optional**

Do not introduce it until a measurable throughput/distribution requirement justifies it.

## 13. Containerization

### Docker

Purpose:

- reproducible services
- local multi-service deployment
- production packaging

Status: **Later**

## 14. Technology Introduction Order

```text
Python
 ↓
Pandas / NumPy
 ↓
scikit-learn
 ↓
PyTorch
 ↓
FastAPI
 ↓
LangGraph
 ↓
MCP
 ↓
OpenTelemetry
 ↓
PostgreSQL
 ↓
Redis/WebSockets if needed
 ↓
Prometheus/Grafana
 ↓
MLflow
 ↓
Docker
 ↓
Kafka/Redpanda only if justified
```

## 15. Main Engineering Principle

A working detector with simple infrastructure is more valuable than a distributed architecture with no validated detector.
