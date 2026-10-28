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

## Known limitations

- This implementation intentionally does not make live PayPal Sandbox requests.
- The approval service is local-only and run on `127.0.0.1` by default.
- In-flight payments already dispatched to a provider are not cancelled by enabling the kill switch; the system documents that as a limitation and does not claim otherwise.
