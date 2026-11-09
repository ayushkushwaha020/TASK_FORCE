# Architecture — V1 Foundation

## System shape

```text
Browser (React / Vite)
  | HTTPS JSON
  v
FastAPI API
  |-- Task Planner / Classifier
  |-- Dynamic Team Composer
  |-- Provider Adapter (server-side credentials)
  |-- QA role injection
  |-- Explicit demo/provider-planned status
  v
Task Plan Response
  |-- Agents
  |-- Workstreams
  |-- QA gate status
  |-- Execution-mode notice
```

## Confirmed product decisions
- Dual interface: chat command center and visual workforce workspace.
- Approval-based autonomy.
- Dynamic teams sized to complexity.
- Shared memory and agent communication are required next-stage modules.
- Independent QA must be able to reject work and request revisions.
- Recovery is bounded and escalates unresolved failures.
- Robot activity must reflect backend state; no fake activity.
- Secrets remain server-side.

## Current implementation boundary
V1 Foundation implements a task-planning vertical slice. Without provider credentials, planning is deterministic and marked `demo`. With a configured OpenAI-compatible provider, the backend can request a structured plan and marks it `provider_planned`. Neither mode claims that subtasks were executed or deliverables verified.

## Planned production architecture
- Frontend: Vercel.
- API and background workers: Railway.
- Persistent task state: PostgreSQL.
- Queue / transient coordination: Redis.
- Object storage: private bucket for uploaded and generated files.
- Provider adapters: one adapter per supported API family.
- Tool execution: isolated, short-lived sandboxes with explicit permissions.

## Trust boundaries
- Browser never receives provider secrets.
- Uploaded files and web content are untrusted input.
- Tool permissions are scoped per task and agent.
- Sensitive or irreversible operations pause for explicit approval.
- QA status is evidence-based; planning is not verification.
