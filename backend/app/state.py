import asyncio
import copy
import os
import uuid
from typing import Any

from dotenv import load_dotenv
from .database import PersistentTaskRepository
from .schemas import AgentMessage, PlanResponse, SharedMemoryEntry, TaskEvent, utc_now

load_dotenv()


class TaskStore:
    """Async task snapshot store with optional PostgreSQL/SQLite persistence."""

    def __init__(self, database_url: str | None = None) -> None:
        configured_url = database_url if database_url is not None else os.getenv("DATABASE_URL")
        self._repository = PersistentTaskRepository(configured_url) if configured_url and configured_url.strip() else None
        self.storage_backend = self._repository.backend if self._repository else "memory"
        self.durable_storage_configured = self._repository is not None
        self.durable_queue_configured = bool(os.getenv("REDIS_URL", "").strip() and self._repository is not None)
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        if self._repository is not None:
            await self._repository.initialize()
            records = await self._repository.load_all()
            async with self._lock:
                for record in records:
                    if record.get("execution_status") in {"queued", "running", "reviewing"}:
                        if self.durable_queue_configured:
                            # The unacknowledged Redis Stream entry is the durable
                            # source of truth; the worker will reclaim it after its
                            # configured idle lease expires.
                            record["execution_status"] = "queued"
                            record["qa_status"] = "pending_execution"
                            event_kind = "execution_requeued"
                            message = ("The API restarted during execution. The saved task was returned to "
                                       "the durable Redis queue and will be retried by the worker.")
                        else:
                            record["execution_status"] = "failed"
                            record["qa_status"] = "rejected"
                            event_kind = "execution_interrupted"
                            message = ("The API restarted while this task was executing. Its partial history "
                                       "was restored, but no durable queue is configured to resume it.")
                        event = TaskEvent(
                            id=str(uuid.uuid4()), actor="Workforce",
                            kind=event_kind, message=message,
                        )
                        record.setdefault("events", []).append(event.model_dump(mode="json"))
                        record["updated_at"] = utc_now()
                    self._tasks[str(record["task_id"])] = record
                    await self._persist_locked(record)
        self._initialized = True

    async def close(self) -> None:
        if self._repository is not None:
            await self._repository.close()
        self._initialized = False

    async def _persist_locked(self, record: dict[str, Any]) -> None:
        if self._repository is not None:
            await self._repository.save(record)

    async def create(self, plan: PlanResponse) -> PlanResponse:
        record = plan.model_dump(mode="json")
        if plan.category == "research":
            record["research_status"] = "not_started"
            record["research_notice"] = "Live web search begins only after you approve this research plan."
        record["updated_at"] = utc_now()
        async with self._lock:
            self._tasks[plan.task_id] = record
            await self._persist_locked(record)
        await self.add_event(plan.task_id, "Workforce", "plan_created",
                             "A specialist team plan was created and is waiting for your approval.")
        return await self.get(plan.task_id)  # type: ignore[return-value]

    async def get(self, task_id: str) -> PlanResponse | None:
        async with self._lock:
            record = self._tasks.get(task_id)
            snapshot = copy.deepcopy(record) if record is not None else None
        return PlanResponse.model_validate(snapshot) if snapshot is not None else None

    async def update(self, task_id: str, **fields: Any) -> PlanResponse | None:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            record.update(fields)
            record["updated_at"] = utc_now()
            await self._persist_locked(record)
            snapshot = copy.deepcopy(record)
        return PlanResponse.model_validate(snapshot)

    async def update_agent(self, task_id: str, agent_id: str, **fields: Any) -> None:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            for agent in record["agents"]:
                if agent["id"] == agent_id:
                    agent.update(fields)
                    break
            record["updated_at"] = utc_now()
            await self._persist_locked(record)

    async def update_subtask(self, task_id: str, subtask_id: str, **fields: Any) -> None:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            for subtask in record["subtasks"]:
                if subtask["id"] == subtask_id:
                    subtask.update(fields)
                    break
            record["updated_at"] = utc_now()
            await self._persist_locked(record)

    async def add_event(self, task_id: str, actor: str, kind: str, message: str) -> None:
        event = TaskEvent(id=str(uuid.uuid4()), actor=actor, kind=kind, message=message)
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is not None:
                record["events"].append(event.model_dump(mode="json"))
                record["updated_at"] = utc_now()
                await self._persist_locked(record)

    async def add_memory(self, task_id: str, source_agent: str, title: str, content: str) -> None:
        item = SharedMemoryEntry(id=str(uuid.uuid4()), source_agent=source_agent,
                                 title=title, content=content[:12000])
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is not None:
                record["shared_memory"].append(item.model_dump(mode="json"))
                record["updated_at"] = utc_now()
                await self._persist_locked(record)

    async def add_source(self, task_id: str, source: Any) -> None:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is not None:
                record["research_sources"].append(source.model_dump(mode="json"))
                record["updated_at"] = utc_now()
                await self._persist_locked(record)

    async def add_message(self, task_id: str, from_agent: str, to_agent: str, kind: str, content: str) -> None:
        item = AgentMessage(id=str(uuid.uuid4()), from_agent=from_agent, to_agent=to_agent,
                            kind=kind, content=content[:6000])
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is not None:
                record["messages"].append(item.model_dump(mode="json"))
                record["updated_at"] = utc_now()
                await self._persist_locked(record)


TASKS = TaskStore()
