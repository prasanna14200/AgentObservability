# AgentSentinel — Risks and Limitations

## 1. Synthetic Data Risk

Synthetic data may not capture real production agent behavior.

### Mitigation

Use public real datasets where suitable and later collect controlled traces from realistic agents.

## 2. False Positives

Legitimate unusual behavior may be classified as anomalous.

### Consequence

Users may experience unnecessary blocks or human-review requests.

### Mitigation

Measure false-positive rate and use risk thresholds/policies rather than blindly blocking every anomaly.

## 3. False Negatives

The system may fail to detect unsafe behavior.

### Consequence

A potentially harmful tool action may execute.

### Mitigation

Use layered controls:

```text
Rules
+
ML
+
Sequence detection
+
Policy constraints
+
Human approval
```

## 4. Anomaly Does Not Equal Maliciousness

An anomaly score indicates deviation from a learned or defined baseline.

It does not automatically prove:

```text
malicious intent
```

or:

```text
harm
```

The risk engine must keep these concepts separate.

## 5. Distribution Shift

Normal agent behavior can change over time.

Examples:

- new tools
- new workflows
- new prompts
- new users
- new model versions

A model trained on historical behavior may become less reliable.

## 6. Model Failure

The ML service may fail, timeout, or produce invalid output.

The safety architecture therefore needs deterministic fallback behavior.

## 7. Telemetry Failure

Missing telemetry is not equivalent to safe behavior.

The system should represent:

```text
NO ANOMALY DETECTED
```

separately from:

```text
INSUFFICIENT TELEMETRY
```

## 8. Latency

Runtime safety checks add latency to tool execution.

Later evaluation must measure:

```text
baseline latency
+
safety-check latency
```

## 9. Cost

Deep-learning and sequence models may require more compute than simple rules.

The project must measure the tradeoff between:

```text
accuracy
latency
compute
explainability
```

## 10. Novelty Risk

The agent-safety field evolves quickly.

A new paper or product may overlap with the proposed combination.

### Mitigation

Refresh prior-art research before major evaluation and before making final claims.

## 11. Security Risk

Trace data may contain sensitive information.

### Mitigation

- avoid raw secrets
- redact sensitive fields
- use synthetic data initially
- define access controls later
- audit sensitive operations

## 12. Operational Risk

A safety system that blocks too many actions may be unusable.

A system that blocks too few actions may be unsafe.

Therefore the project must evaluate both sides of the tradeoff.
