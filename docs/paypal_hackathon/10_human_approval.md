# Day 10 — Human approval, durable queue, and kill switch

## Architecture overview

The trusted application records a payment proposal, then waits for a human approver. The approval queue stores exact fields from the trusted operation record and binds each decision to that stored intent. Only the trusted application can convert an approved record into a live authorization; the agent never reaches the authorization table directly.

```text
Agent (proposal only) -> Trusted gateway -> Approval queue -> Human approver
                                     \-> exact request fingerprint
                                          \-> saved merchant, amount, currency, action, target
                                                \-> approval status and expiry
                                                       \-> atomic authorization issuance
                                                          \-> gateway recheck before dispatch
```

## Approval state machine

```text
PENDING -> APPROVED
PENDING -> REJECTED
PENDING -> EXPIRED
```

Approved, rejected, and expired states are terminal for the approval record unless a separately audited transition is added. A human decision never overrides a deterministic BLOCK, budget violation, expired authorization, loop limit, or active kill switch.

## Trust boundary

- An agent may propose a payment request.
- The trusted application creates a durable approval record using exact stored values.
- The human reviews the stored details, not the original agent prompt.
- Approval binds to the exact stored request fingerprint.
- Authorization issuance is atomic with the approval transition and can only happen in the trusted application path.
- The gateway rechecks policy and kill-switch state before dispatch.

## Kill switch behavior

When the kill switch is active, new payment actions must return `BLOCK` with reason code `KILL_SWITCH_ACTIVE`. Previously approved authorizations cannot bypass the switch, and the gateway fails closed if the kill-switch state cannot be read. Only authenticated approvers may enable or disable it, and each toggle is audited.

## Operator reason-code schema

Operator actions accept bounded reason codes rather than free-form explanations. Codes must be exact, uppercase ASCII identifiers from the service allowlist (at most 64 characters); empty values, unknown codes, control characters, Unicode, and arbitrary text are rejected without echoing the submitted value.

Approval codes are `APPROVED`, `HUMAN_REJECTED`, `REJECTION_POLICY_CONCERN`, `REJECTION_INTENT_MISMATCH`, `REJECTION_UNEXPECTED_DETAILS`, `REJECTION_DUPLICATE`, and `REJECTION_OTHER`. Kill-switch codes are `KILL_SWITCH_ENABLED`, `KILL_SWITCH_DISABLED`, `KILL_SWITCH_SECURITY_INCIDENT`, and `KILL_SWITCH_MAINTENANCE`. The dashboard uses the fixed `HUMAN_REJECTED` and toggle codes; API callers may select a listed action-appropriate code. The kill-switch API accepts only a JSON boolean for `enabled`; strings, numbers, null, and a missing field are invalid.

## Known limitations

- This implementation intentionally does not make live PayPal Sandbox requests.
- The approval service is local-only and run on `127.0.0.1` by default.
- In-flight payments already dispatched to a provider are not cancelled by enabling the kill switch; the system documents that as a limitation and does not claim otherwise.
