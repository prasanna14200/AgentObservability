# PostgreSQL migration and deployment plan

## Status and scope

This is a migration plan, not an implemented PostgreSQL backend. The current
payment gateway and approval service still use SQLite. The local Docker Compose
profile in this repository is a development/demo profile with a persistent
Docker volume; it is not the final PostgreSQL architecture and must not be
exposed publicly.

The target architecture is a containerized FastAPI service with PostgreSQL as
the authoritative payment-state store, persistent audit storage, independently
tested backups, external secret management, HTTPS, production identity, health
monitoring, and reviewed recovery procedures. No cloud host has been selected
or provisioned.

## Current SQLite boundaries

`SQLitePaymentStore` and `ApprovalStore` share the configured payment SQLite
file. Their tables include operations, authorizations, operation events,
operation attempts, approvals, approval events, and kill-switch state. Safety
critical writes use `BEGIN IMMEDIATE`; the kill switch, operation reservation,
authorization consumption, and dispatch marker depend on SQLite's serialized
write behavior.

Payment telemetry uses a separate SQLite audit database with
`payment_events` and `payment_audit_chain`. Its local checkpoint and lock are
sidecar files. A PostgreSQL migration cannot treat those files as an independent
production trust anchor, nor can it silently re-anchor existing history.

## Required PostgreSQL design

1. Define one reviewed schema and migration owner for operations, approvals,
   authorizations, kill-switch state, and audit metadata. Preserve identifiers,
   unique constraints, state checks, and exact operation fingerprints.
2. Replace `BEGIN IMMEDIATE` assumptions with explicit PostgreSQL transactions,
   row locks, and documented isolation. Serialize per-operation reservation,
   budget checks, authorization consumption, and the kill-switch/dispatch
   boundary. Demonstrate concurrent conflict behavior against PostgreSQL.
3. Keep provider dispatch outside database transactions. Persist an idempotent
   operation claim and `MAY_HAVE_DISPATCHED` marker before network I/O. Unknown
   outcomes remain `UNCERTAIN`; recovery must reconcile with provider evidence
   and must never blindly replay a capture.
4. Map timestamps to timezone-aware `timestamptz`, exact monetary values to
   fixed-precision numeric or integer minor units, structured event fields to
   validated JSON, and generated event identities to PostgreSQL identity
   columns. Validate every conversion and constraint against legacy values.
5. Append audit records transactionally under a database advisory lock or
   equivalent serialization. Keep an independently administered, append-only
   checkpoint outside the database trust boundary. A PostgreSQL transaction
   alone does not prevent an administrator from rewriting the entire chain.
6. Design least-privilege database roles, TLS, credential rotation, connection
   limits, statement timeouts, encrypted backups, retention, and audit access.
   Never pass database credentials to agent code or include them in logs.

## Safe migration and rollback sequence

1. Freeze a schema inventory and migration version. Take an encrypted,
   transactionally consistent backup that includes SQLite WAL state, telemetry
   database, audit checkpoint, and required lock metadata. Verify the backup
   using a restore into an isolated environment before migration.
2. Stop new payment admission and provider dispatch with the kill switch.
   Reconcile all operations in `CLAIMED` or `UNCERTAIN`; unresolved outcomes
   remain blocked and are not retried.
3. Export a read-only snapshot, migrate in deterministic batches preserving
   primary keys and operation states, and record row counts/checksums. Do not
   dual-write SQLite and PostgreSQL.
4. Verify schema constraints, authorization bindings and consumed states,
   operation idempotency keys, kill-switch state, event ordering, audit-chain
   hashes, and independently anchored checkpoint. Any mismatch blocks cutover.
5. Run concurrency, crash, recovery, idempotency, kill-switch, audit-tamper,
   backup/restore, and provider-fake tests against PostgreSQL. No Sandbox or
   live provider call is part of database migration tests.
6. Cut over while dispatch remains disabled. Enable admission only after
   operators verify the restored database, audit anchor, health checks, and
   reconciliation queue. Monitor failures and retain the validated source
   backup read-only.
7. Rollback must not switch to a stale SQLite copy after PostgreSQL accepts
   writes. Disable dispatch, preserve the current authoritative database, and
   perform a reviewed forward repair or a verified reverse migration. Never
   resolve rollback by clearing idempotency or audit state.

## Acceptance evidence required before production

- PostgreSQL integration tests prove atomic one-time authorization, hard BLOCK
  precedence, kill-switch races, unique idempotency, and no lost/duplicated
  operations through injected crashes.
- A restore drill proves payment, approval, kill-switch, dispatch, and audit
  state can be restored consistently within approved RPO/RTO targets.
- Audit verification detects row edits, truncation, rollback, and checkpoint
  mismatch; the checkpoint is controlled independently of the database role.
- Database and application health/readiness checks cover actual critical
  dependencies without treating telemetry failure as payment authorization.
- HTTPS, production authentication/authorization, rate limiting, secret
  management, alerting, monitoring, incident response, and rollback are tested.
- A human-approved Sandbox workflow verifies create, buyer approval, capture,
  provider reconciliation, and durable local records. No live payment is
  permitted by this plan.

## Decisions still required

- Select a host only after verifying compute availability, persistent storage,
  backup capacity, networking, data residency, and total cost. Oracle Cloud
  Always Free is an evaluation candidate, not an assumed available service.
- Compare managed PostgreSQL pricing and operational guarantees against
  self-managed PostgreSQL on a VM. No purchase, credit card, or billing
  commitment is authorized.
- Set RPO/RTO, retention, identity provider, key-management ownership, and
  operational on-call expectations.

Until these controls and tests exist, the application is a local prototype;
automated unit tests and the local named volume do not establish production
durability or deployment readiness.
