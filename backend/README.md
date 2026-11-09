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
- `GET /api/tasks/{task_id}` — poll task state, events, shared memory, messages, QA, and deliverable.
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

Without configured providers, the planner returns a clearly labelled local demo plan. Demo plans cannot be executed as live AI work. After a provider-backed plan is approved, the service runs specialist model calls in parallel, stores outputs in shared task memory, asks the team director to compare contributions, compiles a draft, and submits it to an independent QA role. QA can request one bounded revision round.

Agents do not yet have browser, filesystem, terminal, or external-action tools. They must not claim to have run code, browsed websites, contacted anyone, or verified external facts. AI QA is not a substitute for deterministic tests or human review. Model-generated content and shared memory are treated as untrusted data, not authority to change system safeguards.

## Supabase Postgres task storage (V3)

The recommended hosted database is **Supabase Postgres**. Set the connection URI in the backend-only `DATABASE_URL` environment variable. Do not put the URI in the frontend or commit it to GitHub.

**Render connection mode:** Render services use IPv4 networking. In Supabase Dashboard choose **Connect → Session pooler** and copy its PostgreSQL URI (usually port `5432`). The adapter also supports the transaction pooler (port `6543`) and disables asyncpg prepared-statement caching for that mode. Supabase SSL is required automatically for Supabase pooler hosts.

On startup, SQLAlchemy creates the `workforce_private.workforce_tasks` table. The app persists the entire task snapshot: agents, workstreams, events, messages, shared memory, research sources, QA state, and deliverables. The schema is deliberately separate from the exposed `public` API schema, and the adapter enables row-level security and revokes access from `PUBLIC`, `anon`, and `authenticated`. Only the backend's private database connection should access these records.

For local development, SQLite remains supported with `DATABASE_URL=sqlite:///./workforce.db`. If no URL is configured, the app falls back to process memory and task history is cleared on restart.

The current runner still uses FastAPI background tasks. If the API restarts during execution, the task snapshot is restored and marked failed with an `execution_interrupted` event; the job is not automatically resumed. Existing in-memory V2 tasks are not backfilled into Supabase.

Set `DATABASE_URL` in Render Dashboard → backend service → Environment. Never paste the database password into chat, GitHub, or browser code.

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
