# Architecture — V2 Agent Execution Preview

## Runtime path

```text
React Command Center / Workforce HQ
        | plan / approve / poll
        v
FastAPI API
  |-- task planner and dynamic team composer
  |-- human approval gate
  |-- process-local task state store
  |-- execution scheduler (2 task runs; max 4 provider calls in parallel)
  |-- specialist agents (up to 8 active)
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

## Implemented V2 behaviour
- Provider-backed plan generation.
- Explicit approval endpoint.
- Bounded primary-model retry and optional `AI_FALLBACK_MODEL`.
- Up to eight active specialists with a four-call provider concurrency limit.
- Shared task memory and agent-to-director messages.
- Team deliberation with contradictions and open questions.
- Independent QA verdict and one revision round.
- Recovery by reassigning a failed specialist workstream once to a successful team member.
- Frontend polling for live execution status and event feed.

## Current preview limits
- Task state and memory live in process memory. Restarts clear them; multiple API instances are not supported.
- Background execution runs within the API service, not a durable queue.
- No agent has browser, filesystem, terminal, shell, or external-service tools.
- Provider output is model-generated text. The system must not claim code execution, website research, external verification, or completion of real-world actions.
- AI QA is a separate model review, not deterministic proof of correctness.
- The API has a lightweight in-memory rate guard; production still needs durable user identity, quotas, audit, and anti-abuse protections.

## Production target
- Frontend: Render static site preview; Vercel remains an alternative.
- API and background workers: Render for the current hackathon preview.
- Durable task store: PostgreSQL.
- Queue and event fanout: Redis or a durable job system.
- File storage: private object storage.
- Tool execution: isolated, permission-scoped sandboxes.
- Multiple provider adapters with quota, cost, timeout, and fallback policies.

## Trust boundaries
- Browser never receives provider secrets.
- Uploaded files and external content will be treated as untrusted input.
- Sensitive, external, destructive, or irreversible operations require a separate explicit human approval.
- QA status is evidence-based; a successful text review is not the same as code/test execution.
