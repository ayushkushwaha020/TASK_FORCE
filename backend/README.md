# Backend

FastAPI task-planning and approval-gated multi-agent execution service.

## Run locally
```bash
python -m venv .venv
# Activate the environment
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

## API
- `GET /health` — health check and API version.
- `GET /api/config/status` — provider names, protocols, models, local budget usage, and status; never returns secrets.
- `POST /api/providers/test` — user-triggered, one-request connectivity test per configured provider; reports status/latency without returning credentials.
- `POST /api/tasks/plan` — create a proposed team plan. JSON: `{"instruction":"..." }`.
- `GET /api/tasks/{task_id}` — retrieve task state, events, shared memory, messages, QA, and deliverable.
- `GET /api/tasks/{task_id}/stream` — Server-Sent Events stream for task activity and status updates; clients can reconnect using `Last-Event-ID`.
- `POST /api/tasks/{task_id}/approval` — JSON `{"decision":"approve"}` or `{"decision":"reject","reason":"..." }`.

## Provider router

The same workforce pipeline can use these native protocols:

| Protocol | Provider examples | Endpoint format |
|---|---|---|
| `openai_compatible` | OpenAI and compatible gateways | `/v1/chat/completions` |
| `gemini_generate_content` | Google Gemini API | `/v1beta/models/{model}:generateContent` |
| `anthropic_messages` | Anthropic Claude API | `/v1/messages` |

The router accepts multiple endpoints through the server-only `AI_PROVIDERS_JSON` environment variable. Each provider entry requires `name`, `protocol`, `base_url`, `api_key`, and `model`. Optional fields are:
- `priority`: lower values are tried first.
- `max_requests_per_hour`: a local per-process request cap.
- `fallback_model`: another model on the same provider endpoint.
- `max_output_tokens`: per-request output cap (256–65,536).
- `max_output_field`: for OpenAI-compatible endpoints, choose `max_tokens` (default) or `max_completion_tokens`.
- `api_version`: used by Anthropic Messages; defaults to `2026-10-21`.

Example (replace placeholder keys and model IDs with values from your own provider accounts):
```json
[
  {
    "name": "gemini-main",
    "protocol": "gemini_generate_content",
    "base_url": "https://generativelanguage.googleapis.com/v1beta",
    "api_key": "YOUR_GEMINI_KEY",
    "model": "YOUR_GEMINI_MODEL",
    "priority": 10,
    "max_requests_per_hour": 80
  },
  {
    "name": "claude-main",
    "protocol": "anthropic_messages",
    "base_url": "https://api.anthropic.com",
    "api_key": "YOUR_ANTHROPIC_KEY",
    "model": "YOUR_CLAUDE_MODEL",
    "api_version": "2026-10-21",
    "max_output_tokens": 4096,
    "priority": 20,
    "max_requests_per_hour": 40
  },
  {
    "name": "openai-compatible",
    "protocol": "openai_compatible",
    "base_url": "https://api.openai.com/v1",
    "api_key": "YOUR_PROVIDER_KEY",
    "model": "YOUR_MODEL_ID",
    "priority": 30,
    "max_requests_per_hour": 100
  }
]
```

For a single endpoint, the legacy environment variables remain supported:
- `AI_API_KEY`
- `AI_PROVIDER_PROTOCOL` (defaults to `openai_compatible`)
- `AI_API_BASE_URL`
- `AI_MODEL`
- `AI_FALLBACK_MODEL`
- `AI_API_VERSION`
- `AI_MAX_OUTPUT_TOKENS`

The router tries endpoints in priority order, applies bounded retries, observes provider 429 responses with a cooldown, and moves to the next configured endpoint when an endpoint is unavailable or has exhausted its local budget. Each underlying request, retry, and fallback attempt counts against the local provider cap. Local counters are process-local safeguards, not authoritative views of provider quota. Provider-specific limits, billing, terms, and free-tier rules still apply. The router is not designed to bypass provider quotas.

Gemini API keys must be kept server-side and secured/restricted according to Google's current key guidance. New AI Studio keys and standard-key restrictions have changed during 2026; do not rely on an unrestricted legacy key.

Native API references:
- Gemini: https://ai.google.dev/api/generate-content
- Gemini API key guidance: https://ai.google.dev/gemini-api/docs/api-key
- Anthropic Messages: https://platform.claude.com/docs/en/api/messages/create

## Live web research

Research-category tasks run a small, bounded Tavily Search batch after the human approves the plan. The current preview limits each task to at most four basic-search queries, five results per query, and two concurrent search requests. Search results are deduplicated by URL and stored with source IDs (`[S1]`, `[S2]`), title, URL, query, short snippet, relevance score, and provider-reported credit count.

Set `TAVILY_API_KEY` in the backend environment to enable live web search. Optional settings:
- `TAVILY_SEARCH_URL` — defaults to `https://api.tavily.com/search`.
- `TAVILY_MAX_QUERIES_PER_TASK` — hard-capped at four by this preview.
- `TAVILY_MAX_RESULTS_PER_QUERY` — hard-capped at five.
- `TAVILY_TIMEOUT_SECONDS` — request timeout.

