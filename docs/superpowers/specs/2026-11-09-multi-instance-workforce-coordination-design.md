# Multi-Instance Workforce Coordination — Design

**Date:** 2026-11-09  
**Status:** Approved — implementation plan prepared for review  
**Target branch:** feature/autonomous-ai-workforce-v3  
**Live deployment:** unchanged; main remains untouched

## Intent

Make the workforce backend safe to run as multiple API/worker instances without allowing two workers to execute the same approved task concurrently. Keep Supabase PostgreSQL as the authoritative task store, use Redis for coordination and event distribution, and ensure every connected frontend can observe task progress regardless of which API instance accepted the request.

The chosen design builds on the current V3 implementation: SQLAlchemy task snapshots, Redis Streams approved-task queue, a single process-local queue consumer, and an SSE endpoint backed by process-local task memory.

## Goals

- A task has at most one active execution owner at a time.
- A worker must renew ownership while executing; an expired owner cannot continue writing task state.
- A crashed worker's pending stream message can be reclaimed and retried after ownership expires.
- Task reads from every API instance reflect the latest persisted PostgreSQL state.
- Task updates reach SSE clients connected to any API instance.
- Redis notifications are hints, not the source of truth; clients can recover by rereading PostgreSQL state.
- Repeated delivery failures remain bounded and enter the existing dead-letter flow.
- Redis remains optional until both DATABASE_URL and REDIS_URL are configured.
- The design remains usable with the existing Upstash Free Redis database and Supabase Free Postgres, subject to their quotas and connection limits.

## Non-goals

- No change to agent prompts, provider routing, team composition, robot artwork, or approval policy.
- No browser automation, filesystem access, shell execution, or arbitrary code execution.
- No guarantee of exactly-once provider API calls. A process can fail after an external model request succeeds but before its result is committed. The execution model remains at-least-once with task-level fencing and safe recovery; duplicate external requests may still be billable.
- No broad authentication, user tenancy, or billing system in this slice.
- No production cutover or merge to main as part of the implementation.

## Approaches considered

### A — Redis leases + heartbeats + Pub/Sub (selected)

- Redis Streams remains the durable approved-task queue.
- A worker claims a renewable per-task lease using an atomic token-checked Redis operation.
- A monotonically increasing fencing value is associated with each lease generation and checked by persistent writes.
- The worker periodically renews the lease. If renewal fails, it stops scheduling new work and must not commit subsequent task mutations.
- Task snapshots in PostgreSQL are canonical. Every API instance fetches fresh persisted state rather than trusting its startup cache.
- Task mutation commits publish a compact notification to Redis Pub/Sub after the database commit.
- Every API instance subscribes and wakes its local SSE connections. SSE connections reread PostgreSQL and emit updates; missed Pub/Sub messages are recovered by periodic reconciliation.
- Redis Stream pending-entry recovery is paired with lease expiry; pending-entry reclamation alone never authorizes execution.

**Trade-off:** requires careful coordination across Redis leases, Postgres fencing and event notifications. It offers the best fit for the existing queue and database while keeping normal progress traffic modest.

### B — Redis Streams for jobs and every progress event

- Use a second stream for task events and consumer groups for event delivery.
- This retains events durably in Redis, but creates additional retention, acknowledgement, replay and command-volume decisions.
- It is more complex than needed because PostgreSQL already stores task events inside the task snapshot.

### C — Single worker with database refresh only

- Keep one execution worker and refresh task state from Postgres.
- This is simpler but does not provide safe horizontal execution or reliable cross-instance event wakeups.

**Decision:** Approach A. Redis Pub/Sub will only notify; Postgres remains authoritative. Redis Streams retains queued work. This avoids relying on Pub/Sub's non-durable delivery.

## Architecture and data flow

