import json
import unittest
import uuid
from unittest.mock import patch

from app.execution import execute_task
from app.schemas import AgentPlan, PlanResponse, SubtaskPlan
from app.state import TASKS


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
            await execute_task(plan.task_id)

        result = await TASKS.get(plan.task_id)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.execution_status, "completed")
        self.assertEqual(result.qa_status, "passed")
        self.assertTrue(result.result_markdown)
        self.assertGreaterEqual(len(result.shared_memory), 4)
        self.assertTrue(any(event.kind == "team_deliberation" for event in result.events))
        self.assertTrue(any(message.kind == "qa_verdict" for message in result.messages))

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
