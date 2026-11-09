import tempfile
import unittest
import uuid
from pathlib import Path

from app.schemas import AgentPlan, PlanResponse, SubtaskPlan
from app.state import TaskStore


def make_plan() -> PlanResponse:
    return PlanResponse(
        task_id=str(uuid.uuid4()),
        instruction="Prepare a clear campus technology event work plan.",
        title="Campus technology event plan",
        category="event_planning",
        execution_mode="provider_planned",
        notice="Awaiting human approval.",
        agents=[
            AgentPlan(id="director", name="Project Director", role="Project Manager",
                      personality="organised", task="Coordinate the work."),
            AgentPlan(id="qa", name="Independent QA", role="QA Verifier",
                      personality="skeptical", task="Review the deliverable."),
        ],
        subtasks=[SubtaskPlan(id="ops", title="Event operations", owner="Project Director")],
    )


class PersistentTaskStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_and_updates_survive_store_recreation(self):
        with tempfile.TemporaryDirectory() as directory:
            url = "sqlite+aiosqlite:///" + str(Path(directory) / "tasks.db")
            first = TaskStore(url)
            await first.initialize()
            plan = await first.create(make_plan())
            await first.update(plan.task_id, approval_status="approved", execution_status="queued")
            await first.add_memory(plan.task_id, "Project Director", "Scope", "Venue and schedule plan.")
            await first.close()

            second = TaskStore(url)
            await second.initialize()
            restored = await second.get(plan.task_id)
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.approval_status, "approved")
            self.assertEqual(restored.execution_status, "failed")
            self.assertEqual(len(restored.shared_memory), 1)
            self.assertTrue(any(event.kind == "execution_interrupted" for event in restored.events))
            await second.close()

    async def test_inflight_task_is_requeued_when_redis_queue_is_configured(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"REDIS_URL": "rediss://default:secret@example.upstash.io:6379"}):
            url = "sqlite+aiosqlite:///" + str(Path(directory) / "tasks.db")
            first = TaskStore(url)
            await first.initialize()
            plan = await first.create(make_plan())
            await first.update(plan.task_id, approval_status="approved", execution_status="running")
            await first.close()

            second = TaskStore(url)
            await second.initialize()
            restored = await second.get(plan.task_id)
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.execution_status, "queued")
            self.assertTrue(any(event.kind == "execution_requeued" for event in restored.events))
            await second.close()

    async def test_redis_queue_requires_persistent_task_storage(self):
        with patch.dict("os.environ", {"REDIS_URL": "rediss://default:secret@example.upstash.io:6379"}):
            with self.assertRaises(RuntimeError):
                TaskStore(None)

    async def test_waiting_for_approval_survives_restart_without_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            url = "sqlite+aiosqlite:///" + str(Path(directory) / "tasks.db")
            first = TaskStore(url)
            await first.initialize()
            plan = await first.create(make_plan())
            await first.close()

            second = TaskStore(url)
            await second.initialize()
            restored = await second.get(plan.task_id)
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.execution_status, "awaiting_approval")
            self.assertFalse(any(event.kind == "execution_interrupted" for event in restored.events))
            await second.close()


if __name__ == "__main__":
    unittest.main()
