from typing import Literal
from pydantic import BaseModel, Field


class PlanRequest(BaseModel):
    instruction: str = Field(min_length=8, max_length=12000)


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


class PlanResponse(BaseModel):
    task_id: str
    title: str
    category: str
    execution_mode: Literal["demo", "provider_planned"]
    notice: str
    agents: list[AgentPlan]
    subtasks: list[SubtaskPlan]
    qa_status: str = "pending_execution"
