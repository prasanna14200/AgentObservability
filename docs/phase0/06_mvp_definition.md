# AgentSentinel — MVP 1 Definition

## 1. MVP Objective

MVP 1 is the smallest end-to-end experiment that demonstrates:

```text
Agent behavior
    ↓
Trace
    ↓
Features
    ↓
Rules + ML
    ↓
Anomaly decision
    ↓
Evaluation
```

It is intentionally not the final production platform.

## 2. Agent

Use one simple agent type:

**Customer Support Agent**

The agent should perform simple customer/order operations.

## 3. Tools

Exactly three initial tools:

```python
search_customer()
get_order()
update_order()
```

## 4. Initial Trace Schema

Minimum fields:

```text
trace_id
agent_id
task_id
timestamp
event_type
tool_name
tool_input_metadata
tool_output_metadata
latency_ms
status
retry_count
token_usage
```

Sensitive real credentials or raw secrets must never be stored.

## 5. Synthetic Normal Data

Initial normal traces will be generated synthetically.

Every synthetic dataset must be explicitly labelled:

```text
DATA TYPE: SYNTHETIC
```

Limitations:

- does not represent all real production behavior
- may encode assumptions from our generator
- may make anomalies easier to detect than in real environments

## 6. Injected Anomalies

### Anomaly 1 — Tool Loop

Example:

```text
search_customer
get_order
search_customer
get_order
search_customer
get_order
```

### Anomaly 2 — Retry Storm

Example:

```text
get_order → failure → retry → failure → retry → failure
```

### Anomaly 3 — Resource Spike

Example:

```text
normal token usage
normal token usage
large unexpected token usage
```

## 7. Initial Detection Methods

### Rules

Examples:

```text
excessive retries
excessive repeated tool calls
obvious resource threshold
```

### Isolation Forest

Input:

A fixed feature vector representing trace behavior.

Potential features:

```text
total_tool_calls
unique_tools
repeat_count
retry_count
mean_latency
max_latency
token_usage
tool_switch_count
```

Exact features will be finalized after the data/schema work.

## 8. Evaluation Notebook

The first evaluation notebook will compare:

```text
Rules
vs
Isolation Forest
```

Metrics should include:

- precision
- recall
- F1
- confusion matrix
- detection latency where measurable

No performance number should be invented before the experiment is run.

## 9. Reproducibility

Every experiment must record:

```text
random seed
Python version
package versions
dataset version
model version
hardware
```

## 10. What MVP 1 Does Not Include

- Autoencoder
- sequence model
- real-time distributed streaming
- MCP
- production LangGraph integration
- human approval UI
- PostgreSQL production schema
- Kafka
- Kubernetes
- production deployment

These are later phases.

## 11. MVP 1 Success Criteria

MVP 1 is successful if:

1. synthetic normal traces can be generated reproducibly
2. anomalies can be injected reproducibly
3. traces can be converted into features
4. rules can produce decisions
5. Isolation Forest can produce anomaly scores
6. results can be evaluated against known injected labels
7. the entire experiment can be reproduced from the repository
