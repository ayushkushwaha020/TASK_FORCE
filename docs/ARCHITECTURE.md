# Architecture — V2 Agent Execution and Provider Routing

## Runtime path

```text
React Command Center / Workforce HQ
        | plan / approve / SSE stream (poll fallback)
        v
FastAPI API
  |-- task planner and dynamic team composer
  |-- human approval gate
  |-- SQLAlchemy task repository (Supabase Postgres / SQLite when DATABASE_URL is set)
  |-- multi-protocol provider router
  |     |-- OpenAI-compatible Chat Completions
  |     |-- Google Gemini generateContent
  |     |-- Anthropic Messages
  |     |-- per-provider local budgets, retries, cooldown and failover
  |-- specialist agents (up to 8 active; 4 provider calls in parallel)
  |-- shared task memory and structured messages
  |-- team director deliberation
  |-- deliverable compiler
  |-- independent QA reviewer + one revision round
        |
        v
Task snapshot: events, status, messages, memory, QA verdict, deliverable
```

## Human approval lifecycle
1. User submits an instruction.
2. Backend creates a dynamic team and workstream plan; task status becomes `awaiting_approval`.
3. User approves or rejects the plan.
4. Only an approved provider-backed plan can start execution.
5. Specialists work in parallel; completed findings enter shared memory.
6. The director compares specialist contributions and records agreements and unresolved differences.
7. The editor compiles a draft deliverable.
8. Independent QA approves or rejects the draft. A rejected draft receives one bounded revision round. If QA still rejects, the task remains `revision_required`.

## Provider adapters and routing
- `openai_compatible`: Chat Completions request/response translation.
- `gemini_generate_content`: Gemini REST `generateContent` with system instructions, content roles, generation configuration, and `x-goog-api-key` authentication.
- `anthropic_messages`: Anthropic Messages API with top-level system prompt, user/assistant turns, `max_tokens`, `x-api-key`, and configurable API-version header.
- Multiple endpoints are configured by server-only `AI_PROVIDERS_JSON`; the legacy single-provider variables remain supported.
- Providers are ordered by priority. Optional local request caps, bounded retries, per-provider cooldowns, fallback models, and failover are supported.
- Each network attempt, including retry/fallback, consumes a local request-budget slot.
- Local counters are process-local, not authoritative provider quota information. Provider quota, billing, free-tier rules, and terms still apply; quota bypass is not a design goal.
- Credentials are stored only in backend hosting environment variables and never returned from configuration or provider-test endpoints.
- A user-triggered provider connection test sends one tiny prompt per configured endpoint, counts it against local budgets, and reports only status/latency. It does not run automatically.

## Implemented V2 behaviour
- Provider-backed plan generation.
- Explicit human approval endpoint.
- Multi-protocol provider routing across OpenAI-compatible, Gemini and Anthropic APIs.
- Up to eight active specialists with a four-call provider concurrency limit.
- Shared task memory and agent-to-director messages.
- Team deliberation with contradictions and open questions.\n- Bounded live Tavily Search for research tasks, source capture and shared-memory injection.\n- Source-ID citation integrity check and explicit no-live-search disclosure when sources are unavailable.\n- Markdown download and browser Print / Save PDF.
- Independent QA verdict and one revision round.
- Recovery by reassigning a failed specialist workstream once to a successful team member.
- Frontend Server-Sent Events for live execution status and event feed, with polling fallback.

