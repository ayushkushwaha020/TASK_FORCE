# Acceptance Checklist

Statuses: NOT STARTED / IN PROGRESS / IMPLEMENTED / TESTED / VERIFIED / BLOCKED.

## V1 Foundation
- [x] Public GitHub repository selected: TASK_FORCE
- [x] Feature branch created; main preserved
- [x] React + TypeScript + Vite frontend scaffold
- [x] FastAPI backend scaffold
- [x] Chat Command Center view
- [x] Visual Workforce HQ view
- [x] Provider settings status view (no browser-side secrets)
- [x] Task intake and dynamic role planning endpoint
- [x] QA verifier automatically included in team plans
- [x] Demo and provider-planned modes distinguished
- [x] Planning response explicitly says execution/verification has not occurred
- [ ] Frontend production build passes
- [ ] Backend import and health checks pass
- [ ] End-to-end task planning passes
- [ ] Responsive UI manually reviewed
- [ ] Vercel preview deployed and checked
- [ ] Railway API deployed and health checked

## V2 — Real orchestration
- [ ] Agent registry and lifecycle
- [ ] Shared task memory
- [ ] Structured agent-to-agent messaging
- [ ] Task dependency graph
- [ ] Parallel background execution
- [ ] Live status streaming
- [ ] Bounded retries and reassignment

## V3 — QA, tools, approvals
- [ ] Independent QA can reject and request revisions
- [ ] Evidence-backed test execution
- [ ] Sandboxed code runner
- [ ] Controlled browser/search tools
- [ ] Approval dashboard
- [ ] Sensitive action policy enforcement
- [ ] Audit trail

## V4 — Production
- [ ] PostgreSQL persistence
- [ ] Redis queue
- [ ] Secure file storage
- [ ] Multi-provider adapters
- [ ] Quota and cost controls
- [ ] User sessions / optional accounts
- [ ] Security review
- [ ] Automated test suite
- [ ] Production deployment verification
