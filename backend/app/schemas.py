from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlanRequest(BaseModel):
    instruction: str = Field(min_length=8, max_length=12000)


class ApprovalRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=1000)


class AgentPlan(BaseModel):
    id: str
    name: str
    role: str
    personality: str
    status: str = "planned"
    task: str
    emoji: str = "🤖"


class SubtaskPlan(BaseModel):
    id: str
    title: str
    owner: str
    status: str = "planned"
    output: str | None = None


class TaskEvent(BaseModel):
    id: str
    timestamp: str = Field(default_factory=utc_now)
    actor: str
    kind: str
    message: str


class SharedMemoryEntry(BaseModel):
    id: str
    timestamp: str = Field(default_factory=utc_now)
    source_agent: str
    title: str
    content: str


class AgentMessage(BaseModel):
    id: str
    timestamp: str = Field(default_factory=utc_now)
    from_agent: str
    to_agent: str
    kind: str
    content: str


class PlanResponse(BaseModel):
    task_id: str
    instruction: str
    title: str
    category: str
    execution_mode: Literal["demo", "provider_planned"]
    notice: str
    agents: list[AgentPlan]
    subtasks: list[SubtaskPlan]
    execution_status: Literal[
        "awaiting_approval", "queued", "running", "reviewing",
        "completed", "revision_required", "failed", "rejected"
    ] = "awaiting_approval"
    approval_status: Literal["pending", "approved", "rejected"] = "pending"
    qa_status: Literal["pending_execution", "in_progress", "passed", "rejected"] = "pending_execution"
    qa_feedback: str | None = None
    team_discussion: str | None = None
    result_markdown: str | None = None
    shared_memory: list[SharedMemoryEntry] = Field(default_factory=list)
    messages: list[AgentMessage] = Field(default_factory=list)
    events: list[TaskEvent] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
