# Multi-Instance Workforce Coordination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make V3 safe to run as multiple API and worker instances by coordinating exclusive task ownership, rejecting stale writes, reading canonical PostgreSQL state, and delivering task changes to SSE clients on every API instance.

**Architecture:** Keep Redis Streams as the approved-task queue, add a Redis lease with a monotonically increasing fencing generation and heartbeat, and keep PostgreSQL authoritative for versioned task snapshots. Publish compact post-commit Redis Pub/Sub hints; each API process wakes its local SSE listeners and rereads PostgreSQL, with a 15-second reconciliation fallback.

**Tech Stack:** Python 3.11+ (match the repository runtime), FastAPI, asyncio, redis-py asyncio, Redis Lua scripts, SQLAlchemy 2.x async, PostgreSQL/SQLite, Pydantic 2, unittest.

**Spec:** `docs/superpowers/specs/2026-11-09-multi-instance-workforce-coordination-design.md`

## Global Constraints

- Target only `feature/autonomous-ai-workforce-v3`; keep `main` and `feature/autonomous-ai-workforce-v1` untouched.
- Redis Streams remains the durable approved-task queue; the queue is enabled only when both `DATABASE_URL` and `REDIS_URL` are configured.
- PostgreSQL is the canonical task store; Redis Pub/Sub is a wake-up hint, never the source of truth.
- Lease TTL is 30 seconds; heartbeat interval is 10 seconds; SSE reconciliation interval is 15 seconds.
- Redis pending-entry minimum idle threshold remains 15 minutes and must not be treated as the execution lock.
- Keep the task status API compatible; add the existing queue terminal state `cancelled` to the Pydantic status literal rather than silently treating it as an invalid response.
- Lease acquire, renew, and release must compare owner token and fencing generation atomically.
- Every execution-originated task mutation must be rejected when its fencing generation is stale.
- No provider-call exactly-once guarantee; ambiguous in-flight provider work must not be blindly replayed.
- Never hold a database transaction open during an AI provider call.
- Keep Redis and database credentials backend-only; Pub/Sub payloads contain only task ID, version, and event cursor.
- Keep connection pools small for the existing free-tier resources; do not create paid infrastructure.
- Do not merge PR #2, change the current live V1 deployment, or horizontally scale Render as part of this milestone.
- Hosted Supabase verification is blocked until the user provisions a Supabase project and supplies its backend connection string; do not modify the unrelated existing Study Assistant project.

## Review Focus

- **Expired worker resumes after a pause:** its old generation must be rejected even if its Python task wakes up after another worker acquires the lease. Test stale writes after generation rollover.
- **Two workers acquire at once:** Redis must grant at most one lease and issue strictly increasing generations after expiry. Test concurrent acquisition against real Redis.
- **Two API instances mutate the same snapshot:** optimistic version checks must prevent lost updates; a failed mutation must reload and retry or return an explicit conflict. Test with two repository/store instances.
- **Pub/Sub is lost, duplicated, delayed, or unavailable:** clients must converge to the latest canonical snapshot without exposing duplicate task events. Test dropped notifications, repeated version hints, and subscriber failure.
- **A process stops during provider work or shutdown:** execution must stop writing after lease loss, ambiguous provider work must go to human review, and SSE clients must remain connected through Pub/Sub failure. Test heartbeat cancellation and shutdown cleanup.

---

### Task 1: Redis leases and fencing coordinator

**Files:**
- Create: `backend/app/worker_coordination.py`
- Test: `backend/tests/test_worker_coordination.py`
- Test: `backend/tests/test_multi_instance_integration.py` (real-Redis lease cases are added here in Task 7)

**Interfaces:**
- Produce `TaskLease(task_id: str, owner_token: str, generation: int)`.
- Produce `RedisTaskCoordinator.acquire(task_id: str) -> TaskLease | None`.
- Produce `RedisTaskCoordinator.renew(lease: TaskLease) -> bool`.
- Produce `RedisTaskCoordinator.release(lease: TaskLease) -> bool`.
- Produce `RedisTaskCoordinator.run_with_heartbeat(lease: TaskLease, operation: Awaitable[T]) -> T`; raise `LeaseLostError` and cancel the operation when renewal fails.
- Default `lease_ttl_seconds=30`; default `heartbeat_interval_seconds=10`.
- Redis keys: `workforce:lease:{task_id}` and `workforce:fence:{task_id}`; lease values include the unguessable owner token and generation.

