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
- `GET /api/config/status` — provider configuration status; never returns secrets.
- `POST /api/tasks/plan` — create a proposed team plan. JSON: `{"instruction":"..." }`.
- `GET /api/tasks/{task_id}` — poll a task's live state, events, shared memory, messages, QA, and deliverable.
- `POST /api/tasks/{task_id}/approval` — JSON `{"decision":"approve"}` or `{"decision":"reject","reason":"..." }`.

## Provider configuration
Set `AI_API_KEY`, `AI_MODEL`, and optionally `AI_API_BASE_URL` in the backend environment. Optionally set `AI_FALLBACK_MODEL` for one fallback model. This adapter uses an OpenAI-compatible Chat Completions endpoint. Credentials are read only by the backend.

Without a provider, the planner returns a clearly labelled local demo plan. Demo plans cannot be executed as live AI work. After a provider-backed plan is approved, the service runs specialist model calls in parallel, stores each completed output in shared task memory, asks the team director to compare contributions, compiles a draft, and submits it to an independent QA role. QA can request one bounded revision round and can leave the task in `revision_required` rather than falsely declaring success.

The current agent runtime has no browser, file-system, terminal, or external-action tools. It cannot claim that code was run, websites were browsed, messages were sent, or facts were externally verified. QA is an AI content review, not a substitute for deterministic tests or human review.

## Preview limitations
- Tasks and shared memory are process-local in this preview. A service restart clears them.
- Background work runs in the API process; durable workers/queues are a later milestone.
- Up to eight specialists execute concurrently; additional assembled team members remain on standby to bound API usage.
- A small in-memory rate limit is enabled. Production needs durable quotas, identity, and abuse controls.

## Render
The service is deployed from the repository root:
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port $PORT`
- Health path: `/health`

Set `FRONTEND_ORIGINS` to the deployed frontend origin. Add provider secrets through Render Environment settings, never GitHub.
