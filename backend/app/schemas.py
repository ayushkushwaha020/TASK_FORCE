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


class ResearchSource(BaseModel):
    source_id: str
    query: str
    title: str
    url: str
    snippet: str
    score: float | None = None
    published_date: str | None = None
    provider: str = "tavily"


class PlanResponse(BaseModel):
    task_id: str
    instruction: str
    title: str
    category: str
    execution_mode: Literal["demo", "provider_planned"]
    notice: str
    agents: list[AgentPlan]
    subtasks: list[SubtaskPlan]
    # Concurrency metadata is part of every canonical task snapshot.
    version: int = Field(default=1, ge=0)
    execution_fence: int = Field(default=0, ge=0)
    execution_status: Literal[
        "awaiting_approval", "queued", "running", "reviewing",
        "completed", "revision_required", "failed", "rejected", "cancelled"
    ] = "awaiting_approval"
    approval_status: Literal["pending", "approved", "rejected"] = "pending"
    qa_status: Literal["pending_execution", "in_progress", "passed", "rejected", "failed"] = "pending_execution"
    qa_feedback: str | None = None
    team_discussion: str | None = None
    result_markdown: str | None = None
    shared_memory: list[SharedMemoryEntry] = Field(default_factory=list)
    research_sources: list[ResearchSource] = Field(default_factory=list)
    research_status: Literal["not_applicable", "not_started", "searching", "completed", "partial", "failed", "not_configured"] = "not_applicable"
    research_notice: str | None = None
    research_credits: int = 0
    messages: list[AgentMessage] = Field(default_factory=list)
    events: list[TaskEvent] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
