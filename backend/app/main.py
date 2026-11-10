import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from collections import defaultdict, deque

from dotenv import load_dotenv

load_dotenv()

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .execution import execute_task
from .orchestrator import create_plan
from .providers import provider_availability, provider_is_configured, provider_status, test_provider_connections
from .schemas import ApprovalRequest, PlanRequest, PlanResponse
from .state import TASKS
from .task_events import RedisTaskEventBus, TaskEventHub
from .worker_queue import TASK_QUEUE
from .web_research import web_research_is_configured

SSE_RECONCILE_SECONDS = 15
TASK_EVENT_HUB = TaskEventHub()
TASK_EVENT_BUS = (
    RedisTaskEventBus(TASK_QUEUE.redis, TASK_EVENT_HUB)
    if TASK_QUEUE is not None
    else None
)
TASKS.set_event_notifier(
    TASK_EVENT_BUS.notify if TASK_EVENT_BUS is not None else TASK_EVENT_HUB.notify
)


def task_event_status() -> dict:
    if TASK_EVENT_BUS is not None:
        return TASK_EVENT_BUS.status()
    return {"transport": "process_local", "subscriber_connected": False}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A durable queue is enabled only when both DATABASE_URL and REDIS_URL
    # are configured. TaskStore validates that dependency before the app serves.
    if TASK_QUEUE is not None:
        await TASK_QUEUE.initialize()
    await TASKS.initialize(
        active_lease_checker=TASK_QUEUE.has_active_lease if TASK_QUEUE is not None else None
    )
    subscriber_task = None
    if TASK_EVENT_BUS is not None:
        # Pub/Sub is an optimization only. Its subscriber reconnects in the
        # background and must never be a prerequisite for serving the API.
        subscriber_task = asyncio.create_task(
            TASK_EVENT_BUS.run(), name="workforce-task-event-subscriber"
        )
    worker_task = None
    if TASK_QUEUE is not None:
        worker_task = asyncio.create_task(TASK_QUEUE.run(), name="workforce-redis-stream-worker")
    try:
        yield
    finally:
        if worker_task is not None:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
        if subscriber_task is not None:
            subscriber_task.cancel()
            try:
                await subscriber_task
            except asyncio.CancelledError:
                pass
        if TASK_EVENT_BUS is not None:
            await TASK_EVENT_BUS.close()
        if TASK_QUEUE is not None:
            await TASK_QUEUE.close()
        await TASKS.close()


app = FastAPI(
    title="Autonomous AI Workforce API",
    version="0.4.0",
    description="Approval-gated multi-agent planning and execution with durable task history and optional Redis Streams queue.",
    lifespan=lifespan,
)

origins = [
    origin.strip()
    for origin in os.getenv("FRONTEND_ORIGINS", "http://localhost:5173").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)

_rate_windows: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_RATE_WINDOW_SECONDS = 3600


def _format_retry_wait(seconds: int) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def enforce_rate_limit(request: Request, action: str, limit: int) -> None:
    # This is a lightweight preview guard, not a substitute for production auth/quotas.
    source = request.client.host if request.client else "unknown-client"
    now = time.monotonic()
    bucket = _rate_windows[(source, action)]
    while bucket and now - bucket[0] > _RATE_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail=f"Preview limit reached for {action}. Please try again later.")
    bucket.append(now)


@app.get("/")
async def root():
    return {
        "service": "Autonomous AI Workforce API",
        "status": "ok",
        "health": "/health",
        "docs": "/docs",
        "version": "0.4.0",
        "storage_backend": TASKS.storage_backend,
        "durable_storage_configured": TASKS.durable_storage_configured,
        "task_queue_backend": "redis_streams" if TASK_QUEUE is not None else "background_tasks_preview",
        "durable_queue_configured": TASK_QUEUE is not None,
        "task_event_transport": task_event_status(),
    }


@app.get("/health")
async def health():
    return {
        "status": "ok", "service": "autonomous-ai-workforce-api", "version": "0.4.0",
        "storage_backend": TASKS.storage_backend,
        "durable_storage_configured": TASKS.durable_storage_configured,
        "task_queue_backend": "redis_streams" if TASK_QUEUE is not None else "background_tasks_preview",
        "durable_queue_configured": TASK_QUEUE is not None,
        "task_event_transport": task_event_status(),
    }


