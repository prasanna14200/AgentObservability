# Devpost submission draft

## Project name

AgentSentinel: Runtime Safety and Observability Prototype for AI-Agent
Payments

## Short description

AgentSentinel is a production-shaped prototype exploring how an AI-agent
payment proposal can be separated from payment authority. A trusted local
gateway checks a proposal against deterministic policy and exact-bound
authorization, human approval, and a fail-closed kill switch before a
protected adapter can execute it. A controlled mock demo shows a poisoned
catalog instruction moving fake money through an intentionally unprotected
comparison path, while the AgentSentinel path blocks it without moving fake
money.

## What it does

The purchase agent can propose a PayPal-style order action using a scripted
planner or an already-installed local Ollama model. It has no provider
credentials and cannot issue authorization. The trusted application supplies
authenticated context and routes a validated `PaymentIntent` through the
gateway. An authorization is bound to the approved action, target, merchant,
amount, currency, scope, and operation. Hard policy blocks remain authoritative;
detector results are advisory and can request review but cannot authorize a
payment.

The local prototype also includes a durable human approval queue, expiry,
approval audit events, a kill switch, operation-state recovery, allowlisted
payment telemetry, a local hash-chained audit ledger, and an authenticated
localhost dashboard with decision reasons.

## How it was built

The repository combines Python payment contracts and policy, a SQLite-backed
gateway and approval prototype, a LangGraph purchase-agent demo, FastAPI and
Jinja2 local operator views, and existing rules / Isolation Forest detectors.
PayPal REST integration code supports OAuth, Orders create/read/capture, and
capture lookup. The default demonstration and test workflows use fake
adapters, temporary databases, and spy transports; they do not call PayPal.

## Evidence and honest limitations

- Full local test suite: **331 passed**, with one Starlette/AnyIO deprecation
  warning in the recorded clean-clone run.
- Offline attack suite: **22/22 passed**, with zero PayPal calls.
- Side-by-side demo: the unprotected mock path returns ALLOW and records fake
  USD 9.00 movement; AgentSentinel returns BLOCK with
  `AUTHORIZATION_MERCHANT_MISMATCH` and fake USD 0.00 movement.
- Five local trials with the already-installed `qwen2.5:1.5b` model were all
  blocked after following poisoned catalog text. This is a small controlled
  experiment, not evidence of general prompt-injection resistance.
- The unrelated customer-support anomaly experiment **fails Gate C**: the
  Isolation Forest FPR is 56.9% against a registered 10% ceiling. Its score
  must not be used to authorize payments.
- Day 7 Order A remains unresolved after later provider GETs returned 404.
  This submission does not claim that order completed or was captured.
- The gateway, dashboard, SQLite controls, and local audit chain are
  prototypes, not production infrastructure. The guard decorator does not
  sandbox arbitrary Python code; same-process code must be trusted.
- No independent security review, production deployment, external audit
  anchoring, or provider exactly-once guarantee is claimed.

## Demo and setup

Use the no-credentials, offline steps in the
[demo runbook](DEMO_RUNBOOK.md). The repository is public and licensed under
Apache-2.0. A short demonstration video must be recorded and published by the
entrant; this draft is not a claim that a video or Devpost submission has
already been published.