## V3 persistence, worker fencing, and cross-instance event fanout
- TaskStore persists full task snapshots through SQLAlchemy when DATABASE_URL is configured. PostgreSQL is canonical; versioned compare-and-save and execution-fence predicates reject stale writes.
- When both DATABASE_URL and REDIS_URL are present, approved tasks enter a Redis Stream. Consumers acquire token-checked Redis leases, persist a strictly newer generation before execution, renew the lease with heartbeat supervision, and acknowledge only after terminal persistence. Ambiguous interrupted provider work is routed to human review rather than replayed blindly.
- After each successful task snapshot commit, TaskStore emits a compact TaskChange containing task ID, committed version, and latest event cursor. Redis Pub/Sub channel workforce:task-events contains only these three fields; it never carries prompts, provider output, credentials, or full task snapshots. Publish failure is non-fatal because SSE reconciliation remains active.
- Each API process maintains one Redis Pub/Sub subscriber and a bounded, coalescing local event hub. Subscribers reconnect with capped backoff. A hint only wakes listeners; it is never accepted as authoritative task state.
- The /api/tasks/{task_id}/stream route retains the task_event and task_update frame formats, Last-Event-ID reconnect behavior, heartbeat, and terminal close behavior. It rereads canonical TaskStore state after a notification and at least every 15 seconds when no notification arrives. The browser keeps EventSource with polling fallback.
- Supabase Postgres uses asyncpg and SSL; Render should use the Supabase Transaction Pooler (port 6543), with prepared-statement caches disabled. Session pooler port 5432 is also supported. SQLite/aiosqlite is used for local development and integration tests. Database and Redis credentials remain backend-only.
- Task data is stored in the non-exposed workforce_private schema. Row-level security is enabled and table/schema privileges are revoked from PUBLIC, anon, and authenticated; only the backend database connection can access task snapshots.
- Startup restores snapshots, preserves durable queued jobs, checks active Redis leases, and routes ambiguous in-flight work to human review. Pub/Sub subscription starts in the background so a subscriber outage does not block API startup; health/config surfaces non-secret transport state.
- Without DATABASE_URL, the app deliberately remains in process-local memory mode. Existing V2 in-memory tasks are not backfilled into the database.
- Local two-instance Redis/SQLite tests pass, but real Upstash Pub/Sub permission/connectivity and hosted Supabase migration/write/restart verification remain release gates. The hosted deployment remains on V1; local tests do not authorize scale-out or promotion.

## Current preview limits
- Without `DATABASE_URL`, task state and memory live in process memory. With a Supabase Postgres `DATABASE_URL` configured, snapshots survive restarts; multiple API instances still need a durable worker/locking design.
- Without `REDIS_URL`, approved tasks use FastAPI background tasks and an API restart marks in-flight tasks failed. With both `DATABASE_URL` and `REDIS_URL`, approved tasks are enqueued in Redis Streams and reclaimed after the configured pending-message idle threshold. The current worker remains in the API process and is single-instance only.
- Native adapters currently handle text prompt/response calls. Native multimodal file uploads, browser/search tools, function calling, and terminal/code-execution tools are not yet connected.
- No agent has browser, filesystem, terminal, shell, or external-service tools.
- Provider output is model-generated text. The system must not claim code execution, website research, external verification, or completion of real-world actions.
- AI QA is a separate model review, not deterministic proof of correctness.
- The API has a lightweight in-memory rate guard; production still needs durable user identity, quotas, audit, and anti-abuse protections.

## Research evidence flow

After approval, research tasks call Tavily Search with a maximum of four basic queries, five results per query, and two concurrent HTTP requests. Raw page content is disabled. Results are deduplicated and stored with source IDs, titles, URLs, snippets, query text, optional scores, and provider-reported search credits. The source register is injected into specialist context, the report editor, and independent QA. QA checks that citations use source IDs actually collected. If no source results exist, the final report must disclose the absence of live web search. Snippets are partial evidence leads, not a substitute for opening and reviewing full source pages.

The frontend exposes the source register as clickable URLs and supports Markdown download plus browser print-to-PDF export.

## Production target
- Frontend and API: Render for the current hackathon preview.
- Durable task store: PostgreSQL.
- Queue and event fanout: Redis or a durable job system.
- File storage: private object storage.
- Tool execution: isolated, permission-scoped sandboxes.
- Provider routing with measured token/cost budgets, provider-native usage telemetry, and quotas across processes.

## Trust boundaries
- Browser never receives provider secrets.
- Uploaded files and external content will be treated as untrusted input.
- Sensitive, external, destructive, or irreversible operations require a separate explicit human approval.
- QA status is evidence-based; a successful text review is not the same as code/test execution.

## API reference links
- Gemini generateContent: https://ai.google.dev/api/generate-content
- Gemini API keys: https://ai.google.dev/gemini-api/docs/api-key
- Anthropic Messages: https://platform.claude.com/docs/en/api/messages/create