@app.get("/api/config/status")
async def config_status():
    status = provider_status()
    status["web_research_configured"] = web_research_is_configured()
    status["web_research_provider"] = "tavily" if web_research_is_configured() else None
    status["storage_backend"] = TASKS.storage_backend
    status["durable_storage_configured"] = TASKS.durable_storage_configured
    status["task_queue_backend"] = "redis_streams" if TASK_QUEUE is not None else "background_tasks_preview"
    status["durable_queue_configured"] = TASK_QUEUE is not None
    if TASK_QUEUE is not None:
        try:
            status["task_queue"] = await TASK_QUEUE.status()
        except Exception as exc:
            status["task_queue"] = {"backend": "redis_streams", "configured": True, "status": "unavailable", "error": type(exc).__name__}
    else:
        status["task_queue"] = {"backend": "background_tasks_preview", "configured": False}
    status["task_event_transport"] = task_event_status()
    return status


@app.post("/api/providers/test")
async def test_providers(http_request: Request):
    enforce_rate_limit(http_request, "provider connection tests", 5)
    try:
        return await test_provider_connections()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

@app.post("/api/tasks/plan", response_model=PlanResponse)
async def plan_task(request: PlanRequest, http_request: Request):
    enforce_rate_limit(http_request, "task planning", 30)
    plan = await create_plan(request)
    return await TASKS.create(plan)


@app.get("/api/tasks/{task_id}", response_model=PlanResponse)
async def get_task(task_id: str):
    task = await TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found. Check the task ID; tasks created before durable storage was configured are not backfilled.")
    return task


async def _task_event_generator(task_id: str, http_request: Request):
    """Wait for local/Redis hints, reread canonical state, and reconcile every 15s."""
    listener = TASK_EVENT_HUB.subscribe(task_id)
    try:
        # Subscribe before reading to close the read/subscribe race.
        task = await TASKS.get(task_id)
        if task is None:
            yield "event: task_error\ndata: {\"detail\":\"Task not found.\"}\n\n"
            return

        last_event_id = http_request.headers.get("last-event-id") or http_request.query_params.get("after") or ""
        last_index = len(task.events)
        if last_event_id:
            positions = [index for index, item in enumerate(task.events) if item.id == last_event_id]
            last_index = positions[-1] + 1 if positions else max(0, len(task.events) - 10)
        last_updated = task.updated_at
        last_heartbeat = time.monotonic()
        initial_hint = None
        if not listener.empty():
            initial_hint = listener.get_nowait()
            # A write may have committed between subscription and this first
            # snapshot. Use its event cursor to include the just-committed event.
            if not last_event_id and initial_hint.event_cursor:
                positions = [
                    index for index, item in enumerate(task.events)
                    if item.id == initial_hint.event_cursor
                ]
                if positions:
                    last_index = min(last_index, positions[-1])
        force_update = initial_hint is not None
        first_pass = True

        while True:
            if await http_request.is_disconnected():
                break
            if not first_pass:
                # Every wake-up (or timeout) rereads the canonical task snapshot.
                task = await TASKS.get(task_id)
                if task is None:
                    yield "event: task_error\ndata: {\"detail\":\"Task no longer exists.\"}\n\n"
                    break

            new_events = task.events[last_index:]
            for item in new_events:
                payload = item.model_dump(mode="json")
                yield f"id: {item.id}\nevent: task_event\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            last_index = len(task.events)

            changed = force_update or bool(new_events) or task.updated_at != last_updated
            if changed:
                update_payload = {
                    "task_id": task.task_id,
                    "updated_at": task.updated_at,
                    "execution_status": task.execution_status,
                    "approval_status": task.approval_status,
                    "qa_status": task.qa_status,
                    "event_count": len(task.events),
                }
                yield f"event: task_update\ndata: {json.dumps(update_payload, ensure_ascii=False)}\n\n"
                last_updated = task.updated_at
            force_update = False

            if task.execution_status in {"completed", "revision_required", "failed", "rejected", "cancelled"}:
                break

            try:
                await asyncio.wait_for(listener.get(), timeout=SSE_RECONCILE_SECONDS)
            except asyncio.TimeoutError:
                # Periodic canonical reread recovers a lost Pub/Sub notification.
                pass
            if time.monotonic() - last_heartbeat >= 15:
                yield ": workforce stream alive\n\n"
                last_heartbeat = time.monotonic()
            first_pass = False
    finally:
        TASK_EVENT_HUB.unsubscribe(task_id, listener)


