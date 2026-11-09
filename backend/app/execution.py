import asyncio
import json
import uuid
from typing import Any

from .providers import parse_json_object, provider_is_configured, request_completion
from .schemas import PlanResponse
from .state import TASKS


MAX_ACTIVE_SPECIALISTS = 8
MAX_PARALLEL_PROVIDER_CALLS = 4
MAX_QA_REVISION_ROUNDS = 1
_provider_semaphore = asyncio.Semaphore(MAX_PARALLEL_PROVIDER_CALLS)
_execution_semaphore = asyncio.Semaphore(2)


def _qa_agent(task: PlanResponse) -> dict[str, Any]:
    return next(
        (agent.model_dump() for agent in task.agents if "qa" in agent.role.lower() or "verif" in agent.role.lower()),
        {"id": "qa-fallback", "name": "Independent QA", "role": "Independent QA Verifier", "task": "Review the deliverable", "emoji": "🧪"},
    )


def _worker_context(task: PlanResponse, revision_notes: str | None = None) -> str:
    entries = task.shared_memory[-12:]
    memory_text = "\n\n".join(
        f"[{entry.source_agent} — {entry.title}]\n{entry.content[:1600]}" for entry in entries
    ) or "No shared findings yet."
    revision = f"\n\nQA revision instructions:\n{revision_notes}" if revision_notes else ""
    return (
        f"USER MISSION:\n{task.instruction}\n\n"
        f"TASK CATEGORY: {task.category}\n\n"
        f"SHARED TEAM MEMORY (use as context, not as unquestionable truth):\n{memory_text}"
        f"{revision}\n\n"
        "Capability boundary: you have no browser, filesystem, terminal, code runner, or external-action tools in this milestone. "
        "Do not claim to have run code, browsed a website, contacted anyone, or verified an external fact. "
        "Clearly label assumptions, uncertainty, and actions that still need a tool or human."
    )


async def _run_specialist(task_id: str, task: PlanResponse, agent: dict[str, Any], revision_notes: str | None = None) -> dict[str, Any]:
    agent_id = str(agent["id"])
    name = str(agent["name"])
    await TASKS.update_agent(task_id, agent_id, status="running")
    await TASKS.add_event(task_id, name, "agent_started", f"{name} started its assigned specialist workstream.")
    system = (
        f"You are {name}, serving as {agent.get('role', 'Specialist')}. "
        f"Your working style is {agent.get('personality', 'focused')}. "
        f"Your responsibility: {agent.get('task', 'Contribute to the mission')}. "
        "Do useful, specific work for the user's mission. Produce a structured result with findings, decisions, assumptions, risks, "
        "and concrete next steps where relevant. Be candid about what you cannot verify. Do not imitate other agents or claim tool use."
    )
    user = _worker_context(task, revision_notes) + f"\n\nYOUR ASSIGNED ROLE TASK:\n{agent.get('task', '')}"
    try:
        async with _provider_semaphore:
            output = await request_completion(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.25,
            )
        await TASKS.update_agent(task_id, agent_id, status="completed")
        await TASKS.add_memory(task_id, name, f"{agent.get('role', 'Specialist')} findings", output[:10000])
        await TASKS.add_message(task_id, name, "Team Director", "workstream_result", output[:5000])
        await TASKS.add_event(task_id, name, "agent_completed", f"{name} delivered its specialist findings to shared team memory.")
        return {"agent": agent, "output": output, "error": None}
    except Exception as exc:
        await TASKS.update_agent(task_id, agent_id, status="failed")
        await TASKS.add_event(task_id, name, "agent_failed", f"{name} could not complete its workstream ({type(exc).__name__}).")
        return {"agent": agent, "output": "", "error": type(exc).__name__}


