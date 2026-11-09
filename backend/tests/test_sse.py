import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.main import _task_event_generator
from app.schemas import TaskEvent


class TaskEventStreamTests(unittest.IsolatedAsyncioTestCase):
    def make_task(self, status="completed"):
        return SimpleNamespace(
            task_id="task-123",
            events=[
                TaskEvent(id="event-1", actor="Workforce", kind="started", message="Execution started."),
                TaskEvent(id="event-2", actor="Scout", kind="agent_completed", message="Research workstream completed."),
            ],
            updated_at="2026-11-09T18:00:00Z",
            execution_status=status,
            approval_status="approved",
            qa_status="passed",
        )

    async def test_reconnect_streams_events_after_last_event_id(self):
        task = self.make_task()
        fake_store = SimpleNamespace(get=AsyncMock(return_value=task))
        request = SimpleNamespace(
            headers={"last-event-id": "event-1"},
            query_params={},
            is_disconnected=AsyncMock(return_value=False),
        )
        with patch("app.main.TASKS", fake_store):
            chunks = [chunk async for chunk in _task_event_generator("task-123", request)]
        output = "".join(chunks)
        self.assertIn("id: event-2\nevent: task_event\n", output)
        self.assertIn('"execution_status": "completed"', output)
        self.assertNotIn("id: event-1\nevent: task_event", output)

    async def test_missing_task_emits_stream_error(self):
        fake_store = SimpleNamespace(get=AsyncMock(return_value=None))
        request = SimpleNamespace(headers={}, query_params={}, is_disconnected=AsyncMock(return_value=False))
        with patch("app.main.TASKS", fake_store):
            chunks = [chunk async for chunk in _task_event_generator("missing", request)]
        self.assertIn("event: task_error", "".join(chunks))


if __name__ == "__main__":
    unittest.main()
