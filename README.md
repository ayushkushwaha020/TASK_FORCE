# Autonomous AI Workforce

**One user. One instruction. An entire AI-powered workforce.**

A multi-agent AI workforce platform that turns a user request into a dynamically assembled team, coordinated task plan, independent QA review, and professionally delivered result.

## Confirmed product decisions
- Dual interface: AI Command Center (chat) + AI Workforce HQ (visual workspace).
- Approval-based autonomy: internal permitted work can proceed; sensitive, external, destructive, or irreversible actions require explicit approval.
- Dynamic teams sized to task complexity (typically 3–20 agents).
- Shared task memory and structured agent communication.
- Mandatory independent QA with authority to reject and request revisions.
- Bounded autonomous recovery and escalation.
- Animated robot identities represent real agents; demo activity is always labelled.
- Multiple AI providers through server-side adapters. API quotas and context limits are respected; usage is never described as unlimited.
- Login-free initial experience, with optional persistence later.

## Current milestone
**V1 Foundation** — responsive dual-interface shell, task intake, dynamic role planning, task state, and explicit demo-vs-provider status. The first milestone does not claim that agents have executed external actions, run code, or verified deliverables unless the backend reports evidence.

## Repository layout
- `frontend/` — React, TypeScript, Vite.
- `backend/` — FastAPI orchestration API.
- `docs/` — architecture, roadmap, acceptance checklist.

## Local development
Frontend:
```bash
cd frontend
npm install
npm run dev
```

Backend:
```bash
cd backend
python -m venv .venv
# Activate the virtual environment, then:
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Set `VITE_API_BASE_URL=http://localhost:8000` in `frontend/.env.local` if needed.

## AI provider configuration
Copy `backend/.env.example` to `backend/.env` and configure a compatible provider. Keep credentials server-side only. Never commit real keys.

## Hosting direction
- Frontend: Vercel, connected to this GitHub repository with root directory `frontend`.
- Backend: Railway, deployed from `backend` with the FastAPI start command documented in `backend/README.md`.
- Configure CORS and secrets in hosting environment settings.
- No deployment is considered successful until its health endpoint and deployed UI are checked.

See `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, and `docs/ACCEPTANCE_CHECKLIST.md`.