async def _recover_failed_workers(task_id: str, task: PlanResponse, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failed = [item for item in results if item.get("error")]
    successful = [item for item in results if not item.get("error") and item.get("output") ]
    if not failed:
        return results
    backup = next((item["agent"] for item in successful if "director" in item["agent"].get("role", "").lower() or "manager" in item["agent"].get("role", "").lower()), None)
    backup = backup or (successful[0]["agent"] if successful else None)
    if backup is None:
        return results
    for item in failed:
        original = item["agent"]
        await TASKS.add_event(task_id, "Workforce Manager", "recovery_started", f"Reassigning {original['name']}'s workstream after a provider failure.")
        system = (
            f"You are acting as a recovery specialist for {original.get('role', 'a specialist')}. "
            "Complete the missing workstream using the mission and team context. State assumptions and limits. "
            "You have no external tools and must not claim tool execution."
        )
        user = _worker_context(task) + f"\n\nREASSIGNED WORK:\n{original.get('task', '')}"
        try:
            async with _provider_semaphore:
                output = await request_completion([{"role": "system", "content": system}, {"role": "user", "content": user}], temperature=0.2)
            item["output"] = output
            item["error"] = None
            await TASKS.update_agent(task_id, str(original["id"]), status="reassigned")
            await TASKS.add_memory(task_id, str(backup["name"]), f"Recovered work for {original['name']}", output[:10000])
            await TASKS.add_message(task_id, str(backup["name"]), "Team Director", "reassigned_work", output[:5000])
            await TASKS.add_event(task_id, str(backup["name"]), "recovery_succeeded", f"The failed workstream was reassigned and recovered for {original['name']}.")
        except Exception as exc:
            await TASKS.add_event(task_id, "Workforce Manager", "recovery_failed", f"Recovery for {original['name']} failed ({type(exc).__name__}); human intervention is required.")
    return results


async def _team_deliberation(task_id: str, task: PlanResponse, results: list[dict[str, Any]]) -> str:
    director = next((item["agent"] for item in results if "director" in item["agent"].get("role", "").lower() or "manager" in item["agent"].get("role", "").lower()), results[0]["agent"] if results else {"name": "Team Director"})
    evidence = "\n\n".join(
        f"### {item['agent']['name']} — {item['agent'].get('role', 'Specialist')}\n{item['output'][:4200]}"
        for item in results if item.get("output")
    )
    system = (
        "You are facilitating a transparent multi-specialist team review. Compare the specialists' independent work. "
        "Identify agreements, disagreements, incompatible assumptions, missing requirements, and practical resolutions. "
        "Do not invent a consensus: preserve unresolved disagreements and label assumptions."
    )
    user = f"MISSION:\n{task.instruction}\n\nSPECIALIST CONTRIBUTIONS:\n{evidence}\n\nReturn a concise team deliberation memo."
    async with _provider_semaphore:
        discussion = await request_completion([{"role": "system", "content": system}, {"role": "user", "content": user}], temperature=0.2)
    await TASKS.update(task_id, team_discussion=discussion[:12000])
    await TASKS.add_memory(task_id, str(director.get("name", "Team Director")), "Team deliberation and disagreements", discussion[:10000])
    await TASKS.add_message(task_id, "Team Director", "All Specialists", "team_deliberation", discussion[:5000])
    await TASKS.add_event(task_id, str(director.get("name", "Team Director")), "team_deliberation", "Specialist findings were compared; agreements and unresolved differences were recorded.")
    return discussion


async def _compile_deliverable(task_id: str, task: PlanResponse, results: list[dict[str, Any]], discussion: str, revision_notes: str | None = None) -> str:
    contributions = "\n\n".join(
        f"## {item['agent']['name']} ({item['agent'].get('role', 'Specialist')})\n{item['output'][:5000]}"
        for item in results if item.get("output")
    )
    system = (
        "You are the principal editor of a specialist AI workforce. Integrate the contributions into one useful, complete deliverable. "
        "Resolve contradictions only when the evidence supports doing so; otherwise describe the difference. "
        "Separate verified information from assumptions and recommendations. Never fabricate sources, test results, or completed external actions. "
        "Use clear Markdown headings and actionable content appropriate to the user's request."
    )
    user = (
        f"USER MISSION:\n{task.instruction}\n\nTEAM DELIBERATION:\n{discussion[:9000]}"
        f"\n\nSPECIALIST OUTPUTS:\n{contributions[:24000]}"
        + (f"\n\nQA REVISION REQUEST:\n{revision_notes}" if revision_notes else "")
        + "\n\nCreate the integrated deliverable now. State any scope limits clearly."
    )
    async with _provider_semaphore:
        result = await request_completion([{"role": "system", "content": system}, {"role": "user", "content": user}], temperature=0.2)
    await TASKS.update(task_id, result_markdown=result[:30000])
    await TASKS.add_event(task_id, "Report Editor", "deliverable_drafted", "The specialist contributions were integrated into a draft deliverable.")
    return result


async def _qa_review(task_id: str, task: PlanResponse, deliverable: str, discussion: str) -> dict[str, Any]:
    qa = _qa_agent(task)
    await TASKS.update_agent(task_id, str(qa["id"]), status="running")
    await TASKS.update(task_id, execution_status="reviewing", qa_status="in_progress")
    await TASKS.add_event(task_id, str(qa["name"]), "qa_started", "Independent QA is reviewing the integrated deliverable against the original mission.")
    system = (
        "You are the independent QA authority. You did not create the deliverable. Review it critically against the user's exact mission. "
        "Check completeness, internal consistency, clarity, unsupported factual claims, false claims of execution, and disclosed limitations. "
        "Approve only when the deliverable is fit for purpose. Otherwise reject it and give specific, actionable revision instructions. "
        "Return JSON only: decision (approve or revise), summary (string), issues (array of strings), "
        "revision_instructions (string), affected_agents (array of agent names). Do not approve merely to be agreeable."
    )
    user = (
        f"ORIGINAL USER MISSION:\n{task.instruction}\n\nTEAM REVIEW NOTES:\n{discussion[:5000]}"
        f"\n\nDRAFT DELIVERABLE:\n{deliverable[:20000]}"
    )
    async with _provider_semaphore:
        raw = await request_completion([{"role": "system", "content": system}, {"role": "user", "content": user}], temperature=0.0, json_mode=True)
    verdict = parse_json_object(raw)
    decision = str(verdict.get("decision", "revise")).strip().lower()
    if decision not in ("approve", "revise"):
        decision = "revise"
    verdict["decision"] = decision
    verdict["summary"] = str(verdict.get("summary", "QA response received."))[:3000]
    verdict["revision_instructions"] = str(verdict.get("revision_instructions", "Address the QA issues and submit a corrected deliverable."))[:6000]
    issues = verdict.get("issues", [])
    verdict["issues"] = [str(issue)[:1000] for issue in issues[:12]] if isinstance(issues, list) else [str(issues)[:1000]]
    affected = verdict.get("affected_agents", [])
    verdict["affected_agents"] = [str(name)[:80] for name in affected[:8]] if isinstance(affected, list) else []
    await TASKS.update_agent(task_id, str(qa["id"]), status="completed" if decision == "approve" else "revision_requested")
    await TASKS.update(task_id, qa_feedback=json.dumps(verdict, ensure_ascii=False))
    await TASKS.add_memory(task_id, str(qa["name"]), "Independent QA verdict", json.dumps(verdict, ensure_ascii=False))
    await TASKS.add_message(task_id, str(qa["name"]), "Workforce Manager", "qa_verdict", json.dumps(verdict, ensure_ascii=False))
    await TASKS.add_event(task_id, str(qa["name"]), "qa_passed" if decision == "approve" else "qa_rejected", verdict["summary"])
    return verdict


async def execute_task(task_id: str) -> None:
    """Execute an approved plan through model-backed specialists and independent AI QA."""
    async with _execution_semaphore:
        task = await TASKS.get(task_id)
        if task is None or task.execution_status != "queued":
            return
        if not provider_is_configured() or task.execution_mode != "provider_planned":
            await TASKS.update(task_id, execution_status="failed", qa_status="rejected")
            await TASKS.add_event(task_id, "Workforce", "execution_blocked", "No live provider is configured; demo plans cannot be executed as real agent work.")
            return
        try:
            await TASKS.update(task_id, execution_status="running", approval_status="approved")
            await TASKS.add_event(task_id, "Workforce Manager", "execution_started", "Human approval received. Specialist execution has started.")
            task = await TASKS.get(task_id)
            assert task is not None
            qa = _qa_agent(task)
            workers = [agent.model_dump() for agent in task.agents if agent.id != qa["id"] and "qa" not in agent.role.lower() and "verif" not in agent.role.lower()]
            active = workers[:MAX_ACTIVE_SPECIALISTS]
            idle = workers[MAX_ACTIVE_SPECIALISTS:]
            for agent in idle:
                await TASKS.update_agent(task_id, agent["id"], status="standby")
            await TASKS.update_agent(task_id, qa["id"], status="waiting_for_review")
            if not active:
                raise RuntimeError("The approved team has no execution specialists.")
            await TASKS.add_event(task_id, "Workforce Manager", "team_activated", f"Activated {len(active)} specialists in parallel; {len(idle)} additional team members remain on standby to control provider usage.")
            results = await asyncio.gather(*[_run_specialist(task_id, task, agent) for agent in active])
            results = await _recover_failed_workers(task_id, task, results)
            if any(item.get("error") for item in results):
                raise RuntimeError("One or more specialist workstreams failed after bounded recovery.")
            task = await TASKS.get(task_id)
            assert task is not None
            grouped_outputs: dict[str, list[str]] = {}
            for index, item in enumerate(results):
                if task.subtasks:
                    subtask = task.subtasks[index % len(task.subtasks)]
                    grouped_outputs.setdefault(subtask.id, []).append(
                        f"[{item['agent']['name']}]\n{item['output'][:3500]}"
                    )
            for subtask in task.subtasks:
                outputs = grouped_outputs.get(subtask.id, [])
                if outputs:
                    await TASKS.update_subtask(
                        task_id, subtask.id, status="completed",
                        output="\n\n".join(outputs)[:10000],
                    )
            discussion = await _team_deliberation(task_id, task, results)
            deliverable = await _compile_deliverable(task_id, task, results, discussion)
            verdict: dict[str, Any] | None = None
            for revision_round in range(MAX_QA_REVISION_ROUNDS + 1):
                task = await TASKS.get(task_id)
                assert task is not None
                verdict = await _qa_review(task_id, task, deliverable, discussion)
                if verdict["decision"] == "approve":
                    break
                if revision_round >= MAX_QA_REVISION_ROUNDS:
                    break
                await TASKS.update(task_id, execution_status="running")
                await TASKS.add_event(task_id, "Workforce Manager", "revision_cycle_started", "QA rejected the draft. The affected specialists have been asked to revise their work.")
                affected_names = {name.casefold() for name in verdict.get("affected_agents", [])}
                revise_agents = [item["agent"] for item in results if not affected_names or item["agent"]["name"].casefold() in affected_names]
                if not revise_agents:
                    revise_agents = [item["agent"] for item in results[:3]]
                notes = verdict["revision_instructions"] + "\nQA issues: " + "; ".join(verdict.get("issues", []))
                revised = await asyncio.gather(*[_run_specialist(task_id, task, agent, notes) for agent in revise_agents])
                if any(item.get("error") for item in revised):
                    raise RuntimeError("A requested QA revision failed to complete.")
                results = [next((new for new in revised if new["agent"]["id"] == old["agent"]["id"]), old) for old in results]
                task = await TASKS.get(task_id)
                assert task is not None
                discussion = await _team_deliberation(task_id, task, results)
                deliverable = await _compile_deliverable(task_id, task, results, discussion, notes)
            if verdict and verdict["decision"] == "approve":
                await TASKS.update(task_id, execution_status="completed", qa_status="passed", qa_feedback=json.dumps(verdict, ensure_ascii=False), result_markdown=deliverable)
                await TASKS.add_event(task_id, str(qa["name"]), "task_completed", "Independent AI QA approved the final draft. No external tools or code execution were used in this milestone.")
            else:
                summary = verdict["summary"] if verdict else "QA did not return a valid approval."
                await TASKS.update(task_id, execution_status="revision_required", qa_status="rejected", qa_feedback=json.dumps(verdict or {}, ensure_ascii=False))
                await TASKS.add_event(task_id, str(qa["name"]), "human_review_required", f"QA did not approve the deliverable after the bounded revision cycle: {summary}")
        except Exception as exc:
            await TASKS.update(task_id, execution_status="failed", qa_status="rejected")
            await TASKS.add_event(task_id, "Workforce Recovery", "execution_failed", f"The execution pipeline stopped safely ({type(exc).__name__}). Review the task and start a new run after correcting the issue.")