@app.get("/api/tasks/{task_id}/stream")
async def stream_task_events(task_id: str, http_request: Request):
    task = await TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return StreamingResponse(
        _task_event_generator(task_id, http_request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/tasks/{task_id}/retry", response_model=PlanResponse)
async def retry_task(task_id: str, background_tasks: BackgroundTasks, http_request: Request):
    """Resume a failed approved task while preserving completed specialist checkpoints."""
    enforce_rate_limit(http_request, "failed task retries", 8)
    task = await TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task.execution_status != "failed" or task.approval_status != "approved":
        raise HTTPException(status_code=409, detail="Only a previously approved task that stopped on execution failure can be retried.")
    if task.qa_status in {"passed", "rejected"}:
        raise HTTPException(status_code=409, detail="This task already has a final QA decision. Create a new mission or use the QA revision workflow.")

    availability = provider_availability()
    if not availability.get("configured"):
        raise HTTPException(status_code=503, detail="No live AI provider is configured. Add a provider in the backend environment before retrying.")
    if availability.get("available_provider_count", 0) <= 0:
        retry_after = availability.get("retry_after_seconds")
        wait_note = f" Try again in about {_format_retry_wait(retry_after)} ({retry_after:,} seconds)." if retry_after else " Wait until the provider request budget resets."
        fallback_note = " Configure a second provider in AI_PROVIDERS_JSON for automatic failover." if not availability.get("fallback_configured") else ""
        blocked_label = availability.get("blocked_label")
        quota_note = f" Provider status: {blocked_label}." if blocked_label else ""
        metric_note = f" Quota metric: {availability['quota_metric']}." if availability.get("quota_metric") else ""
        raise HTTPException(status_code=503, detail="All configured providers are currently unavailable." + quota_note + metric_note + wait_note + fallback_note)

    for agent in task.agents:
        if "qa" in agent.role.casefold() or "verif" in agent.role.casefold():
            await TASKS.update_agent(task_id, agent.id, status="waiting_for_review")
        elif agent.status in {"failed", "blocked_by_provider"}:
            await TASKS.update_agent(task_id, agent.id, status="planned")
    for subtask in task.subtasks:
        if subtask.status in {"failed", "blocked_by_provider", "in_progress"}:
            await TASKS.update_subtask(task_id, subtask.id, status="planned", output=None)

    await TASKS.update(task_id, execution_status="queued", qa_status="pending_execution", qa_feedback=None)
    await TASKS.add_event(
        task_id, "Human", "execution_retry_requested",
        "The user requested a retry. Completed specialist outputs remain saved; only unfinished or failed workstreams will be called again.",
    )
    if TASK_QUEUE is not None:
        try:
            message_id = await TASK_QUEUE.enqueue(task_id)
            await TASKS.add_event(task_id, "Workforce Queue", "queue_job_enqueued", f"Retry was committed to the durable Redis queue (message {message_id}).")
        except Exception as exc:
            await TASKS.update(task_id, execution_status="failed", qa_status="pending_execution")
            await TASKS.add_event(task_id, "Workforce Queue", "queue_enqueue_failed", f"Retry queue submission failed ({type(exc).__name__}); saved specialist outputs remain intact.")
            raise HTTPException(status_code=503, detail="The durable task queue is unavailable. Completed specialist outputs have been preserved; try again after the queue recovers.") from exc
    else:
        background_tasks.add_task(execute_task, task_id)
    result = await TASKS.get(task_id)
    return result or task


@app.post("/api/tasks/{task_id}/approval", response_model=PlanResponse)
async def decide_task_approval(task_id: str, decision: ApprovalRequest, background_tasks: BackgroundTasks, http_request: Request):
    task = await TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task.execution_status != "awaiting_approval" or task.approval_status != "pending":
        raise HTTPException(status_code=409, detail="This task is no longer waiting for an approval decision.")
    if decision.decision == "reject":
        await TASKS.update(task_id, execution_status="rejected", approval_status="rejected")
        await TASKS.add_event(task_id, "Human", "plan_rejected", decision.reason or "The user rejected the proposed plan.")
        return await TASKS.get(task_id)
    enforce_rate_limit(http_request, "approved executions", 8)
    if not provider_is_configured() or task.execution_mode != "provider_planned":
        raise HTTPException(
            status_code=409,
            detail="Live execution is unavailable. Configure AI_API_KEY and AI_MODEL in the backend hosting environment, then create a fresh plan. Demo plans are never executed as if they were real agents.",
        )
    updated = await TASKS.update(task_id, execution_status="queued", approval_status="approved")
    await TASKS.add_event(task_id, "Human", "plan_approved", "The user approved the team plan and authorised internal AI execution.")
    if TASK_QUEUE is not None:
        try:
            message_id = await TASK_QUEUE.enqueue(task_id)
            await TASKS.add_event(task_id, "Workforce Queue", "queue_job_enqueued", f"Approved task was committed to the durable Redis queue (message {message_id}).")
        except Exception as exc:
            await TASKS.update(task_id, execution_status="failed", qa_status="pending_execution")
            await TASKS.add_event(task_id, "Workforce Queue", "queue_enqueue_failed", f"Durable queue submission failed ({type(exc).__name__}); no execution was started.")
            raise HTTPException(status_code=503, detail="The durable task queue is unavailable. The task was not started; check the queue configuration and retry with a new plan.") from exc
    else:
        background_tasks.add_task(execute_task, task_id)
    result = await TASKS.get(task_id)
    return result or updated

