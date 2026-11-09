# Acceptance Checklist

Statuses: NOT STARTED / IN PROGRESS / IMPLEMENTED / TESTED / VERIFIED / BLOCKED.

## V1 — Planning foundation
- [x] Public GitHub repository selected: TASK_FORCE
- [x] Dedicated feature branch; main preserved
- [x] React + TypeScript dual-interface shell
- [x] FastAPI API and provider configuration status
- [x] Dynamic specialist team planning with mandatory QA role
- [x] Demo vs provider-backed planning clearly labelled
- [x] Provider credentials kept server-side

## V2 — Agent execution preview
- [x] Human approval/rejection before internal execution
- [x] Provider-backed specialist workstream execution
- [x] Parallel specialist calls with concurrency cap
- [x] Shared task memory and structured agent messages
- [x] Team director deliberation across specialist findings
- [x] Integrated draft deliverable
- [x] Independent QA verdict can approve or reject
- [x] QA-triggered revision pass
- [x] Bounded model retry, optional fallback model, and workstream reassignment
- [x] Frontend polling, event timeline, shared-memory feed, and QA report
- [x] Backend execution tests for approval path, QA revision, and demo-plan safety
- [ ] Provider-backed live end-to-end task verified with a configured key
- [ ] Manual responsive review after V2 UI update
- [ ] Durable task persistence (preview tasks are in process memory)
- [ ] Browser/search or code-runner tool integration

## V3 — Tools and action governance
- [ ] Permission-scoped browser/search tools with source capture
- [ ] Isolated code runner and deterministic test evidence
- [ ] File ingestion and PDF/document generation
- [ ] Human approval queue for external/destructive actions
- [ ] Audit trail and policy engine
- [ ] Persistent shared memory across tasks

## V4 — Production readiness
- [ ] PostgreSQL persistence
- [ ] Durable background worker queue and event streaming
- [ ] Redis coordination and quotas
- [ ] Multi-provider adapter registry and routing
- [ ] Usage budgets, rate limits, and abuse protection
- [ ] Optional user accounts and workspace isolation
- [ ] Security, load, and end-to-end testing
- [ ] Production deployment verification

## Release rule
A feature is not marked tested or verified solely because it appears in the UI. Record the actual test, tool, or evidence used.
