# Autonomous AI Workforce

**One user. One instruction. An entire AI-powered workforce.**

A multi-agent AI workforce platform that assembles specialists, coordinates their work, shares task memory, and submits an integrated deliverable to an independent QA reviewer.

## Confirmed product decisions
- Dual interface: AI Command Center (chat) + AI Workforce HQ (visual workspace).
- Approval-based autonomy: the user approves a plan before agents start internal execution.
- Dynamic teams, sized to task complexity (typically 3–20 agents).
- Shared task memory and structured agent communication.
- Mandatory independent QA with authority to reject work and request revisions.
- Bounded retry, fallback-model support, reassignment, and escalation.
- Robot activity reflects backend-reported task state.
- Server-side API credentials; provider quotas and limits are respected.
- Login-free preview; optional persistent accounts are a later milestone.

## Current milestone — V2 Agent Execution Preview + Provider Router
- Provider-backed planning and approval-gated specialist execution.
- Multi-endpoint routing for OpenAI-compatible Chat Completions, native Gemini generateContent, and Anthropic Messages APIs, ordered by priority.
- Optional local hourly request caps, cooldowns, bounded retries, per-endpoint fallback models, and failover.
- Parallel specialist outputs, shared memory, structured messages, and team deliberation.
- Integrated draft deliverable and independent QA review.
- One bounded QA revision round.
- Live status polling, activity timeline, shared-memory feed, and QA report in the frontend.

**Important boundaries:** agents currently produce and review model-generated text. They do not yet have browser, filesystem, terminal, or external-action tools. The platform must not claim that websites were browsed, code was executed, messages were sent, or external facts were verified. QA is an independent AI content review, not deterministic proof of correctness. Tasks currently live in backend process memory and can be cleared by a service restart.

## Repository layout
- `frontend/` — React, TypeScript, Vite.
- `backend/` — FastAPI planning and execution API.
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
# Activate the environment
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Set `VITE_API_BASE_URL=http://localhost:8000` in `frontend/.env.local` if needed.

## AI provider configuration
Copy `backend/.env.example` to `backend/.env`. Configure one endpoint with `AI_API_KEY`, `AI_MODEL`, and optionally `AI_API_BASE_URL` and `AI_FALLBACK_MODEL`, or several endpoints with server-side `AI_PROVIDERS_JSON` using `openai_compatible`, `gemini_generate_content`, or `anthropic_messages` protocols. Per-endpoint `priority`, optional `max_requests_per_hour`, and `fallback_model` are supported. Provider quotas still apply; local caps are a preview safeguard and do not represent remaining provider quota. Keep credentials server-side only. Never commit real keys. Add `TAVILY_API_KEY` in the backend environment for live research search; without it the report must disclose that no live web search was performed. Native Gemini/Anthropic protocol adapters are future work.

Without an API key, the app can create clearly labelled demo plans but cannot start live specialist execution.

## Hosting
The current preview is hosted on Render:
- Frontend: `https://autonomous-ai-workforce-web.onrender.com`
- Backend: `https://autonomous-ai-workforce-api.onrender.com`

See `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, and `docs/ACCEPTANCE_CHECKLIST.md`.