1. The approval API verifies the existing human approval gate and writes the task as queued to PostgreSQL.
2. It appends the task ID to the Redis approved-task stream.
3. A worker reads the stream entry and requests a lease with a unique owner token.
4. Redis atomically grants the lease only if the task has no unexpired owner, and increments a per-task fencing generation.
5. The worker confirms the durable task state and records the active fencing generation before starting execution.
6. A heartbeat renews the lease at a fraction of the lease TTL (initial proposal: 30-second TTL, renewal every 10 seconds). Renewal succeeds only for the matching owner token and generation.
7. Task mutations are persisted through a fence-aware repository operation. A stale worker whose lease expired or whose generation was superseded cannot overwrite newer state.
8. After each successful task mutation commits, the backend publishes a compact task ID, version and event cursor notification on a Redis Pub/Sub channel.
9. Each API instance's subscriber forwards notifications to its local SSE listeners. The SSE handler rereads the current snapshot from Postgres, emits new events, and periodically reconciles in case a Pub/Sub message was missed.
10. The worker marks terminal state, releases the lease only if it still owns the matching token/generation, and acknowledges/deletes the Redis Stream message.
11. If a worker crashes, heartbeats stop. After lease expiry, another consumer can reclaim the pending stream entry, acquire a higher fencing generation, inspect persisted task state, and apply bounded recovery rules.

## Ownership and write-safety rules

- Lease acquisition, renewal and release use atomic Lua scripts or equivalent Redis transactions. Compare the owner token and generation before extending or deleting a lease.
- Use a per-task monotonically increasing fencing generation. Do not rely on lock TTL alone: a paused worker can resume after its lease expires.
- The persisted task record carries the current execution fencing generation. Every worker-originated state mutation must be conditional on the worker's current generation; stale generations are rejected.
- A worker that loses the lease cancels its execution task, stops starting new provider calls, and cannot persist additional results.
- Database writes and the task-version increment are committed atomically. Notifications are published after commit. If notification publishing fails, periodic SSE reconciliation still discovers the committed state.
- Redis Pub/Sub does not carry secrets, prompts, or full task payloads; publish only task ID, persisted version, and event cursor.
- Approval state and execution status remain validated server-side. The lease is not a substitute for user approval.
- A new worker must inspect the persisted execution state before resuming. It must not blindly restart a completed, rejected, cancelled, or terminal QA result.
- A partial provider call may have succeeded before a crash. The UI and event log must not claim exactly-once execution.

## Multi-instance task-state repository

- Add a monotonically increasing version field to the persisted task row and task snapshot.
- Introduce atomic PostgreSQL/SQLite upsert semantics; avoid the current update-then-insert race.
- Add a conditional save method that checks the worker fencing generation for execution-originated writes.
- Persistent get(task_id) reads the canonical row and refreshes the local cache. In-memory mode retains current behavior.
- Keep the task event list and updated_at in the committed snapshot. SSE reconnect uses event IDs with periodic full-snapshot reconciliation.
- Database transactions must be short. Do not hold a DB transaction open while calling provider APIs.

## Redis keys and channels

Names are configurable using the existing workforce prefix convention.

- workforce:lease:{task_id}: owner token and TTL; acquired/renewed/released atomically.
- workforce:fence:{task_id}: monotonically increasing fencing counter.
- workforce:task-events: Pub/Sub channel containing compact post-commit update hints.
- workforce:approved-tasks: existing Redis Stream for approved task IDs.
- workforce:dead-tasks: existing dead-letter stream.

No task body or provider credential is stored in the lease or Pub/Sub message.

## SSE behavior

- Each API process maintains one Redis Pub/Sub subscriber and a local registry of active task listeners.
- A listener receives local notifications for its task and rereads the authoritative task snapshot from PostgreSQL.
- SSE frames retain the existing task_event and task_update format and Last-Event-ID reconnect semantics.
- A periodic reconciliation (initial proposal: every 15 seconds) rereads Postgres so dropped Pub/Sub notifications do not permanently hide updates.
- If Redis Pub/Sub disconnects, the API keeps existing SSE clients alive using slower DB reconciliation and reports degraded event transport in health/config status.
- If PostgreSQL is unavailable, the API must not claim that a new task update was persisted.

## Recovery behavior

