# PayPal AI Hackathon scope and provenance

Verified on 2026-10-12 against the [official rules](https://paypalaihackathon.devpost.com/rules). The submission period starts **2026-10-01 09:15 Pacific Daylight Time = 2026-10-01 16:15 UTC** and ends 2026-11-12 at 12:00 Pacific Time. An existing project must be significantly updated during the submission period; PayPal Sandbox integration must be central, and an AI tool/model/platform must be part of the project. The judges decide whether a change is significant.

## Pre-hackathon baseline

The latest reachable commit with a **committer timestamp before the start** is `d75579747ac42e5cf6960b2c00f1fdcc4408b43d` (`2026-08-30T08:38:18+05:30`, or `2026-08-30 03:08:18 UTC`). Its author timestamp is the same. The next commit, `77808612f13a4c7b75c28215b87863beecf3e4c4`, is dated 2026-10-07 and is after the cutoff. The `pre-hackathon-baseline` tag points to the August commit; it was created without moving history or changing that commit.

Before the cutoff, the repository contained the master plan and research, synthetic customer-support traces, Pydantic schema and local collector, preprocessing, contextual features, EDA notebook, Gate C preregistration, rules and Isolation Forest, the first classical-ML notebook, tests, and model cards. It **did not** contain a PayPal Sandbox integration, runtime payment gateway, payment policy engine, or dashboard.

After the cutoff, the October 7 and October 12 commits added Stage 1/2 auditing and offline pipeline corrections. This Day 1 commit adds verified scope, license, source references, setup instructions, and repository hygiene. These changes establish provenance and preparation; they do **not** by themselves demonstrate the significant functional PayPal integration required for submission.

## Gate C registration integrity

`5a9dbe229e9bdcd11b6f715daab853908499d5dd` committed `docs/gate_c_preregistration.md` at **2026-08-30 07:36:41 +05:30** (committer and author times). It registered PR-AUC as the primary metric, F1 as secondary, an FPR ceiling of **10%**, a chronological 80/20 trace split, and the 13-feature set. `69b12fcc3051890faffa6037636fc3b4a8b8be9a` committed the first rules/Isolation Forest implementation, evaluation notebook, and model cards at **2026-08-30 08:36:59 +05:30**, about one hour later. The first commit is an ancestor of the second, and the evaluation notebook did not exist in the preregistration commit. These are separate commits in the correct order. The preregistration file did **not** specify a numeric PR-AUC cutoff of 0.5; that number appears in later evaluation material. Git cannot prove no uncommitted evaluation was run earlier, and later data corrections changed the evaluated artifact. No history was rewritten.

## Intended submission boundary

- Use PayPal Sandbox business and personal accounts only during development.
- Convert user purchase requests into proposed actions. The agent cannot grant its own payment authority or access PayPal credentials.
- Enforce deterministic amount, currency, merchant, scope, expiry, duplicate-operation, and buyer-approval checks before any protected action. Anomaly scores remain advisory.
- Keep durable operation identity, avoid duplicate captures, and reconcile uncertain outcomes before any retry.
- Record decisions and outcomes in an auditable interface, with redaction that excludes raw payment tokens.
- Build a runnable demo and honest evaluation. Current synthetic support-agent detector results do not prove payment safety.

Day 1 stops before payment implementation. The 12-stage plan remains in [PROGRESS.md](paypal_hackathon/PROGRESS.md); Stage 3 payment schema work requires separate approval.

## Submission obligations to revisit

The [rules](https://paypalaihackathon.devpost.com/rules) require a functional demo or complete build/run instructions, a public code repository with an open-source license, and a public YouTube demonstration video under three minutes. The rules permit existing work only if significantly updated after the start. Account eligibility and the final submission remain the entrant's responsibility.
