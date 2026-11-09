import asyncio
import copy
import uuid
from typing import Any

from .schemas import (
    AgentMessage,
    PlanResponse,
    SharedMemoryEntry,
    TaskEvent,
    utc_now,
)


class TaskStore:
    """Process-local task store for the V2 preview.

    A persistent database replaces this store in the production milestone.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def create(self, plan: PlanResponse) -> PlanResponse:
        record = plan.model_dump(mode="json")
        record["updated_at"] = utc_now()
        async with self._lock:
            self._tasks[plan.task_id] = record
        await self.add_event(plan.task_id, "Workforce", "plan_created", "A specialist team plan was created and is waiting for your approval.")
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

    async def add_event(self, task_id: str, actor: str, kind: str, message: str) -> None:
        event = TaskEvent(id=str(uuid.uuid4()), actor=actor, kind=kind, message=message)
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is not None:
                record["events"].append(event.model_dump(mode="json"))
                record["updated_at"] = utc_now()

    async def add_memory(self, task_id: str, source_agent: str, title: str, content: str) -> None:
        item = SharedMemoryEntry(
            id=str(uuid.uuid4()), source_agent=source_agent, title=title, content=content[:12000]
        )
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is not None:
                record["shared_memory"].append(item.model_dump(mode="json"))
                record["updated_at"] = utc_now()

    async def add_message(self, task_id: str, from_agent: str, to_agent: str, kind: str, content: str) -> None:
        item = AgentMessage(
            id=str(uuid.uuid4()), from_agent=from_agent, to_agent=to_agent,
            kind=kind, content=content[:6000]
        )
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is not None:
                record["messages"].append(item.model_dump(mode="json"))
                record["updated_at"] = utc_now()


TASKS = TaskStore()