- [ ] **Step 1: Write failing coordinator tests.** Cover acquire returns a lease with generation 1; an occupied lease returns `None`; a different token cannot renew or release; a matching lease can renew and release; a subsequent acquisition receives a strictly higher generation; failed heartbeat cancels the operation and raises `LeaseLostError`.
- [ ] **Step 2: Run the focused tests and confirm failure.**

Run: `cd backend && python -m unittest discover -s tests -p 'test_worker_coordination.py' -v`

Expected: FAIL because the coordinator module and lease API do not yet exist.

- [ ] **Step 3: Implement the coordinator.** Use atomic Lua scripts (or an equivalent single atomic Redis operation) for acquire, renew, and release. The acquire script checks for an existing live lease before incrementing the task fencing counter and storing the owner token/generation with a 30-second TTL. Renew and release compare both token and generation. Do not implement ownership using separate GET/SET calls.
- [ ] **Step 4: Implement heartbeat supervision.** Run the supplied operation and a 10-second renewal loop concurrently. On any renewal error or ownership mismatch, cancel and await the operation, then raise `LeaseLostError`. Normal operation completion stops the heartbeat cleanly.
- [ ] **Step 5: Run focused tests and confirm pass.**

Run: `cd backend && python -m unittest discover -s tests -p 'test_worker_coordination.py' -v`

Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add backend/app/worker_coordination.py backend/tests/test_worker_coordination.py
git commit -m "feat: add renewable task leases and fencing"
```

### Task 2: Atomic versioned task repository and schema migration

**Files:**
- Modify: `backend/app/database.py`
- Modify: `backend/app/schemas.py`
- Create: `supabase/migrations/202611090002_multi_instance_task_fencing.sql`
- Test: `backend/tests/test_database.py`
- Modify: `backend/tests/test_state.py` only where fixtures need the new version field.

**Interfaces:**
- Add `PlanResponse.version: int = 1`.
- Add repository columns `version BIGINT NOT NULL DEFAULT 1` and `execution_fence BIGINT NOT NULL DEFAULT 0`.
- Produce `PersistentTaskRepository.get(task_id: str) -> dict[str, Any] | None`.
- Produce `PersistentTaskRepository.save(record: dict[str, Any], expected_version: int | None, fencing_generation: int | None = None) -> int`; return the committed version, and raise typed `TaskVersionConflict` or `StaleExecutionFence` errors on rejected writes.
- Produce `PersistentTaskRepository.activate_fence(task_id: str, generation: int, expected_version: int) -> int`; persist a strictly newer generation and increment task version atomically.
- `load_all()` must return each row with its authoritative version and execution fence attached.

- [ ] **Step 1: Write failing database tests.** Assert inserts begin at version 1; each successful mutation increments version once; two repositories saving the same expected version cannot both win; a stale expected version raises `TaskVersionConflict`; a worker save with a lower or non-current fence raises `StaleExecutionFence`; activating a higher fence invalidates the previous generation; SQLite restart preserves version and fence.
- [ ] **Step 2: Run the focused tests and confirm failure.**

Run: `cd backend && python -m unittest discover -s tests -p 'test_database.py' -v`

Expected: FAIL because version, fence, and atomic compare-and-save operations do not exist.

- [ ] **Step 3: Add version and fence columns to the SQLAlchemy table.** Keep the existing private schema and connection-pool behavior. Do not remove the current Supabase RLS or privilege revocation setup. Add `cancelled` to the task execution-status Literal because the existing queue already treats it as terminal.
- [ ] **Step 4: Implement atomic repository operations.** Use a transaction and conditional update/upsert so the stored version is incremented only when the caller's expected version matches. Enforce the execution fence in the same database write. Distinguish version conflicts from stale fencing generations. Make `get` and `load_all` return metadata from the table columns, not an untrusted cached copy inside the payload.
- [ ] **Step 5: Add the additive Supabase migration.** Use `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for both columns, backfill existing rows to version 1 and fence 0, and preserve RLS plus revokes. The migration must be safe to apply to the existing V3 table without dropping task data. In `PersistentTaskRepository.initialize`, add a guarded additive SQLite column upgrade for an existing local database (inspect `PRAGMA table_info` before `ALTER TABLE`); never drop or recreate an existing task table.
- [ ] **Step 6: Run focused database tests and confirm pass.**

Run: `cd backend && python -m unittest discover -s tests -p 'test_database.py' -v`

