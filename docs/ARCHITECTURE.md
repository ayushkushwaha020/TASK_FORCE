# Architecture — V2 Agent Execution and Provider Routing

## Runtime path

```text
React Command Center / Workforce HQ
        | plan / approve / poll
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
- Frontend polling for live execution status and event feed.

## V3 Supabase persistence foundation
- The TaskStore keeps the existing async mutation interface and persists the full task snapshot to `workforce_tasks` through SQLAlchemy when `DATABASE_URL` is configured.
- Supabase Postgres uses asyncpg and SSL; Render should use the Supabase Session Pooler (port 5432). Transaction pooler port 6543 is supported with asyncpg statement caching disabled. SQLite uses aiosqlite for local development. Database credentials are never returned to the browser.
- Task data is stored in the non-exposed `workforce_private` schema. Row-level security is enabled and table/schema privileges are revoked from `PUBLIC`, `anon`, and `authenticated`; only the backend database connection can access the task snapshots.
- API startup restores snapshots. Queued/running/reviewing tasks are marked failed with an `execution_interrupted` event because FastAPI in-process jobs cannot be resumed safely.
- Without `DATABASE_URL`, the app deliberately remains in memory mode. Existing V2 in-memory tasks are not backfilled into the database.

## Current preview limits
- Without `DATABASE_URL`, task state and memory live in process memory. With a Supabase Postgres `DATABASE_URL` configured, snapshots survive restarts; multiple API instances still need a durable worker/locking design.
- Background execution still runs within the API service, not a durable queue. A restart during execution marks that task failed; work is not resumed.
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
