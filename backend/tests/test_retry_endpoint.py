import unittest
import uuid
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException
from starlette.requests import Request

from app import main
from app.schemas import AgentPlan, PlanResponse, SubtaskPlan
from app.state import TASKS


class RetryEndpointTests(unittest.IsolatedAsyncioTestCase):
    def make_request(self) -> Request:
        return Request({
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/tasks/test/retry",
            "raw_path": b"/api/tasks/test/retry",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        })

    def make_failed_task(self) -> PlanResponse:
        return PlanResponse(
            task_id=str(uuid.uuid4()),
            instruction="Prepare a small event plan with roles and an operations checklist.",
            title="Retryable event plan",
            category="event_planning",
            execution_mode="provider_planned",
            notice="Provider-backed plan.",
            agents=[
                AgentPlan(id="director", name="Project Director", role="Project Manager", personality="focused", task="Coordinate the work.", status="completed"),
                AgentPlan(id="ops", name="Operations Planner", role="Operations Specialist", personality="methodical", task="Prepare an operations checklist.", status="failed"),
                AgentPlan(id="qa", name="QA Verifier", role="Independent QA Verifier", personality="careful", task="Review the deliverable.", status="waiting_for_review"),
            ],
            subtasks=[
                SubtaskPlan(id="s1", title="Project coordination", owner="Project Director", status="completed", output="Saved director output"),
                SubtaskPlan(id="s2", title="Operations checklist", owner="Operations Planner", status="failed", output="Rate limited"),
            ],
            execution_status="failed",
            approval_status="approved",
            qa_status="pending_execution",
        )

    async def test_retry_resets_only_failed_work_and_preserves_completed_checkpoint(self):
        task = self.make_failed_task()
        await TASKS.create(task)
        background = BackgroundTasks()
        with patch("app.main.provider_availability", return_value={"configured": True, "available_provider_count": 1}), patch("app.main.TASK_QUEUE", None):
            result = await main.retry_task(task.task_id, background, self.make_request())
        self.assertEqual(result.execution_status, "queued")
        self.assertEqual(result.approval_status, "approved")
        self.assertEqual(result.qa_status, "pending_execution")
        agents = {item.id: item for item in result.agents}
        self.assertEqual(agents["director"].status, "completed")
        self.assertEqual(agents["ops"].status, "planned")
        subtasks = {item.id: item for item in result.subtasks}
        self.assertEqual(subtasks["s1"].status, "completed")
        self.assertEqual(subtasks["s1"].output, "Saved director output")
        self.assertEqual(subtasks["s2"].status, "planned")
        self.assertIsNone(subtasks["s2"].output)
        self.assertEqual(len(background.tasks), 1)

    async def test_retry_resets_provider_blocked_agents_and_subtasks(self):
        task = self.make_failed_task().model_copy(update={
            "agents": [
                self.make_failed_task().agents[0],
                AgentPlan(id="ops", name="Operations Planner", role="Operations Specialist", personality="methodical", task="Prepare checklist.", status="blocked_by_provider"),
                self.make_failed_task().agents[2],
            ],
            "subtasks": [
                SubtaskPlan(id="s1", title="Project coordination", owner="Project Director", status="completed", output="Saved director output"),
                SubtaskPlan(id="s2", title="Operations checklist", owner="Operations Planner", status="blocked_by_provider", output="Gemini daily quota exhausted"),
            ],
        })
        await TASKS.create(task)
        with patch("app.main.provider_availability", return_value={"configured": True, "available_provider_count": 1}), patch("app.main.TASK_QUEUE", None):
            result = await main.retry_task(task.task_id, BackgroundTasks(), self.make_request())
        agents = {item.id: item for item in result.agents}
        subtasks = {item.id: item for item in result.subtasks}
        self.assertEqual(agents["ops"].status, "planned")
        self.assertEqual(subtasks["s2"].status, "planned")
        self.assertIsNone(subtasks["s2"].output)

    async def test_retry_is_blocked_when_all_configured_providers_are_cooling_down(self):
        task = self.make_failed_task()
        await TASKS.create(task)
        with patch("app.main.provider_availability", return_value={"configured": True, "available_provider_count": 0, "retry_after_seconds": 45, "fallback_configured": False}):
            with self.assertRaises(HTTPException) as raised:
                await main.retry_task(task.task_id, BackgroundTasks(), self.make_request())
        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("45 seconds", raised.exception.detail)
        current = await TASKS.get(task.task_id)
        self.assertEqual(current.execution_status, "failed")


if __name__ == "__main__":
    unittest.main()