Expected: PASS for SQLite atomicity and restart cases. The SQL migration is syntax-reviewed here; hosted Supabase execution remains a later release gate.

- [ ] **Step 7: Commit.**

```bash
git add backend/app/database.py backend/app/schemas.py backend/tests/test_database.py backend/tests/test_state.py supabase/migrations/202611090002_multi_instance_task_fencing.sql
git commit -m "feat: add atomic task versions and execution fences"
```

### Task 3: Canonical TaskStore reads and fenced mutations

**Files:**
- Modify: `backend/app/state.py`
- Modify: `backend/tests/test_state.py`
- Test: `backend/tests/test_state_coordination.py`

**Interfaces:**
- Add `TaskStore.activate_execution_lease(task_id: str, lease: TaskLease) -> bool`.
- Add optional keyword-only `lease: TaskLease | None = None` to worker-capable mutation methods: `update`, `update_agent`, `update_subtask`, `add_event`, `add_memory`, `add_source`, and `add_message`.
- Persistent `TaskStore.get(task_id)` must read the canonical repository row and refresh the local cache; memory-only mode keeps current in-memory behavior.
- Each successful task mutation commits a new version and publishes only after its database commit; the publisher is injectable and optional.

- [ ] **Step 1: Write failing multi-store tests.** Create two TaskStore instances over the same temporary SQLite database; after instance A changes a task, instance B's next `get` must return the latest version and fields. Assert every mutation increments version. Assert a stale lease cannot update status, agent, subtask, event, memory, source, or message data. Assert a version conflict does not silently overwrite another instance's update.
- [ ] **Step 2: Run focused tests and confirm failure.**

Run: `cd backend && python -m unittest discover -s tests -p 'test_state_coordination.py' -v`

Expected: FAIL because persistent reads still use only process-local memory and mutations have no fence argument.

- [ ] **Step 3: Implement canonical reads and cache refresh.** Add repository-backed `get`; attach row version/fence metadata to the validated snapshot and refresh `_tasks`. In memory-only mode, preserve existing behavior.
- [ ] **Step 4: Centralize mutation and retry semantics.** Route all TaskStore mutation methods through a shared mutation helper that loads the latest record, applies one mutation, and calls atomic `save` with its expected version. Retry a version conflict a bounded number of times by reloading and reapplying the same mutation; after the retry limit, raise an explicit conflict rather than losing data. Pass the supplied lease generation to every execution-originated save.
- [ ] **Step 5: Activate a lease fence before worker execution.** Persist the new generation through `activate_execution_lease` and reject a generation that is not newer than the previously stored value. Do not let a worker begin provider work until activation succeeds.
- [ ] **Step 6: Preserve event and task snapshot compatibility.** Keep current `PlanResponse` fields and event ordering; version is additive. Ensure a missing version in older stored payloads is read as version 1.
- [ ] **Step 7: Run state and persistence regression tests.**

Run: `cd backend && python -m unittest discover -s tests -p 'test_state*.py' -v`

Expected: PASS, including the existing restart and in-flight task tests updated to the new lease-aware recovery rules.

- [ ] **Step 8: Commit.**

```bash
git add backend/app/state.py backend/tests/test_state.py backend/tests/test_state_coordination.py
git commit -m "feat: read canonical task state and reject stale writes"
```

### Task 4: Lease-aware queue processing and safe execution cancellation

**Files:**
- Modify: `backend/app/worker_queue.py`
- Modify: `backend/app/execution.py`
- Modify: `backend/app/state.py` only if the TaskStore lease integration needs a small interface adjustment.
- Modify: `backend/tests/test_worker_queue.py`
- Create: `backend/tests/test_execution_fencing.py`

**Interfaces:**
- Change execution entry point to `execute_task(task_id: str, lease: TaskLease | None = None) -> None`.
- `RedisStreamTaskQueue` receives a `RedisTaskCoordinator` using its configured Redis client.
- A message is acknowledged only after a persisted terminal state or a bounded dead-letter decision; failure to acquire/renew a lease leaves it pending.
- Keep `MAX_DELIVERIES = 3` and `MIN_IDLE_CLAIM_MS >= 900000` unless the spec is explicitly revised.