The integration requests basic search results and does not download raw page content. Search snippets are evidence leads, not full-page reading or independent fact verification. Agent reports must cite collected source IDs; QA rejects missing or unrecognised source IDs when sources exist. If the key is missing or the search returns no sources, the final report must disclose that live web research was not performed.

## Deliverable export

The Workforce HQ can download the final Markdown draft. The Print / Save PDF action opens the browser print dialog and lets the user save the deliverable as a PDF. This is client-side export; it does not create a server-side PDF file.

## Agent execution

Without configured providers, the planner returns a clearly labelled local demo plan. Demo plans cannot be executed as live AI work. After a provider-backed plan is approved, specialist workstreams can run concurrently, while calls to any one provider endpoint are serialised and paced; separate configured providers can be used for failover. Outputs are stored in shared task memory, the team director compares contributions, a draft is compiled, and an independent QA role reviews it. QA can request one bounded revision round.

### Provider pacing, failover, and retry

Each configured provider has its own async request lock, so only one request is in flight per endpoint. The default local safety budget is **4 requests per rolling minute** with a **15-second minimum interval** between requests to that endpoint. These conservative defaults are intended for low-quota API keys; they can be adjusted with `AI_PROVIDER_MAX_REQUESTS_PER_MINUTE` and `AI_PROVIDER_MIN_INTERVAL_SECONDS`, or overridden per provider in `AI_PROVIDERS_JSON` with `max_requests_per_minute` and `min_request_interval_seconds`. The existing `max_requests_per_hour` cap continues to apply.

HTTP 429 responses open a provider cooldown. The router honours `Retry-After` and provider retry metadata where supplied, uses a configurable fallback cooldown (`AI_PROVIDER_429_COOLDOWN_SECONDS`, default 60 seconds), and applies a longer configurable cooldown for explicit daily-quota exhaustion (`AI_PROVIDER_DAILY_QUOTA_COOLDOWN_SECONDS`, default one hour). Requests waiting behind the same provider lock see the open cooldown and do not send another request. The router continues to the next configured provider where available. Multiple independent providers must be configured in `AI_PROVIDERS_JSON` for cross-provider failover; a fallback model on the same API key does not create independent quota capacity.

When all providers are unavailable, automatic reassignment calls are deferred rather than sent to the same exhausted endpoint. Successful specialist outputs remain durable. A failed approved task can be retried through `POST /api/tasks/{task_id}/retry`; completed specialist checkpoints are rehydrated from shared memory and only failed or unfinished specialists are requested again. The retry endpoint refuses to start while all configured providers are cooling down or budget-limited.

Agents do not yet have browser, filesystem, terminal, or external-action tools. They must not claim to have run code, browsed websites, contacted anyone, or verified external facts. AI QA is not a substitute for deterministic tests or human review. Model-generated content and shared memory are treated as untrusted data, not authority to change system safeguards.

### Workstream and subtask status semantics

Subtasks are not completed by their position in the plan. A specialist output is attached only when the subtask owner exactly and uniquely matches that specialist's name, role, or ID; ambiguous or unmatched owner mappings remain open and emit a `subtask_mapping_deferred` event. Each uniquely matched specialist workstream is reconciled independently: successful output becomes `completed`, while an exhausted worker/recovery failure becomes `failed` with a bounded diagnostic. One failed specialist no longer leaves unrelated successful workstreams stuck at `in_progress`. QA-related subtasks are completed only after the independent reviewer returns an approval; a failed final QA verdict leaves them at `revision_required`. A QA transport/provider failure is `qa_status=failed`, not `rejected`; `rejected` means an actual QA decision. When execution stops before QA starts, the QA status remains `pending_execution`. Explicit sign-off tasks (for example, a formal, stakeholder, or go/no-go sign-off) are never completed by an AI agent: after QA passes they become `awaiting_human_signoff`, and if QA rejects they become `blocked_by_qa`. Task-level `completed` means the AI deliverable passed QA; it does not imply that any pending human sign-off has happened.

The Workforce HQ includes a Mission Output window that surfaces available specialist findings before a final integrated deliverable exists. These partial reports are not represented as QA-approved final output. Run Diagnostics displays the saved failure events. Future provider errors retain a short, credential-redacted explanation; legacy tasks that saved only an exception class (for example, `RuntimeError`) cannot be retrospectively attributed to a specific provider failure or HTTP status. The frontend also recognises older task snapshots where QA was incorrectly stored as `rejected` despite no `qa_started` event or verdict, and displays that state as **Not run** without rewriting the historical record.
The Workforce HQ also displays completed, failed, working, and waiting specialist counts separately. When a mission stops before QA, the diagnostics panel can retry unfinished workstreams after provider capacity returns; completed specialist outputs are preserved and are not regenerated.

