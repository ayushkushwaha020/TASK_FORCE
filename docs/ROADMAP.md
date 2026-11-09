# Development Roadmap

## V1 — Foundation (current branch)
- [x] Repository baseline and project documentation
- [x] React + TypeScript dual-interface shell
- [x] FastAPI health and task-planning endpoints
- [x] Dynamic role planning with mandatory QA role
- [x] Server-side OpenAI-compatible planning adapter
- [x] Explicit demo vs provider-planned labels
- [ ] Build and runtime verification
- [ ] First hosted preview

## V2 — Real orchestration
- Agent registry and lifecycle
- Task graph and dependency scheduler
- Structured agent messages
- Shared task memory
- Background execution and streaming status
- Bounded retries and recovery

## V3 — Tools and approvals
- Sandboxed code execution
- Controlled browser/search tools
- Human approval queue
- Audit log and permission policies
- Independent QA execution and evidence

## V4 — Production readiness
- PostgreSQL persistence
- Redis queue
- Authentication and optional accounts
- File storage and document generation
- Usage budgets and quota-aware provider routing
- Security, load, and end-to-end testing
- Production deployment

## Release rule
A milestone is complete only when its acceptance criteria are implemented and tested. Do not label planned work as shipped.