- [ ] **Step 1: Write failing queue and execution tests.** Assert two queue workers cannot both call `execute_task`; a worker that cannot acquire a lease leaves the entry pending and does not acknowledge it; a worker activates its DB fence before execution; lease loss cancels execution and does not acknowledge; an old lease cannot write after another worker has acquired a newer generation; terminal redelivery is acknowledged without execution.
- [ ] **Step 2: Write crash-recovery tests.** A redelivered task still in `queued` may start after lease acquisition. A task persisted as `running` after a crash is not blindly restarted: transition it to `revision_required`/human review, append a recovery event, and acknowledge only after that decision is committed. Do not change interrupted active tasks back to `queued` during every API instance startup.
- [ ] **Step 3: Run focused tests and confirm failure.**

Run: `cd backend && python -m unittest discover -s tests -p 'test_worker_queue.py' -v` and `cd backend && python -m unittest discover -s tests -p 'test_execution_fencing.py' -v`

Expected: FAIL because the current worker has no lease, the startup path requeues active tasks, and execution writes are not fenced.

- [ ] **Step 4: Integrate lease acquisition into queue processing.** Acquire an exclusive lease before transitioning task state or invoking execution. When another owner holds the lease, leave the stream entry pending. Activate the generation in TaskStore before calling `execute_task`.
- [ ] **Step 5: Supervise execution with the heartbeat.** Run execution through `run_with_heartbeat`; cancel and await execution on lease loss. Ensure cancellation is not converted into a normal failed result and do not acknowledge a non-terminal task after cancellation.
- [ ] **Step 6: Thread the lease through every execution-originated mutation.** Pass the same lease through specialist work, reassignment/recovery, research, deliberation, compilation, QA, revision, status, and event writes. Every TaskStore mutation performed by the execution pipeline must carry it, including error paths.
- [ ] **Step 7: Make crash recovery conservative.** Remove startup logic that blindly resets `running` tasks to `queued`. Only a task still durably queued before provider work begins may be started on redelivery. For a persisted running task without a provable safe checkpoint, save `revision_required`, `qa_status=rejected`, and a human-review event; never reissue an ambiguous provider request automatically.
- [ ] **Step 8: Keep delivery and dead-letter behavior bounded.** Preserve the current 3-delivery limit and the 15-minute pending claim threshold; never let `XAUTOCLAIM` itself grant execution ownership. Release the lease in a `finally` block after execution or recovery; the Lua compare-token/generation check must make a late release from an old worker harmless.
- [ ] **Step 9: Run the focused tests and the full backend suite.**

Run: `cd backend && python -m unittest discover -s tests -v`

Expected: PASS; lease loss leaves no stale worker mutation and terminal messages are acknowledged exactly once at the queue layer.

- [ ] **Step 10: Commit.**

```bash
git add backend/app/worker_queue.py backend/app/execution.py backend/app/state.py backend/tests/test_worker_queue.py backend/tests/test_execution_fencing.py
git commit -m "feat: fence queue workers and recover interrupted tasks safely"
```

### Task 5: Post-commit task-change Pub/Sub and local event hub

**Files:**
- Create: `backend/app/task_events.py`
- Modify: `backend/app/state.py`
- Create: `backend/tests/test_task_events.py`

**Interfaces:**
- Produce `TaskChange(task_id: str, version: int, event_cursor: str | None)`.
- Produce `TaskEventHub.register(task_id: str) -> asyncio.Queue[TaskChange]`, `unregister(task_id: str, queue: asyncio.Queue[TaskChange]) -> None`, and `notify(change: TaskChange) -> None`. Use a bounded, coalescing listener queue because messages are wake-up hints, not a durable event history.
- Produce `RedisTaskEventBus.publish(change: TaskChange) -> None` and `run_subscriber(hub: TaskEventHub, stop_event: asyncio.Event) -> None`.
- Channel: `workforce:task-events`; JSON payload contains only `task_id`, `version`, and `event_cursor`.

- [ ] **Step 1: Write failing event bus tests.** Assert a committed change is published with only the three allowed fields; local listeners for the matching task are woken; listeners for another task are not; repeated or out-of-order hints do not become authoritative state; Redis subscriber disconnect is reported but does not terminate the local hub.
- [ ] **Step 2: Run focused tests and confirm failure.**

Run: `cd backend && python -m unittest discover -s tests -p 'test_task_events.py' -v`

Expected: FAIL because no event hub or Pub/Sub bus exists.

