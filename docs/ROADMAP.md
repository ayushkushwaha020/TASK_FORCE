# Development Roadmap

## V1 — Foundation (delivered)
- [x] React + TypeScript dual-interface shell
- [x] FastAPI planning API
- [x] Dynamic specialist planning with mandatory QA role
- [x] Server-side OpenAI-compatible planning adapter
- [x] Demo vs provider-backed plan status
- [x] Initial hosting on Render

## V2 — Agent execution preview (delivered foundation)
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
- [ ] Durable background worker queue and event fanout (V3)
- [x] Capped Tavily search for research tasks with URL/snippet source capture
- [x] Source-ID citations and independent QA integrity check
- [x] Markdown download and browser print-to-PDF export\n- [ ] General browser navigation/page extraction
- [ ] Sandboxed code execution and deterministic test evidence
- [ ] Human approval queue for external/destructive actions
- [x] Multi-provider quota-aware router

## V3 — Persistence and production controls (in progress)
- [x] SQLAlchemy-backed Supabase Postgres adapter with SSL, pooler support, private schema, and row-level security
- [x] Unit tests for Supabase pooler URL normalization, SQLite URL normalization, and private schema selection
- [x] Versioned SQL migration prepared for Supabase; live application waits for project provisioning
- [x] Restore task history, shared memory, messages, research sources, events, and deliverables after API restart
- [x] Mark interrupted in-process jobs as failed with an explicit recovery event
- [ ] Provision Supabase Free project and configure the Render backend connection (blocked by account approval/login)
- [ ] Apply migration and verify real Supabase create/read/update/restart behavior
- [ ] Durable Redis worker/job queue and event fanout (next V3 slice)
- [ ] Permission-scoped browser/search tools
- [ ] Isolated code runner
- [ ] File ingestion and document generation
- [ ] Approval queue and audit trail
- [ ] Redis job/event queue and durable worker execution
- [ ] User identities, quotas, and abuse protection
- [ ] Cost and token budgets per task/team
- [ ] Multi-provider adapter registry

## Release rule
A milestone is complete only when its acceptance criteria are implemented and tested. A visual indicator is not evidence that an agent executed a tool or verified a deliverable.
