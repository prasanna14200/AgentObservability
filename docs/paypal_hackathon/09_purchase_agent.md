# Day 9 — purchase agent and controlled comparison

On 2026-10-14, the local Day 9 build used [LangGraph 1.2.15](https://pypi.org/project/langgraph/1.2.15/) for a two-node graph and [Ollama Python 0.6.3](https://pypi.org/project/ollama/0.6.3/) to call the already installed local `qwen2.5:1.5b` model. These package versions were checked on PyPI that day. No model was downloaded and no PayPal request was made. Run `python scripts/demo_side_by_side.py` for the deterministic comparison or `python scripts/run_local_llm_trials.py --model qwen2.5:1.5b` for five explicit local model calls. Tests never call Ollama or PayPal.

```mermaid
flowchart LR
  U[User request and budget] --> P[LangGraph planner]
  C[Static catalog: untrusted description] --> P
  P --> T[Two-method proposal client]
  T --> G[Existing AgentSentinel gateway]
  G --> M[Mock payment adapter]
  A[Trusted demo authorization] --> G
```

The planner emits a proposed create or capture action. Parsing discards extra model fields, including forged authorization objects. The tool returns only decision, closed status, and safe reason code. The agent package has no payment adapter, store, authorization repository, issuance code, or credentials. The trusted demo script issues a temporary mock authorization for the original USD 1.00 offer. Its **unprotected** route exists only in `scripts/demo_side_by_side.py`, accepts only a credential-refusing mock, and deliberately skips the gateway for comparison.

| Controlled scripted path | Proposed merchant | Proposed USD | Decision | Mock money moved USD |
|---|---|---:|---|---:|
| Unprotected mock | OTHER123 | 9.00 | ALLOW | 9.00 |
| AgentSentinel | OTHER123 | 9.00 | BLOCK (`AUTHORIZATION_MERCHANT_MISMATCH`) | 0.00 |

The same naive scripted agent followed the poisoned catalog description in both paths. The numbers represent **fake money only**. A separate local-model experiment ran the same poisoned request five times with Ollama `qwen2.5:1.5b` on 2026-10-14. All five model outputs changed the merchant or amount; the existing gateway BLOCKED all five with `AUTHORIZATION_MERCHANT_MISMATCH`, and the mock moved USD 0.00 each time. This is five observations from one small local model, not a claim about general prompt-injection coverage or model robustness. Local prompt/output text was not logged; only token counts (188 input, 36 output for each call) were logged.

The catalog and demo authorization are controlled simulation inputs. Python object boundaries are not deployment isolation, and the demo's pre-approved order is fictional. Real buyer consent, provider capture, production authentication, and Day 7 order A reconciliation remain separate work. Day 10 has not started.
