import json
import unittest
import uuid
from unittest.mock import patch

from app.execution import execute_task
from app.schemas import AgentPlan, PlanResponse, SubtaskPlan
from app.state import TASKS
from app.worker_coordination import TaskLease


class ExecutionPipelineTests(unittest.IsolatedAsyncioTestCase):
    def make_plan(self) -> PlanResponse:
        return PlanResponse(
            task_id=str(uuid.uuid4()),
            instruction="Prepare a structured plan for a small campus technology event.",
            title="Campus technology event plan",
            category="event_planning",
            execution_mode="provider_planned",
            notice="Provider-backed plan ready for approval.",
            agents=[
                AgentPlan(id="director", name="Project Director", role="Project Manager", personality="organised", task="Coordinate the event plan."),
                AgentPlan(id="ops", name="Operations Planner", role="Operations Specialist", personality="methodical", task="Create an operations checklist."),
                AgentPlan(id="qa", name="QA Verifier", role="Independent QA Verifier", personality="skeptical", task="Independently review the final deliverable."),
            ],
            subtasks=[SubtaskPlan(id="s1", title="Event operations", owner="Operations Planner")],
        )

    async def test_approved_task_runs_workers_and_independent_qa(self):
        plan = self.make_plan()
        await TASKS.create(plan)
        await TASKS.update(plan.task_id, execution_status="queued", approval_status="approved")
        lease = TaskLease(plan.task_id, "pipeline-test-owner", 1)
        await TASKS.activate_execution_lease(plan.task_id, lease)

        async def fake_completion(messages, *, temperature=0.2, json_mode=False):
            system = messages[0]["content"]
            if "independent QA authority" in system:
                return json.dumps({"decision": "approve", "summary": "The deliverable covers the mission and discloses limits.", "issues": [], "revision_instructions": "", "affected_agents": []})
            if "facilitating a transparent" in system:
                return "The team aligned on scope and identified the remaining assumptions."
            if "principal editor" in system:
                return "# Campus Event Plan\n\nA structured event plan with operations, risks, and next steps."
            return "Specialist findings: proposed schedule, operational tasks, budget assumptions, and open questions."

        with patch("app.execution.provider_is_configured", return_value=True), patch("app.execution.request_completion", side_effect=fake_completion):
            await execute_task(plan.task_id, lease=lease)

        result = await TASKS.get(plan.task_id)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.execution_status, "completed")
        self.assertEqual(result.qa_status, "passed")
        self.assertTrue(result.result_markdown)
        self.assertGreaterEqual(len(result.shared_memory), 4)
        self.assertTrue(any(event.kind == "team_deliberation" for event in result.events))
        self.assertTrue(any(message.kind == "qa_verdict" for message in result.messages))

    async def test_persistent_pipeline_writes_all_use_the_active_execution_fence(self):
        import tempfile
        from pathlib import Path
        from app.state import TaskStore

        with tempfile.TemporaryDirectory() as directory:
            url = "sqlite+aiosqlite:///" + str(Path(directory) / "execution-fenced.db")
            store = TaskStore(url)
            await store.initialize()
            plan = self.make_plan()
            await store.create(plan)
            await store.update(
                plan.task_id, execution_status="queued", approval_status="approved"
            )
            lease = TaskLease(plan.task_id, "persistent-pipeline-owner", 1)
            await store.activate_execution_lease(plan.task_id, lease)

            async def fake_completion(messages, *, temperature=0.2, json_mode=False):
                system = messages[0]["content"]
                if "independent QA authority" in system:
                    return json.dumps({
                        "decision": "approve", "summary": "The deliverable is complete.",
                        "issues": [], "revision_instructions": "", "affected_agents": [],
                    })
                if "facilitating a transparent" in system:
                    return "The team aligned on the delivery plan and captured remaining assumptions."
                if "principal editor" in system:
                    return "# Campus Event Plan\n\nThe event plan includes roles, timing, and risks."
                return "The specialist mapped the assigned workstream, decisions, and open questions."

            with patch("app.execution.TASKS", store), \
                 patch("app.execution.provider_is_configured", return_value=True), \
                 patch("app.execution.request_completion", side_effect=fake_completion):
                await execute_task(plan.task_id, lease=lease)

            result = await store.get(plan.task_id)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.execution_status, "completed")
            self.assertEqual(result.execution_fence, lease.generation)
            self.assertTrue(result.result_markdown)
            await store.close()

    async def test_qa_can_reject_then_request_a_revision(self):
        plan = self.make_plan()
        await TASKS.create(plan)
        await TASKS.update(plan.task_id, execution_status="queued", approval_status="approved")
        qa_calls = 0

        async def fake_completion(messages, *, temperature=0.2, json_mode=False):
            nonlocal qa_calls
            system = messages[0]["content"]
            if "independent QA authority" in system:
                qa_calls += 1
                decision = "revise" if qa_calls == 1 else "approve"
                return json.dumps({
                    "decision": decision,
                    "summary": "One missing contingency was found." if decision == "revise" else "The revision addressed the identified gap.",
                    "issues": ["Add an explicit venue contingency." ] if decision == "revise" else [],
                    "revision_instructions": "Add a venue fallback and explain the decision criteria." if decision == "revise" else "",
                    "affected_agents": ["Operations Planner"] if decision == "revise" else [],
                })
            if "facilitating a transparent" in system:
                return "Team review: operations needs a venue fallback."
            if "principal editor" in system:
                return "# Revised Event Plan\n\nThe plan includes a venue fallback."
            return "Operations output with venue fallback and decision criteria."

        with patch("app.execution.provider_is_configured", return_value=True), patch("app.execution.request_completion", side_effect=fake_completion):
            await execute_task(plan.task_id)

        result = await TASKS.get(plan.task_id)
        assert result is not None
        self.assertEqual(qa_calls, 2)
        self.assertEqual(result.execution_status, "completed")
        self.assertTrue(any(event.kind == "revision_cycle_started" for event in result.events))


    async def test_research_execution_collects_sources_and_preserves_citations(self):
        from app.schemas import ResearchSource

        plan = self.make_plan().model_copy(update={
            "instruction": "Research evidence-based approaches to evaluate AI agent teams.",
            "title": "AI agent evaluation research",
            "category": "research",
        })
        await TASKS.create(plan)
        await TASKS.update(plan.task_id, execution_status="queued", approval_status="approved")
        source = ResearchSource(
            source_id="S1",
            query="AI agent evaluation methods",
            title="A study of agent evaluation",
            url="https://research.example.org/agent-evaluation",
            snippet="The paper compares task completion, reliability and human review.",
            score=0.92,
        )

        async def fake_research(instruction, subtask_titles):
            return {
                "status": "completed",
                "notice": "Collected one source snippet from one bounded query.",
                "sources": [source],
                "credits": 1,
                "errors": [],
            }

        async def fake_completion(messages, *, temperature=0.2, json_mode=False):
            system = messages[0]["content"]
            user = messages[1]["content"]
            if "independent QA authority" in system:
                self.assertIn("[S1]", user)
                return json.dumps({
                    "decision": "approve", "summary": "The report cites the available source.",
                    "issues": [], "revision_instructions": "", "affected_agents": [],
                })
            if "facilitating a transparent" in system:
                return "The team compared evaluation quality and reliability. Source [S1] provides a relevant study."
            if "principal editor" in system:
                self.assertIn("RETRIEVED WEB SOURCE REGISTER", user)
                self.assertIn("https://research.example.org/agent-evaluation", user)
                return "# AI Agent Evaluation\n\nThe cited study compares reliability and task completion [S1]."
            self.assertIn("[S1]", user)
            self.assertIn("research.example.org", user)
            return "Findings: the cited study discusses reliability and task completion [S1]."

        with patch("app.execution.provider_is_configured", return_value=True), \
             patch("app.execution.collect_research", side_effect=fake_research), \
             patch("app.execution.request_completion", side_effect=fake_completion):
            await execute_task(plan.task_id)

        result = await TASKS.get(plan.task_id)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.execution_status, "completed")
        self.assertEqual(result.research_status, "completed")
        self.assertEqual(result.research_credits, 1)
        self.assertEqual(len(result.research_sources), 1)
        self.assertIn("[S1]", result.result_markdown or "")
        self.assertTrue(any(event.kind == "web_research_completed" for event in result.events))

    async def test_research_qa_rejects_missing_source_citations(self):
        from app.schemas import ResearchSource

        plan = self.make_plan().model_copy(update={
            "instruction": "Research evidence-based approaches to evaluate AI agent teams.",
            "title": "AI agent evaluation research",
            "category": "research",
        })
        await TASKS.create(plan)
        await TASKS.update(plan.task_id, execution_status="queued", approval_status="approved")
        source = ResearchSource(
            source_id="S1", query="AI agent evaluation",
            title="Agent evaluation study", url="https://research.example.org/study",
            snippet="The source describes evaluation criteria.",
        )

        async def fake_research(instruction, subtask_titles):
            return {"status": "completed", "notice": "One source collected.", "sources": [source], "credits": 1, "errors": []}

        async def fake_completion(messages, *, temperature=0.2, json_mode=False):
            system = messages[0]["content"]
            if "independent QA authority" in system:
                return json.dumps({
                    "decision": "approve", "summary": "Looks good.",
                    "issues": [], "revision_instructions": "", "affected_agents": [],
                })
            if "facilitating a transparent" in system:
                return "The team discussed the source."
            if "principal editor" in system:
                return "# Research Report\n\nA conclusion without any source citation."
            return "Specialist findings based on the provided search context."

        with patch("app.execution.provider_is_configured", return_value=True), \
             patch("app.execution.collect_research", side_effect=fake_research), \
             patch("app.execution.request_completion", side_effect=fake_completion):
            await execute_task(plan.task_id)

        result = await TASKS.get(plan.task_id)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.execution_status, "revision_required")
        self.assertEqual(result.qa_status, "rejected")
        self.assertIn("source evidence and disclosure integrity check", result.qa_feedback or "")


    async def test_demo_plan_cannot_be_executed_as_live_agents(self):
        plan = self.make_plan().model_copy(update={"execution_mode": "demo"})
        await TASKS.create(plan)
        await TASKS.update(plan.task_id, execution_status="queued", approval_status="approved")
        with patch("app.execution.provider_is_configured", return_value=True):
            await execute_task(plan.task_id)
        result = await TASKS.get(plan.task_id)
        assert result is not None
        self.assertEqual(result.execution_status, "failed")
        self.assertTrue(any(event.kind == "execution_blocked" for event in result.events))


if __name__ == "__main__":
    unittest.main()
