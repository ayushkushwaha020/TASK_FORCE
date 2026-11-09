import os
import time
from contextlib import asynccontextmanager
from collections import defaultdict, deque

from dotenv import load_dotenv

load_dotenv()

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from .execution import execute_task
from .orchestrator import create_plan
from .providers import provider_is_configured, provider_status, test_provider_connections
from .schemas import ApprovalRequest, PlanRequest, PlanResponse
from .state import TASKS
from .worker_queue import TASK_QUEUE
from .web_research import web_research_is_configured


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A durable queue is enabled only when both DATABASE_URL and REDIS_URL
    # are configured. TaskStore validates that dependency before the app serves.
    await TASKS.initialize()
    worker_task = None
    if TASK_QUEUE is not None:
        await TASK_QUEUE.initialize()
        worker_task = __import__("asyncio").create_task(TASK_QUEUE.run(), name="workforce-redis-stream-worker")
    try:
        yield
    finally:
        if worker_task is not None:
            worker_task.cancel()
            try:
                await worker_task
            except __import__("asyncio").CancelledError:
                pass
        if TASK_QUEUE is not None:
            await TASK_QUEUE.close()
        await TASKS.close()


app = FastAPI(
    title="Autonomous AI Workforce API",
    version="0.3.0",
    description="Approval-gated multi-agent task planning with durable task history.",
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
        "version": "0.3.0",
        "storage_backend": TASKS.storage_backend,
        "durable_storage_configured": TASKS.durable_storage_configured,
        "task_queue_backend": "redis_streams" if TASK_QUEUE is not None else "background_tasks_preview",
        "durable_queue_configured": TASK_QUEUE is not None,
    }


@app.get("/health")
async def health():
    return {
        "status": "ok", "service": "autonomous-ai-workforce-api", "version": "0.3.0",
        "storage_backend": TASKS.storage_backend,
        "durable_storage_configured": TASKS.durable_storage_configured,
        "task_queue_backend": "redis_streams" if TASK_QUEUE is not None else "background_tasks_preview",
        "durable_queue_configured": TASK_QUEUE is not None,
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
            await TASKS.update(task_id, execution_status="failed", qa_status="rejected")
            await TASKS.add_event(task_id, "Workforce Queue", "queue_enqueue_failed", f"Durable queue submission failed ({type(exc).__name__}); no execution was started.")
            raise HTTPException(status_code=503, detail="The durable task queue is unavailable. The task was not started; check the queue configuration and retry with a new plan.") from exc
    else:
        background_tasks.add_task(execute_task, task_id)
    result = await TASKS.get(task_id)
    return result or updated