- [ ] **Step 3: Implement the local event hub.** Maintain per-task listener queues, fan out only matching task changes, and clean up empty listener sets when clients disconnect.
- [ ] **Step 4: Implement the Redis Pub/Sub publisher and subscriber.** Use one subscriber per API process, parse and validate the small payload, and dispatch hints to the local hub. Reconnect with bounded backoff after subscriber errors. Never put prompts, provider output, credentials, or full task snapshots on the channel.
- [ ] **Step 5: Publish only after persistence commits.** Inject the publisher into TaskStore; after a successful repository commit, publish `TaskChange(task_id, version, last_event_id)`. A Pub/Sub failure must be logged and exposed as degraded transport but must not roll back a committed task mutation or claim it was not persisted.
- [ ] **Step 6: Run focused tests and confirm pass.**

Run: `cd backend && python -m unittest discover -s tests -p 'test_task_events.py' -v`

Expected: PASS, including safe payload and subscriber-reconnect cases.

- [ ] **Step 7: Commit.**

```bash
git add backend/app/task_events.py backend/app/state.py backend/tests/test_task_events.py
git commit -m "feat: broadcast committed task changes across API instances"
```

### Task 6: Cross-instance SSE reconciliation and lifecycle integration

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_sse.py`
- Modify: `backend/tests/test_worker_queue.py` only if app lifespan fixtures need adjustment.

**Interfaces:**
- Add `SSE_RECONCILE_SECONDS = 15`.
- The SSE generator registers with `TaskEventHub`, loads an initial canonical snapshot, then reloads canonical state after a local notification or a 15-second timeout.
- The existing `task_event` and `task_update` frame names, payload shape, and `Last-Event-ID`/query `after` reconnect behavior remain compatible.
- App lifespan starts and stops the Redis Pub/Sub subscriber and closes its connections cleanly.
- Health/config responses expose non-secret storage, queue, lease, and Pub/Sub health only.

- [ ] **Step 1: Write failing SSE tests.** Assert an event published by one app instance wakes an SSE listener attached to another instance; the listener fetches the canonical snapshot rather than trusting notification contents; a deliberately dropped Pub/Sub message is visible after the 15-second reconciliation timeout; reconnect from `Last-Event-ID` emits only later events; Pub/Sub failure does not terminate the stream.
- [ ] **Step 2: Run focused tests and confirm failure.**

Run: `cd backend && python -m unittest discover -s tests -p 'test_sse.py' -v`

Expected: FAIL because the SSE loop currently polls local TaskStore state and has no cross-instance notification wait.
- [ ] **Step 3: Replace one-second polling with notification-or-timeout waiting.** Register a per-task queue for the stream lifetime, wake on matching Pub/Sub hints, and always reread PostgreSQL. Reconcile every 15 seconds even when no notification arrives. Unregister in a `finally` block.
- [ ] **Step 4: Preserve SSE compatibility.** Retain current event IDs, `task_event` and `task_update` names, terminal-state close behavior, comments/heartbeat, and reconnect fallback when a prior event ID is no longer in the retained list.
- [ ] **Step 5: Integrate app startup and shutdown.** Initialize TaskStore, initialize the existing Redis queue/coordinator, attach the event publisher, start one Pub/Sub subscriber, and stop/cancel/close them in reverse order. A Pub/Sub outage should leave database reads and SSE reconciliation running.
- [ ] **Step 6: Expose safe operational status.** Extend `/health` and `/api/config/status` with configured backend, lease TTL/heartbeat, Pub/Sub connected/degraded state, and last error category. Do not return Redis URL, tokens, passwords, prompts, or provider data.
- [ ] **Step 7: Run SSE and API regression tests.**

Run: `cd backend && python -m unittest discover -s tests -p 'test_sse.py' -v` and `cd backend && python -m unittest discover -s tests -v`

Expected: PASS; API remains available during Pub/Sub outage and cross-instance SSE converges on PostgreSQL state. The local hub must coalesce a burst of hints without unbounded per-client memory growth.

- [ ] **Step 8: Commit.**

```bash
git add backend/app/main.py backend/tests/test_sse.py
git commit -m "feat: reconcile SSE updates across API instances"
```

### Task 7: Two-worker integration, operational documentation, and release gates

**Files:**
- Create: `backend/tests/test_multi_instance_integration.py`
- Modify: `backend/README.md`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/roadmap.md`
- Modify: `docs/acceptance-checklist.md`
- Modify: the existing backend CI workflow under `.github/workflows/` only after identifying its actual filename and current test command.

