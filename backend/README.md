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
- `GET /api/config/status` — provider names, models, local budget usage, and status; never returns secrets.
- `POST /api/tasks/plan` — create a proposed team plan. JSON: `{"instruction":"..." }`.
- `GET /api/tasks/{task_id}` — poll task state, events, shared memory, messages, QA, and deliverable.
- `POST /api/tasks/{task_id}/approval` — JSON `{"decision":"approve"}` or `{"decision":"reject","reason":"..." }`.

## Multi-provider configuration
The router accepts multiple **OpenAI-compatible Chat Completions endpoints** through server-side `AI_PROVIDERS_JSON`. Each entry needs:
- `name`: display name, not a secret.
- `base_url`: provider API base URL.
- `api_key`: credential, kept server-side.
- `model`: model identifier.
- `priority`: lower values are tried first.
- `max_requests_per_hour`: optional local per-process request cap.
- `fallback_model`: optional model on the same endpoint.

Example structure (replace all example values with credentials and model identifiers issued by the provider):
```json
[
  {
    "name": "provider-one",
    "base_url": "https://api.provider-one.example/v1",
    "api_key": "YOUR_PROVIDER_KEY",
    "model": "YOUR_MODEL_ID",
    "priority": 10,
    "max_requests_per_hour": 30
  }
]
```

The router sorts configured endpoints by priority, tracks optional local request budgets, applies bounded retries, and moves to another configured endpoint when one is unavailable or responds with a rate-limit error. It does not rotate multiple credentials for the same provider to bypass a provider limit. Local counters are a preview safeguard, not an authoritative view of the provider's remaining quota. The provider's own limits and terms still apply.

For one endpoint, the legacy `AI_API_KEY`, `AI_API_BASE_URL`, and `AI_MODEL` variables remain supported. Optionally set `AI_FALLBACK_MODEL`.

## Agent execution
Without a provider, the planner returns a clearly labelled local demo plan. Demo plans cannot be executed as live AI work. After a provider-backed plan is approved, the service runs specialist model calls in parallel, stores outputs in shared task memory, asks the team director to compare contributions, compiles a draft, and submits it to an independent QA role. QA can request one bounded revision round.

Agents do not yet have browser, filesystem, terminal, or external-action tools. They must not claim to have run code, browsed websites, contacted anyone, or verified external facts. AI QA is not a substitute for deterministic tests or human review.

## Preview limitations
- Tasks and shared memory are process-local and clear after a service restart.
- Background work runs in the API process, not a durable queue.
- Up to eight specialists execute concurrently; four provider calls run concurrently.
- A small in-memory rate limit and local provider request caps are enabled. Production needs durable identity, quotas, and abuse controls.

## Render
The service is deployed from the repository root:
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port $PORT`
- Health path: `/health`

Set `FRONTEND_ORIGINS` to the deployed frontend origin. Add provider secrets through Render Environment settings, never GitHub.