## Supabase Postgres task storage (V3)

The recommended hosted database is **Supabase Postgres**. Set the connection URI in the backend-only `DATABASE_URL` environment variable. Do not put the URI in the frontend or commit it to GitHub.

**Render connection mode:** Render services use IPv4 networking. In Supabase Dashboard choose **Connect → Transaction pooler** and copy its PostgreSQL URI (port `6543`). The adapter disables both SQLAlchemy's prepared-statement cache and asyncpg's driver cache for this mode. Session pooler (port `5432`) is also supported. Supabase SSL is required automatically for Supabase pooler hosts.

On startup, SQLAlchemy creates the `workforce_private.workforce_tasks` table. The app persists the entire task snapshot: agents, workstreams, events, messages, shared memory, research sources, QA state, and deliverables. The schema is deliberately separate from the exposed `public` API schema, and the adapter enables row-level security and revokes access from `PUBLIC`, `anon`, and `authenticated`. Only the backend's private database connection should access these records.

For local development, SQLite remains supported with `DATABASE_URL=sqlite:///./workforce.db`. If no URL is configured, the app falls back to process memory and task history is cleared on restart.

When Redis queue mode is not configured, the runner uses FastAPI background tasks; an API restart restores the task snapshot but marks in-flight work failed with an `execution_interrupted` event. When both Supabase `DATABASE_URL` and Redis `REDIS_URL` are configured, approved jobs are requeued through Redis Streams and reclaimed after the idle threshold. Existing in-memory V2 tasks are not backfilled into Supabase.

Set `DATABASE_URL` in Render Dashboard → backend service → Environment. Never paste the database password into chat, GitHub, or browser code.

## Durable Redis Streams worker queue (V3 slice)

To enable durable approved-task delivery, configure both `DATABASE_URL` (Supabase Postgres) and `REDIS_URL` (Upstash Redis TLS URL) on the backend. The queue is deliberately disabled unless both values are configured; Redis alone is rejected because a queued message without a durable task snapshot cannot survive an API restart.

Approved plans are appended to the `workforce:approved-tasks` stream and consumed by a Redis consumer group. Pending entries can be reclaimed after the configured idle interval (`WORKFORCE_QUEUE_CLAIM_IDLE_MS`, default 15 minutes); the idle-reclaim scan interval is controlled by `WORKFORCE_QUEUE_CLAIM_SCAN_SECONDS` (default 60 seconds). The worker acknowledges a message only after the task reaches a terminal state. Repeated delivery failures are capped and moved to the `workforce:dead-tasks` dead-letter stream.

The current preview runs one queue consumer inside the FastAPI service. The task SSE endpoint reads the same API process task state and emits event/status changes; it is not yet a cross-process Redis Pub/Sub fanout. This demonstrates durable queueing and process-restart reclamation without requiring a separate paid worker service. The default 15-minute idle threshold avoids reclaiming ordinary long-running jobs too early; jobs lasting beyond the threshold still need a heartbeat extension before horizontal scaling. Do not scale the API horizontally yet: multi-instance execution needs distributed job leases/heartbeats, worker concurrency coordination, and duplicate-execution protection. The existing provider router remains separately rate-limited; a Redis-backed shared quota counter is not yet implemented.

Upstash Redis Free currently lists 500,000 commands/month, 256 MB data, and 10 GB monthly bandwidth. The worker uses a 30-second blocking XREADGROUP call while idle and scans for stale jobs once per minute by default, roughly 130,000 idle commands/month before task traffic. XADD/XPENDING/XAUTOCLAIM/XACK/XDEL operations add to that usage; monitor actual usage in the Upstash console. https://upstash.com/pricing/redis

## Preview limitations
- With no `DATABASE_URL`, tasks and shared memory are process-local and clear after a service restart.
- Even with durable snapshots, background work runs in the API process, not a durable queue; interrupted work is marked failed, not resumed.
- Up to eight specialists execute concurrently; four provider calls run concurrently per process.
- Local task and provider budgets are process-local. Production needs durable identity, global quotas, and abuse controls.
- The provider adapter currently supports text prompt/response translation for the three protocols above; native multimodal uploads, tools, and provider-native function calling are future work.

## Render
The service is deployed from the repository root:
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port $PORT`
- Health path: `/health`

Set `FRONTEND_ORIGINS` to the deployed frontend origin. Add provider secrets through Render Environment settings, never GitHub.
