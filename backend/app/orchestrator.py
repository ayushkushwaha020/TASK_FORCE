import re
import uuid
from typing import Any

from .providers import provider_is_configured, request_json_plan
from .schemas import AgentPlan, PlanRequest, PlanResponse, SubtaskPlan


ROLE_SETS: dict[str, list[tuple[str, str, str, str]]] = {
    "software_development": [
        ("Project Manager", "coordinates scope, dependencies, and delivery", "🧭", "organised"),
        ("Product Analyst", "translates the request into user requirements", "📋", "curious"),
        ("System Architect", "designs the application structure and interfaces", "🏗️", "methodical"),
        ("Frontend Engineer", "plans the user interface and interactions", "🎨", "creative"),
        ("Backend Engineer", "plans APIs, business logic, and services", "⚙️", "precise"),
        ("Database Engineer", "plans data models and persistence", "🗄️", "analytical"),
        ("Security Specialist", "reviews trust boundaries and security risks", "🛡️", "cautious"),
        ("QA Verifier", "independently checks requirements and test evidence", "🧪", "skeptical"),
    ],
    "research": [
        ("Research Director", "coordinates the research question and evidence plan", "🧭", "systematic"),
        ("Primary Researcher", "investigates the central research question", "🔎", "curious"),
        ("Evidence Researcher", "collects supporting and contrasting evidence", "📚", "thorough"),
        ("Data Analyst", "organises and interprets quantitative evidence", "📊", "analytical"),
        ("Source Verifier", "checks source quality, dates, and traceability", "🔍", "skeptical"),
        ("Critical Reviewer", "identifies gaps, assumptions, and counterarguments", "🧠", "challenging"),
        ("Report Editor", "structures the final report and citations", "✍️", "careful"),
        ("QA Verifier", "checks claims, citations, and requirement coverage", "🧪", "independent"),
    ],
    "event_planning": [
        ("Event Director", "coordinates the event objectives and overall plan", "🎪", "decisive"),
        ("Operations Planner", "organises the event workflow and run of show", "🗂️", "organised"),
        ("Budget Planner", "builds the budget and cost assumptions", "💹", "practical"),
        ("Venue Coordinator", "defines venue and accessibility requirements", "🏛️", "attentive"),
        ("Logistics Planner", "plans equipment, travel, and on-site resources", "📦", "methodical"),
        ("Marketing Lead", "plans promotion and audience communication", "📣", "outgoing"),
        ("Risk Specialist", "identifies contingencies and safety considerations", "🛡️", "cautious"),
        ("QA Verifier", "checks schedule, budget, dependencies, and completeness", "🧪", "independent"),
    ],
    "general": [
        ("Task Director", "coordinates the objective and delivery plan", "🧭", "organised"),
        ("Domain Specialist", "handles the main subject-matter workstream", "🧠", "curious"),
        ("Research Analyst", "gathers context and supporting information", "🔎", "thorough"),
        ("Operations Planner", "organises steps, resources, and dependencies", "🗂️", "methodical"),
        ("Editor", "integrates outputs into a clear final deliverable", "✍️", "careful"),
        ("QA Verifier", "independently checks completeness and correctness", "🧪", "skeptical"),
    ],
}


def classify(instruction: str) -> str:
    text = instruction.lower()
    if any(word in text for word in ("code", "coding", "website", "web app", "application", "software", "api", "debug", "program")):
        return "software_development"
    if any(word in text for word in ("research", "analyse", "analyze", "paper", "report", "study", "compare", "investigate")):
        return "research"
    if any(word in text for word in ("event", "conference", "hackathon", "workshop", "launch", "meetup")):
        return "event_planning"
    return "general"


def make_demo_plan(instruction: str, category: str) -> tuple[list[AgentPlan], list[SubtaskPlan]]:
    roles = ROLE_SETS[category]
    # Small requests use fewer specialists; complex requests keep the full team.
    complexity = len(instruction.split())
    count = min(len(roles), max(3, 4 if complexity < 18 else len(roles)))
    selected = roles[:count]
    agents = [
        AgentPlan(
            id=f"agent-{index + 1}",
            name=f"{role.split()[0]} {index + 1}",
            role=role,
            personality=personality,
            task=task,
            emoji=emoji,
        )
        for index, (role, task, emoji, personality) in enumerate(selected)
    ]
    work = [
        ("Clarify objective and acceptance criteria", agents[0].name),
        ("Break work into specialist workstreams", agents[min(1, len(agents)-1)].name),
        ("Integrate outputs and resolve dependencies", agents[min(2, len(agents)-1)].name),
        ("Independent quality and completeness review", next(a.name for a in agents if "QA" in a.role or "Verifier" in a.role)),
    ]
    subtasks = [SubtaskPlan(id=f"step-{i+1}", title=title, owner=owner) for i, (title, owner) in enumerate(work)]
    return agents, subtasks


async def create_plan(request: PlanRequest) -> PlanResponse:
    task_id = str(uuid.uuid4())
    instruction = request.instruction.strip()
    category = classify(instruction)
    title = re.sub(r"\s+", " ", instruction)
    title = title[:92] + ("…" if len(title) > 92 else "")

    if provider_is_configured():
        try:
            data: dict[str, Any] = await request_json_plan(instruction)
            raw_agents = data["agents"][:20]
            agents = [
                AgentPlan(
                    id=f"agent-{i+1}",
                    name=str(a.get("name", f"Specialist {i+1}"))[:60],
                    role=str(a.get("role", "Specialist"))[:80],
                    personality=str(a.get("personality", "focused"))[:40],
                    task=str(a.get("task", "Contribute to the assigned workstream"))[:240],
                    emoji=str(a.get("emoji", "🤖"))[:8],
                )
                for i, a in enumerate(raw_agents)
            ]
            if not any("qa" in a.role.lower() or "verif" in a.role.lower() for a in agents):
                agents.append(AgentPlan(
                    id=f"agent-{len(agents)+1}", name="QA Verifier", role="Independent QA Verifier",
                    personality="skeptical", task="Review the plan and later verify deliverables independently.", emoji="🧪"
                ))
            agents = agents[:20]
            raw_steps = data["subtasks"][:12]
            subtasks = [
                SubtaskPlan(
                    id=f"step-{i+1}",
                    title=str(s.get("title", "Complete assigned work"))[:160],
                    owner=str(s.get("owner", "Task Director"))[:60],
                )
                for i, s in enumerate(raw_steps)
            ]
            return PlanResponse(
                task_id=task_id,
                title=str(data.get("title") or title)[:120],
                category=str(data.get("category") or category)[:60],
                execution_mode="provider_planned",
                notice="A configured AI provider generated this plan. This is planning only: no external actions, code execution, or deliverable verification have occurred yet.",
                agents=agents,
                subtasks=subtasks,
                qa_status="pending_execution",
            )
        except Exception as exc:
            agents, subtasks = make_demo_plan(instruction, category)
            return PlanResponse(
                task_id=task_id, title=title, category=category, execution_mode="demo",
                notice=f"Provider planning failed ({type(exc).__name__}). A local demo plan is shown; no AI-provider execution or task execution is claimed.",
                agents=agents, subtasks=subtasks, qa_status="pending_execution",
            )

    agents, subtasks = make_demo_plan(instruction, category)
    return PlanResponse(
        task_id=task_id,
        title=title,
        category=category,
        execution_mode="demo",
        notice="Demo planning mode: this team and its workstreams were generated by local rules. No live AI agents, external tools, code execution, or verification have run.",
        agents=agents,
        subtasks=subtasks,
        qa_status="pending_execution",
    )