**Interfaces:**
- Integration tests use `WORKFORCE_TEST_REDIS_URL` for a disposable Redis instance and temporary SQLite databases; skip only when no integration Redis URL is configured.
- Tests must use two independent coordinator/queue objects and two independent TaskStore instances, not one shared in-process object.
- Document migration `202611090002_multi_instance_task_fencing.sql`, the 30s/10s/15s timing, required backend env vars, and the fact that Supabase must be provisioned and migrated before durable multi-instance mode is enabled.

- [ ] **Step 1: Write real-Redis integration tests.** Test concurrent acquire by two workers (only one succeeds); expiry then reacquire gives a higher generation; stale renew/release and stale database writes fail; kill/cancel the lease owner and verify the next owner can take over only after lease expiry; verify the previous owner's late write is rejected.
- [ ] **Step 2: Add two-instance store and SSE integration tests.** Connect two independent TaskStore instances to the same temporary database and two event-bus subscriber instances to Redis. Commit through one store and verify the other API listener reloads the latest version, including when one Pub/Sub hint is dropped and the reconciliation timeout fires.
- [ ] **Step 3: Add bounded recovery and terminal-idempotency cases.** Verify ambiguous running work becomes human review, terminal tasks are never executed on redelivery, and repeated failures enter the existing DLQ after the third permitted delivery.
- [ ] **Step 4: Run integration tests against a disposable Redis.**

Run: `cd backend && WORKFORCE_TEST_REDIS_URL=redis://127.0.0.1:6379/15 python -m unittest discover -s tests -p 'test_multi_instance_integration.py' -v`

Expected: PASS with local disposable Redis running. Do not substitute the Upstash production database for destructive integration tests.

- [ ] **Step 5: Wire integration tests into the existing CI workflow.** Inspect the actual workflow file first, then add a Redis service container and set `WORKFORCE_TEST_REDIS_URL` for the integration test command. Preserve the existing frontend build and backend test jobs.
- [ ] **Step 6: Update documentation and acceptance checklist.** Distinguish unit tests, local Redis integration, real Upstash smoke, and hosted Supabase validation. Mark Supabase/hosted multi-instance checks blocked until the user provisions and migrates a Supabase project. Do not mark deployment-scale acceptance complete based solely on local tests.
- [ ] **Step 7: Run final verification.**

Run:
```bash
cd backend && python -m unittest discover -s tests -v
# From repository root, run the existing frontend build command documented in package.json/CI.
# Run Redis integration tests with WORKFORCE_TEST_REDIS_URL set to a disposable local Redis.
```

Expected: all existing and new unit tests pass; two-worker integration passes with disposable Redis; frontend build passes; CI is green for the final V3 commit. Real Upstash script/Pub/Sub smoke and hosted Supabase write/restart verification remain separately recorded until performed.

- [ ] **Step 8: Commit.**

```bash
git add backend/tests/test_multi_instance_integration.py backend/README.md README.md docs/architecture.md docs/roadmap.md docs/acceptance-checklist.md .github/workflows
git commit -m "test: verify multi-instance workforce coordination"
```

## Release acceptance checklist

- [ ] Two distinct workers cannot execute one task concurrently.
- [ ] Lease acquire, renew, and release are atomic and token/generation checked.
- [ ] A lease generation strictly increases after expiry and stale writes are rejected at the database boundary.
- [ ] The worker cancels execution and stops writes when heartbeat renewal fails.
- [ ] A valid lease is required before a worker processes a queue message.
- [ ] Stream pending-entry recovery is paired with lease acquisition; the 15-minute idle threshold is not an execution lock.
- [ ] Running tasks with ambiguous provider-call state go to human review rather than automatic replay.
- [ ] Task versions increment atomically; optimistic conflicts cannot silently overwrite another API instance's update.
- [ ] Persistent task reads always reflect the latest PostgreSQL row.
- [ ] Post-commit notifications contain only task ID, version, and event cursor.
- [ ] SSE clients on separate API instances receive updates; dropped Pub/Sub notifications are repaired by 15-second reconciliation.
- [ ] Pub/Sub failure degrades transport but does not stop canonical DB reads or kill SSE connections.
- [ ] Existing approval gates, terminal-state handling, event format, and frontend behavior remain compatible.
- [ ] Full backend tests, Redis integration tests, frontend build, and CI pass.
- [ ] Real Upstash lease/PubSub smoke and real Supabase migration/write/restart checks are completed before any hosted scale-out.
- [ ] PR #2 remains draft/open; no merge, production cutover, or horizontal scaling occurs in this milestone.
