import asyncio
import copy
import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Iterator

from dotenv import load_dotenv
from .database import PersistentTaskRepository, StaleExecutionFenceError, TaskWriteConflict
from .schemas import AgentMessage, PlanResponse, SharedMemoryEntry, TaskEvent, utc_now
from .worker_coordination import TaskLease

load_dotenv()


class TaskStore:
    """Async task snapshot store with canonical reads and optimistic writes."""

    def __init__(self, database_url: str | None = None) -> None:
        configured_url = database_url if database_url is not None else os.getenv("DATABASE_URL")
        self._repository = PersistentTaskRepository(configured_url) if configured_url and configured_url.strip() else None
        self.storage_backend = self._repository.backend if self._repository else "memory"
        self.durable_storage_configured = self._repository is not None
        self.durable_queue_configured = bool(os.getenv("REDIS_URL", "").strip() and self._repository is not None)
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._initialized = False
        self._execution_lease: ContextVar[TaskLease | None] = ContextVar(
            f"task_store_execution_lease_{id(self)}", default=None
        )

    async def initialize(
        self,
        active_lease_checker: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        if self._initialized:
            return
        if self._repository is not None:
            await self._repository.initialize()
            records = await self._repository.load_all()
            async with self._lock:
                for record in records:
                    needs_persist = False
                    status = record.get("execution_status")
                    if status == "queued":
                        if self.durable_queue_configured:
                            # Approved work remains queued. Its unacknowledged
                            # Redis Stream entry is reclaimed by the consumer group.
                            pass
                        else:
                            record["execution_status"] = "failed"
                            record["qa_status"] = "rejected"
                            event = TaskEvent(
                                id=str(uuid.uuid4()), actor="Workforce",
                                kind="execution_interrupted",
                                message=(
                                    "The service restarted before this queued task could run, and no durable "
                                    "queue is configured to resume it. Create a fresh approved run."
                                ),
                            )
                            record.setdefault("events", []).append(event.model_dump(mode="json"))
                            record["updated_at"] = utc_now()
                            needs_persist = True
                    elif status in {"running", "reviewing"}:
                        # A live worker may belong to another API instance. Only
                        # recover when Redis confirms there is no active owner.
                        active_owner = False
                        if self.durable_queue_configured and active_lease_checker is not None:
                            active_owner = await active_lease_checker(str(record["task_id"]))
                        if not active_owner:
                            record["execution_status"] = "revision_required"
                            record["qa_status"] = "rejected"
                            event = TaskEvent(
                                id=str(uuid.uuid4()), actor="Workforce Recovery",
                                kind="execution_requires_human_review",
                                message=(
                                    "The prior worker no longer holds an active Redis lease. Because a provider "
                                    "request may already have been sent, this task was not replayed automatically. "
                                    "Review its partial history and explicitly start a fresh run if appropriate."
                                ),
                            )
                            record.setdefault("events", []).append(event.model_dump(mode="json"))
                            record["updated_at"] = utc_now()
                            needs_persist = True
                    self._tasks[str(record["task_id"])] = record
                    if needs_persist:
                        recovery_fence = int(record.get("execution_fence", 0) or 0)
                        await self._persist_locked(
                            record,
                            execution_fence=recovery_fence if recovery_fence > 0 else None,
                        )
        self._initialized = True

    async def close(self) -> None:
        if self._repository is not None:
            await self._repository.close()
        self._initialized = False

    async def _refresh_locked(self, task_id: str) -> dict[str, Any] | None:
        """Refresh this instance's cache from the canonical database row."""
        if self._repository is not None:
            latest = await self._repository.get(task_id)
            if latest is None:
                self._tasks.pop(task_id, None)
                return None
            self._tasks[task_id] = latest
        return self._tasks.get(task_id)

    @contextmanager
    def execution_lease(self, lease: TaskLease | None) -> Iterator[None]:
        """Bind one worker lease to this async context and its child tasks.

        asyncio task creation copies ContextVars, so specialist gather() tasks
        inherit the same fence without relying on every helper to remember a
        lease argument. Explicit lease arguments still take precedence.
        """
        if lease is None:
            yield
            return
        token = self._execution_lease.set(lease)
        try:
            yield
        finally:
            self._execution_lease.reset(token)

    def _lease_generation(self, task_id: str, lease: TaskLease | None) -> int | None:
        if lease is None:
            lease = self._execution_lease.get()
        if lease is None:
            return None
        if lease.task_id != task_id:
            raise ValueError("Task lease does not belong to this task.")
        if lease.generation <= 0:
            raise ValueError("Task lease generation must be positive.")
        return int(lease.generation)

    async def activate_execution_lease(self, task_id: str, lease: TaskLease) -> bool:
        """Persist the lease generation before allowing worker execution."""
        generation = self._lease_generation(task_id, lease)
        assert generation is not None
        async with self._lock:
            record = await self._refresh_locked(task_id)
            if record is None:
                return False
            current_fence = int(record.get("execution_fence", 0))
            if generation <= current_fence:
                from .database import StaleExecutionFenceError
                raise StaleExecutionFenceError(
                    task_id,
                    int(record.get("version", 0)),
                    int(record.get("version", 0)),
                    generation,
                    current_fence,
                )
            record["execution_fence"] = generation
            record["updated_at"] = utc_now()
            if self._repository is not None:
                try:
                    record["version"] = await self._repository.activate_fence(
                        task_id,
                        generation,
                        expected_version=int(record.get("version", 0)),
                    )
                except TaskWriteConflict:
                    latest = await self._repository.get(task_id)
                    if latest is None:
                        self._tasks.pop(task_id, None)
                    else:
                        self._tasks[task_id] = latest
                    raise
            else:
                await self._persist_locked(record, execution_fence=generation)
            return True

    async def _persist_locked(
        self,
        record: dict[str, Any],
        execution_fence: int | None = None,
    ) -> None:
        task_id = str(record["task_id"])
        current_fence = int(record.get("execution_fence", 0) or 0)
        if execution_fence is None and current_fence > 0:
            # Once a worker generation is active, no unscoped mutation may
            # silently adopt the latest database fence. This protects the
            # pipeline even if a future helper forgets to pass/bind its lease.
            version = int(record.get("version", 0) or 0)
            raise StaleExecutionFenceError(
                task_id, version, version, requested_fence=0, current_fence=current_fence
            )
        if self._repository is not None:
            try:
                record["version"] = await self._repository.save(
                    record,
                    execution_fence=execution_fence,
                )
            except TaskWriteConflict:
                # Do not leave a stale mutated object in this instance's cache.
                latest = await self._repository.get(task_id)
                if latest is None:
                    self._tasks.pop(task_id, None)
                else:
                    self._tasks[task_id] = latest
                raise
        else:
            record["version"] = int(record.get("version", 0)) + 1

    async def create(self, plan: PlanResponse) -> PlanResponse:
        record = plan.model_dump(mode="json")
        if plan.category == "research":
            record["research_status"] = "not_started"
            record["research_notice"] = "Live web search begins only after you approve this research plan."
        record["version"] = 0
        record["execution_fence"] = 0
        record["updated_at"] = utc_now()
        async with self._lock:
            self._tasks[plan.task_id] = record
            await self._persist_locked(record)
        await self.add_event(plan.task_id, "Workforce", "plan_created",
                             "A specialist team plan was created and is waiting for your approval.")
        return await self.get(plan.task_id)  # type: ignore[return-value]

    async def get(self, task_id: str) -> PlanResponse | None:
        if self._repository is not None:
            latest = await self._repository.get(task_id)
            async with self._lock:
                if latest is None:
                    self._tasks.pop(task_id, None)
                else:
                    self._tasks[task_id] = latest
                snapshot = copy.deepcopy(latest)
        else:
            async with self._lock:
                snapshot = copy.deepcopy(self._tasks.get(task_id))
        return PlanResponse.model_validate(snapshot) if snapshot is not None else None

    async def update(
        self,
        task_id: str,
        *,
        lease: TaskLease | None = None,
        **fields: Any,
    ) -> PlanResponse | None:
        """Update a task; workers pass their lease to enforce database fencing."""
        generation = self._lease_generation(task_id, lease)
        async with self._lock:
            record = await self._refresh_locked(task_id)
            if record is None:
                return None
            record.update(fields)
            if generation is not None:
                record["execution_fence"] = generation
            record["updated_at"] = utc_now()
            await self._persist_locked(record, execution_fence=generation)
            snapshot = copy.deepcopy(record)
        return PlanResponse.model_validate(snapshot)

    async def update_agent(
        self, task_id: str, agent_id: str, *, lease: TaskLease | None = None, **fields: Any
    ) -> None:
        generation = self._lease_generation(task_id, lease)
        async with self._lock:
            record = await self._refresh_locked(task_id)
            if record is None:
                return
            for agent in record["agents"]:
                if agent["id"] == agent_id:
                    agent.update(fields)
                    break
            if generation is not None:
                record["execution_fence"] = generation
            record["updated_at"] = utc_now()
            await self._persist_locked(record, execution_fence=generation)

    async def update_subtask(
        self, task_id: str, subtask_id: str, *, lease: TaskLease | None = None, **fields: Any
    ) -> None:
        generation = self._lease_generation(task_id, lease)
        async with self._lock:
            record = await self._refresh_locked(task_id)
            if record is None:
                return
            for subtask in record["subtasks"]:
                if subtask["id"] == subtask_id:
                    subtask.update(fields)
                    break
            if generation is not None:
                record["execution_fence"] = generation
            record["updated_at"] = utc_now()
            await self._persist_locked(record, execution_fence=generation)

    async def add_event(
        self, task_id: str, actor: str, kind: str, message: str, *,
        lease: TaskLease | None = None,
    ) -> None:
        generation = self._lease_generation(task_id, lease)
        event = TaskEvent(id=str(uuid.uuid4()), actor=actor, kind=kind, message=message)
        async with self._lock:
            record = await self._refresh_locked(task_id)
            if record is not None:
                record["events"].append(event.model_dump(mode="json"))
                if generation is not None:
                    record["execution_fence"] = generation
                record["updated_at"] = utc_now()
                await self._persist_locked(record, execution_fence=generation)

    async def add_memory(
        self, task_id: str, source_agent: str, title: str, content: str, *,
        lease: TaskLease | None = None,
    ) -> None:
        generation = self._lease_generation(task_id, lease)
        item = SharedMemoryEntry(id=str(uuid.uuid4()), source_agent=source_agent,
                                 title=title, content=content[:12000])
        async with self._lock:
            record = await self._refresh_locked(task_id)
            if record is not None:
                record["shared_memory"].append(item.model_dump(mode="json"))
                if generation is not None:
                    record["execution_fence"] = generation
                record["updated_at"] = utc_now()
                await self._persist_locked(record, execution_fence=generation)

    async def add_source(
        self, task_id: str, source: Any, *, lease: TaskLease | None = None
    ) -> None:
        generation = self._lease_generation(task_id, lease)
        async with self._lock:
            record = await self._refresh_locked(task_id)
            if record is not None:
                record["research_sources"].append(source.model_dump(mode="json"))
                if generation is not None:
                    record["execution_fence"] = generation
                record["updated_at"] = utc_now()
                await self._persist_locked(record, execution_fence=generation)

    async def add_message(
        self, task_id: str, from_agent: str, to_agent: str, kind: str, content: str, *,
        lease: TaskLease | None = None,
    ) -> None:
        generation = self._lease_generation(task_id, lease)
        item = AgentMessage(id=str(uuid.uuid4()), from_agent=from_agent, to_agent=to_agent,
                            kind=kind, content=content[:6000])
        async with self._lock:
            record = await self._refresh_locked(task_id)
            if record is not None:
                record["messages"].append(item.model_dump(mode="json"))
                if generation is not None:
                    record["execution_fence"] = generation
                record["updated_at"] = utc_now()
                await self._persist_locked(record, execution_fence=generation)


TASKS = TaskStore()
