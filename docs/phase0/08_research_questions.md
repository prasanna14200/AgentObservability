# AgentSentinel — Research Questions and Evaluation Hypotheses

## 1. Research Goal

The project should be more than a collection of technologies.

It should answer measurable engineering/research questions.

## 2. RQ1 — Rules vs Learned Detection

> Can learned behavioral anomaly detection identify controlled deviations that deterministic rules fail to detect?

Comparison:

```text
Rules
vs
Isolation Forest
```

Later:

```text
Rules
vs
Isolation Forest
vs
Autoencoder
```

## 3. RQ2 — Context

> Does including task/agent context improve anomaly detection compared with raw operational counts?

Compare:

```text
basic counts
vs
contextual features
```

## 4. RQ3 — Sequence

> Do sequence-aware models detect abnormal tool-call trajectories that flattened feature vectors miss?

Example:

```text
A → B → C
```

versus:

```text
A → C → B
```

The counts may be identical while the sequences differ.

## 5. RQ4 — Multi-Signal Risk

> Does combining multiple detection signals produce better safety decisions than relying on a single detector?

Potential inputs:

```text
rule signal
behavior anomaly
sequence anomaly
tool sensitivity
context
confidence
```

## 6. RQ5 — Runtime Tradeoff

> What is the tradeoff between detection quality and runtime latency?

Measure:

```text
detection quality
+
decision latency
+
compute
```

## 7. RQ6 — Distribution Shift

> How does detector performance change when normal agent behavior changes?

Possible shifts:

- new task types
- new tool usage patterns
- different agent policies
- different model versions

## 8. RQ7 — Safety Thresholds

> How should anomaly and risk thresholds be selected to balance false positives and false negatives?

The goal is not simply:

```text
maximum recall
```

or:

```text
maximum precision
```

but an operationally meaningful tradeoff.

## 9. RQ8 — Intervention

> Can the safety layer intervene before selected tool actions execute without making normal agent execution impractically slow?

Measure:

- interception latency
- normal-task success rate
- false intervention rate

## 10. Evaluation Principle

No accuracy, precision, recall, F1, latency, or improvement number should be stated before the corresponding experiment is actually run.

Use:

```text
EXPECTED
ACTUAL
HYPOTHETICAL
```

labels where appropriate.

## 11. Experimental Progression

```text
Rules
  ↓
Isolation Forest
  ↓
Autoencoder
  ↓
Sequence Detection
  ↓
Combined Risk Engine
  ↓
Runtime Intervention
```

Every additional component must be justified by measured evidence rather than complexity alone.
