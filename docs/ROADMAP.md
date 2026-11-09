# Development Roadmap

## V1 — Foundation (delivered)
- [x] React + TypeScript dual-interface shell
- [x] FastAPI planning API
- [x] Dynamic specialist planning with mandatory QA role
- [x] Server-side OpenAI-compatible planning adapter
- [x] Demo vs provider-backed plan status
- [x] Initial hosting on Render

## V2 — Agent execution preview (current)
- [x] Human approval before internal AI execution
- [x] Parallel model-backed specialist workstreams
- [x] Shared task memory
- [x] Structured agent-to-director messages
- [x] Team deliberation and contradiction tracking
- [x] Integrated deliverable drafting
- [x] Independent QA can reject and request revisions
- [x] One bounded revision cycle
- [x] Bounded retry, optional fallback model, and one workstream reassignment
- [x] Frontend polling, activity timeline, shared-memory feed, and QA view
- [ ] Durable task persistence and background worker queue
- [x] Capped Tavily search for research tasks with URL/snippet source capture\n- [x] Source-ID citations and independent QA integrity check\n- [x] Markdown download and browser print-to-PDF export\n- [ ] General browser navigation/page extraction
- [ ] Sandboxed code execution and deterministic test evidence
- [ ] Human approval queue for external/destructive actions
- [ ] Multi-provider quota-aware router

## V3 — Tools and production controls
- [ ] Permission-scoped browser/search tools
- [ ] Isolated code runner
- [ ] File ingestion and document generation
- [ ] Approval queue and audit trail
- [ ] Postgres persistence and Redis job/event queue
- [ ] User identities, quotas, and abuse protection
- [ ] Cost and token budgets per task/team
- [ ] Multi-provider adapter registry

## Release rule
A milestone is complete only when its acceptance criteria are implemented and tested. A visual indicator is not evidence that an agent executed a tool or verified a deliverable.