- A worker only claims a pending Stream message after its lease can be acquired.
- If another valid lease exists, leave the stream message pending; do not execute it.
- A worker whose heartbeat fails cancels execution and stops writes.
- After lease expiry, another consumer claims the message and acquires a new fencing generation.
- Reconcile persisted state and the event log before retry. Resume only from an explicit safe recovery point; otherwise mark the task as requiring human review rather than repeating potentially non-idempotent work.
- Preserve the existing bounded delivery count and dead-letter handling.
- Lease release must be owner-token and generation checked so a stale worker cannot release a new worker's lease.

## Configuration and operational limits

- Keep DATABASE_URL and REDIS_URL backend-only.
- Proposed defaults: 30-second lease TTL, 10-second heartbeat, 15-second SSE reconciliation, and 15-minute minimum Redis pending-entry idle threshold. The pending-entry idle threshold must be greater than the expected recovery window and must not be used as the only execution lock.
- Expose only non-secret configuration/status: database backend, queue mode, worker instance count, Pub/Sub connection health, active lease count where safely measurable, and last subscriber error category.
- Monitor Upstash command/connection usage; Pub/Sub and heartbeat traffic count against plan limits.
- Keep Postgres pool sizes and Redis subscriber connections small for the Free-tier footprint.
- Horizontal scaling must remain disabled until distributed lease, fencing, persistent-read, and cross-instance SSE tests pass.

## Testing and acceptance

### Unit tests

- Lease acquisition is exclusive for two distinct workers.
- Owner-token mismatch cannot renew or release a lease.
- Lease expiry permits another worker to acquire a strictly higher fencing generation.
- Stale fencing generations cannot persist task mutations.
- Heartbeat cancellation is triggered when ownership is lost.
- A worker that sees another valid lease leaves the Stream entry pending and does not call execution.
- A crashed worker's pending job can be reclaimed only after lease expiry.
- A completed/rejected/cancelled task is never re-executed on redelivery.
- PostgreSQL/SQLite upsert and version increment are atomic.
- A stale API instance reads the newest task version from the repository.
- Post-commit Pub/Sub notification contains only task ID/version/event cursor.
- SSE receives cross-instance notification and rereads the snapshot.
- SSE periodic reconciliation discovers an update when a Pub/Sub message is intentionally dropped.
- Pub/Sub disconnect degrades to DB reconciliation without killing the API.

### Integration tests

- Two independent worker instances contend for the same task; exactly one acquires the lease and enters execution.
- Kill the lease owner during execution, wait for expiry, and confirm a second worker recovers without stale writes.
- Connect SSE clients to two independent API instances; mutate the task through one worker and observe updates on both.
- Drop Pub/Sub delivery and confirm periodic reconciliation makes the update visible.
- Verify terminal state and Redis stream acknowledgement after successful completion.
- Verify bounded dead-letter behavior after repeated failed deliveries.

### Release acceptance

- CI passes the complete frontend build and backend test suite.
- Two-worker integration tests pass using a disposable Redis and SQLite/Postgres test repository.
- A real Upstash Redis smoke test confirms lease scripts and Pub/Sub permissions.
- A real Supabase connection test confirms atomic versioned writes and restart persistence.
- Staging deploy uses at least two API/worker instances; both observe consistent task state.
- Do not merge PR #2, scale Render horizontally, or change the current live V1 deployment until these acceptance conditions are satisfied.

## Alternatives rejected and why

- Exactly-once provider requests cannot be guaranteed across a network/process crash; use fencing, persisted progress and safe at-least-once recovery instead.
- Pub/Sub as the sole event store is rejected because messages can be missed; Postgres is authoritative and SSE reconciles.
- Redis lease without fencing is rejected because an expired worker may resume after a pause and overwrite newer work.
- Multi-instance deployment before distributed locking and canonical reads is rejected because it risks duplicate agent execution and divergent task state.

## Implementation calls left to the builder

- Implement the lease/fence Lua scripts with versioned key names and bounded TTLs.
- Keep the first stage in the existing V3 branch and use test-first development.
- Do not create paid resources. Reuse existing Upstash Free; live Supabase integration remains blocked until the user completes account project provisioning.
- Run multi-worker integration tests against local disposable Redis; do not claim hosted multi-instance verification until Supabase is connected.
