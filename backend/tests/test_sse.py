import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.main import _task_event_generator
from app.schemas import TaskEvent
from app.task_events import TaskChange


class TaskEventStreamTests(unittest.IsolatedAsyncioTestCase):
    def make_task(self, status="completed", event_count=2, updated_at="2026-11-09T18:00:00Z"):
        events = [
            TaskEvent(id=f"event-{index}", actor="Workforce", kind="started", message=f"Event {index}")
            for index in range(1, event_count + 1)
        ]
        return SimpleNamespace(
            task_id="task-123",
            events=events,
            updated_at=updated_at,
            execution_status=status,
            approval_status="approved",
            qa_status="passed",
        )

    def make_request(self, headers=None):
        return SimpleNamespace(
            headers=headers or {},
            query_params={},
            is_disconnected=AsyncMock(return_value=False),
        )

    async def test_reconnect_streams_events_after_last_event_id(self):
        task = self.make_task()
        fake_store = SimpleNamespace(get=AsyncMock(return_value=task))
        with patch("app.main.TASKS", fake_store):
            chunks = [chunk async for chunk in _task_event_generator(
                "task-123", self.make_request({"last-event-id": "event-1"})
            )]
        output = "".join(chunks)
        self.assertIn("id: event-2\nevent: task_event\n", output)
        self.assertIn('"execution_status": "completed"', output)
        self.assertNotIn("id: event-1\nevent: task_event", output)
        self.assertEqual(fake_store.get.await_count, 1)

    async def test_missing_task_emits_stream_error_and_unsubscribes(self):
        fake_store = SimpleNamespace(get=AsyncMock(return_value=None))
        from app.main import TASK_EVENT_HUB
        before = TASK_EVENT_HUB.listener_count
        with patch("app.main.TASKS", fake_store):
            chunks = [chunk async for chunk in _task_event_generator("missing", self.make_request())]
        self.assertIn("event: task_error", "".join(chunks))
        self.assertEqual(TASK_EVENT_HUB.listener_count, before)

    async def test_pubsub_wakeup_rereads_canonical_snapshot(self):
        initial = self.make_task(status="running")
        terminal = self.make_task(status="completed", event_count=3, updated_at="2026-11-10T10:00:00Z")
        terminal.events[-1] = TaskEvent(id="event-final", actor="QA", kind="approved", message="QA approved")
        calls = 0

        async def read_task(_task_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                from app.main import TASK_EVENT_HUB
                asyncio.get_running_loop().call_later(
                    0.01,
                    TASK_EVENT_HUB.notify,
                    TaskChange("task-123", 9, "event-final"),
                )
                return initial
            return terminal

        fake_store = SimpleNamespace(get=AsyncMock(side_effect=read_task))
        with patch("app.main.TASKS", fake_store):
            chunks = [chunk async for chunk in _task_event_generator("task-123", self.make_request())]
        output = "".join(chunks)
        self.assertIn("id: event-final\nevent: task_event\n", output)
        self.assertIn('"execution_status": "completed"', output)
        self.assertEqual(fake_store.get.await_count, 2)

    async def test_dropped_notification_is_reconciled_after_interval(self):
        initial = self.make_task(status="running")
        terminal = self.make_task(status="completed", event_count=3)
        terminal.events[-1] = TaskEvent(id="event-recovered", actor="QA", kind="approved", message="Recovered")
        fake_store = SimpleNamespace(get=AsyncMock(side_effect=[initial, initial, terminal]))
        with (
            patch("app.main.TASKS", fake_store),
            patch("app.main.SSE_RECONCILE_SECONDS", 0.01),
        ):
            chunks = [chunk async for chunk in _task_event_generator("task-123", self.make_request())]
        output = "".join(chunks)
        self.assertIn("id: event-recovered\nevent: task_event\n", output)
        self.assertIn('"execution_status": "completed"', output)
        self.assertEqual(fake_store.get.await_count, 3)


if __name__ == "__main__":
    unittest.main()
