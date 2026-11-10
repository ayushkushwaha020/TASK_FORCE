# Acceptance Checklist

This checklist tracks the requirements in the Autonomous AI Workforce master specification.
Status key (not checklist items): VERIFIED = implemented and covered by automated tests or current deployment evidence; PARTIAL = implemented in code or present in the preview, but not verified end-to-end in the live environment; NOT STARTED = not implemented yet; BLOCKED = waiting on an external approval, credential, account resource, or decision.

## V1 — Foundation and planning

- [x] React + TypeScript command-center and workforce workspace.
- [x] FastAPI planning API and backend provider-status endpoint.
- [x] Task classification and specialist-team planning.
- [x] Provider-backed planning can propose 3–20 agents and includes a QA/verifier role.
- [x] Demo planning is explicitly labelled; demo plans cannot be executed as live provider work.
- [x] Human approval/rejection gate before internal execution.
- [x] Server-side provider credentials; browser never receives API keys.
- [x] Render frontend and backend services are live on the V1 feature branch.
- [~] Team planning without a configured provider uses deterministic role templates; it is not a free-form autonomous planner.
- [ ] User accounts, cross-device identity, and workspace isolation.

## V2 — Multi-agent execution preview

- [x] Provider-backed specialist execution and parallel workstreams.
- [x] Bounded execution: maximum 8 active specialists, 4 concurrent provider requests, and 2 simultaneous task executions per process.
- [x] Shared task memory and structured agent-to-director messages.
- [x] Team deliberation records agreements, disagreements, and unresolved assumptions.
- [x] Integrated deliverable drafting.
- [x] Independent AI QA agent can reject and request one bounded revision round.
- [x] One bounded workstream recovery/reassignment attempt.
- [x] Frontend polls task updates and displays the event timeline, memory, messages, QA, and deliverable.
- [x] Markdown export and browser Print / Save as PDF.
- [~] Live research integration through capped Tavily Search is implemented; the currently deployed environment has no Tavily key, so live web research is not active.
- [~] OpenAI-compatible, Gemini, and Anthropic provider adapters and connection-test code are implemented; no live provider-backed end-to-end task has been verified with configured production credentials.
- [~] New Scout, Forge, Orbit, and Sentinel robot artwork and state animations are deployed to the V1 Render frontend. Communication, meeting, and task-specific gesture animations are not implemented.
- [ ] Live provider-backed task execution verified end-to-end with a configured key.
- [x] Server-Sent Events task stream with EventSource frontend updates and polling fallback (single API process).
- [ ] Full responsive/manual accessibility review of the live UI.
- [ ] File upload and secure file ingestion.
- [ ] General browser navigation and page extraction.
- [ ] Sandboxed code execution and deterministic test evidence.
- [ ] DOCX generation and complete document-production pipeline.

## V3 — Supabase persistence foundation

- [x] SQLAlchemy async persistence adapter for PostgreSQL and SQLite.
- [x] Supabase URL normalization and SSL handling.
- [x] Supabase shared transaction pooler support with prepared-statement caches disabled.
- [x] Private `workforce_private` schema, RLS enablement, and revocation of `PUBLIC`, `anon`, and `authenticated` access.
- [x] Task snapshot serialization includes task plan, agent states, subtasks, events, messages, shared memory, sources, QA, and deliverable.
- [x] Startup restoration and explicit marking of interrupted in-process jobs as failed.
- [x] Database URL unit tests and SQLite task-store persistence tests pass in CI.
- [x] Versioned SQL migration script prepared for the Supabase project.
- [BLOCKED] Provision a separate Supabase Free project; account approval/login flow declined the project-creation request.
- [BLOCKED] Add the Supabase connection URI to the Render backend environment.
- [ ] Run the migration against an actual Supabase database.
- [ ] Verify live Render-to-Supabase connectivity, create/read/update persistence, and restart recovery.
- [ ] Backfill any pre-existing in-memory tasks (not currently supported).
- [x] Redis Streams queue with consumer group, pending-message reclamation, bounded delivery attempts, and dead-letter handling (single API worker).
- [x] Provisioned the Upstash Free Redis database `autonomous-ai-workforce` in Singapore and verified Redis Streams consumer-group, enqueue, read, pending, acknowledge, and cleanup commands.
- [x] Stored the Upstash TLS `REDIS_URL` as a secret environment variable on the existing Render backend service; the current service still runs V1, so V3 queue activation awaits the V3 deployment and Supabase `DATABASE_URL`.
- [x] Added BIGINT task version and execution-fence columns, atomic compare-and-save, strictly newer fence activation, and an additive Supabase migration.
- [x] Added a guarded SQLite upgrade for legacy task tables; tests verify legacy rows survive and start at version 1 / fence 0.
- [x] Persistent TaskStore reads the canonical database row and accepts lease values on every mutation method; stale generations are rejected before writes.
- [x] Added multi-repository concurrency, duplicate-create, deleted-row resurrection, lease activation, and stale-mutation tests.
- [~] Redis lease coordinator acquire/renew/release and heartbeat cancellation are unit-tested with a fake Redis client; live Redis integration and queue-worker wiring remain pending.
- [~] Startup recovery still has the earlier queued-state recovery behavior; conservative lease-aware human-review handling for ambiguous in-flight provider work remains pending.
- [ ] Wire lease acquisition, activation, heartbeat supervision, and lease arguments through the Redis Stream worker and complete execution pipeline.
- [ ] Run two-worker integration tests against disposable Redis and validate real Upstash Lua/lease behavior.
- [ ] Multi-instance Redis Pub/Sub event fanout and 15-second SSE reconciliation.
- [ ] Run Supabase migration and verify live PostgreSQL create/read/update/restart plus fencing behavior.
- [ ] Verify multi-instance staging deployment before enabling horizontal scaling.
- [ ] Cross-instance Server-Sent Events wakeups backed by Pub/Sub and canonical database reconciliation.
- [ ] Persistent project memory and permission-scoped agent memory.

## V4 — Tools, governance, and production readiness

- [ ] Permission-scoped browser and search tools with source capture.
- [ ] Isolated code runner with network/filesystem/resource restrictions.
- [ ] File ingestion, validation, object storage, and PDF/DOCX generation.
- [ ] Human approval queue for external/destructive actions.
- [ ] Audit trail, policy engine, and scoped action permissions.
- [ ] User identities, quotas, and workspace isolation.
- [ ] Per-task token/cost budgets and cross-process provider quotas.
- [ ] Security, load, failure-injection, and full end-to-end tests.
- [ ] Production deployment verification for the Supabase-backed branch.

## Current deployment facts

- Render frontend and backend services currently track `feature/autonomous-ai-workforce-v1`.
- V3 code is in draft PR #2 targeting V1; it is not merged to `main`.
- The Render backend is not yet configured with Supabase `DATABASE_URL`; deployed task storage remains process memory until that secret is configured and a new V3 backend deployment is explicitly promoted.
- The new robot frontend is live; the V3 Supabase persistence code is not yet the deployed backend.
- Main branch remains untouched.

## Release rule

A feature is not marked tested or verified solely because code or a UI element exists. Record the actual test, live integration, or deployment evidence.
