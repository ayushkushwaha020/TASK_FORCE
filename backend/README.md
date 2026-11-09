# Backend

FastAPI task-planning service for Autonomous AI Workforce.

## Run locally
```bash
python -m venv .venv
# Activate the environment
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

Health check: `GET /health`  
Provider status: `GET /api/config/status`  
Plan a task: `POST /api/tasks/plan` with JSON `{"instruction":"..." }`

## Provider configuration
Set `AI_API_KEY`, `AI_MODEL`, and optionally `AI_API_BASE_URL` in the server environment. The current adapter expects an OpenAI-compatible Chat Completions endpoint. Credentials are read only by the backend.

If no provider is configured, the API returns a clearly labelled local demo plan. A plan is not execution: this milestone does not run code, browse the web, send messages, or verify deliverables.

## Railway
Set the service root directory to `backend`. Use:
- Build/install: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health check path: `/health`

Set `FRONTEND_ORIGINS` to the deployed frontend origin and add provider secrets through Railway Variables.
