import asyncio
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.schemas import AgentPlan, PlanResponse
from app.state import TaskStore
from app.task_events import (
    RedisTaskEventBus,
    TASK_EVENT_CHANNEL,
    TaskChange,
    TaskEventHub,
    decode_task_change,
)

REDIS_URL = os.getenv("WORKFORCE_TEST_REDIS_URL", "").strip()


def make_plan() -> PlanResponse:
    return PlanResponse(
        task_id=str(uuid.uuid4()),
        instruction="Create a campus event delivery plan.",
        title="Campus event plan",
        category="event_planning",
        execution_mode="provider_planned",
        notice="Waiting for approval.",
        agents=[
            AgentPlan(id="director", name="Project Director", role="Project Manager",
                      personality="organised", task="Coordinate the work."),
            AgentPlan(id="qa", name="Independent QA", role="QA Verifier",
                      personality="skeptical", task="Review the deliverable."),
        ],
        subtasks=[],
    )


class TaskEventHubTests(unittest.IsolatedAsyncioTestCase):
    def test_payload_allowlist_and_validation(self):
        valid = json.dumps({"task_id": "task-1", "version": 7, "event_cursor": "evt-9"})
        self.assertEqual(decode_task_change(valid), TaskChange("task-1", 7, "evt-9"))
        self.assertIsNone(decode_task_change('{"task_id":"task-1","version":true,"event_cursor":null}'))
        self.assertIsNone(decode_task_change(valid[:-1] + ',"prompt":"secret"}'))
        self.assertIsNone(decode_task_change("not-json"))
        self.assertIsNone(decode_task_change("x" * 2048))

    def test_listener_isolation_and_bounded_coalescing(self):
        hub = TaskEventHub()
        first = hub.subscribe("task-a")
        second = hub.subscribe("task-a")
        other = hub.subscribe("task-b")
        hub.notify(TaskChange("task-a", 2, "event-2"))
        hub.notify(TaskChange("task-a", 1, "late-event-1"))
        self.assertEqual(first.qsize(), 1)
        self.assertEqual(first.get_nowait(), TaskChange("task-a", 2, "event-2"))
        self.assertEqual(second.get_nowait(), TaskChange("task-a", 2, "event-2"))
        self.assertTrue(other.empty())
        hub.unsubscribe("task-a", first)
        hub.unsubscribe("task-a", second)
        self.assertEqual(hub.listener_count, 1)


class RedisTaskEventBusUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_contains_only_compact_hint_and_failure_is_nonfatal(self):
        redis = type("FakeRedis", (), {})()
        redis.publish = AsyncMock(return_value=1)
        hub = TaskEventHub()
        bus = RedisTaskEventBus(redis, hub)
        listener = hub.subscribe("task-1")
        change = TaskChange("task-1", 5, "evt-5")
        bus.notify(change)
        self.assertEqual(listener.get_nowait(), change)
        await bus.drain()
        raw = redis.publish.await_args.args[1]
        self.assertEqual(set(json.loads(raw)), {"task_id", "version", "event_cursor"})
        self.assertEqual(redis.publish.await_args.args[0], TASK_EVENT_CHANNEL)

        redis.publish.side_effect = OSError("Redis unavailable")
        bus.notify(TaskChange("task-1", 6, "evt-6"))
        await bus.drain()
        self.assertEqual(bus.publish_failures, 1)
        self.assertEqual(listener.get_nowait(), TaskChange("task-1", 6, "evt-6"))
        bus.subscriber_connected = True
        self.assertEqual(bus.status()["status"], "degraded")
        redis.publish.side_effect = None
        bus.notify(TaskChange("task-1", 7, "evt-7"))
        await bus.drain()
        self.assertEqual(bus.status()["status"], "connected")
        self.assertEqual(bus.status()["publish_failures"], 1)
        await bus.close()

    async def test_subscriber_reconnects_after_transient_disconnect(self):
        original_sleep = asyncio.sleep
        instances = []

        class FakePubSub:
            def __init__(self, index):
                self.index = index
                self.closed = False

            async def subscribe(self, _channel):
                return None

            async def get_message(self, **_kwargs):
                if self.index == 1:
                    raise OSError("connection dropped")
                await asyncio.Event().wait()

            async def aclose(self):
                self.closed = True

        class FakeRedis:
            def pubsub(self):
                item = FakePubSub(len(instances) + 1)
                instances.append(item)
                return item

        async def fast_retry(_delay):
            return None

        bus = RedisTaskEventBus(FakeRedis(), TaskEventHub())
        with patch("app.task_events.asyncio.sleep", new=fast_retry):
            running = asyncio.create_task(bus.run())
            try:
                for _ in range(100):
                    if len(instances) >= 2:
                        break
                    await original_sleep(0.005)
                self.assertGreaterEqual(len(instances), 2)
                self.assertTrue(bus.subscriber_connected)
                self.assertIsNone(bus.last_error)
            finally:
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
        await bus.close()
        self.assertTrue(all(item.closed for item in instances))


@unittest.skipUnless(REDIS_URL, "WORKFORCE_TEST_REDIS_URL is not configured")
class RedisTaskEventBusIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_api_instances_receive_cross_instance_hint(self):
        from redis.asyncio import Redis

        suffix = uuid.uuid4().hex
        channel = f"{TASK_EVENT_CHANNEL}:{suffix}"
        redis_a = Redis.from_url(REDIS_URL, decode_responses=True)
        redis_b = Redis.from_url(REDIS_URL, decode_responses=True)
        hub_a, hub_b = TaskEventHub(), TaskEventHub()
        bus_a = RedisTaskEventBus(redis_a, hub_a, channel=channel)
        bus_b = RedisTaskEventBus(redis_b, hub_b, channel=channel)
        listener_a = hub_a.subscribe("task-cross")
        listener_b = hub_b.subscribe("task-cross")
        subscriber = asyncio.create_task(bus_b.run())
        try:
            for _ in range(100):
                if bus_b.subscriber_connected:
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(bus_b.subscriber_connected)
            change = TaskChange("task-cross", 42, "event-cross")
            bus_a.notify(change)
            await asyncio.wait_for(listener_a.get(), timeout=2)
            remote = await asyncio.wait_for(listener_b.get(), timeout=2)
            self.assertEqual(remote, change)
        finally:
            subscriber.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await subscriber
            await bus_a.close()
            await bus_b.close()
            await redis_a.aclose()
            await redis_b.aclose()

    async def test_committed_store_mutation_wakes_second_store_listener(self):
        from redis.asyncio import Redis

        suffix = uuid.uuid4().hex
        channel = f"{TASK_EVENT_CHANNEL}:{suffix}"
        redis_publisher = Redis.from_url(REDIS_URL, decode_responses=True)
        redis_subscriber = Redis.from_url(REDIS_URL, decode_responses=True)
        publisher_hub, subscriber_hub = TaskEventHub(), TaskEventHub()
        publisher = RedisTaskEventBus(redis_publisher, publisher_hub, channel=channel)
        subscriber = RedisTaskEventBus(redis_subscriber, subscriber_hub, channel=channel)
        with tempfile.TemporaryDirectory() as directory:
            database_url = "sqlite+aiosqlite:///" + str(Path(directory) / "shared-tasks.db")
            store_a, store_b = TaskStore(database_url), TaskStore(database_url)
            task_id = None
            listener = None
            subscriber_task = None
            try:
                await store_a.initialize()
                await store_b.initialize()
                task = await store_a.create(make_plan())
                task_id = task.task_id
                store_a.set_event_notifier(publisher.notify)
                listener = subscriber_hub.subscribe(task_id)
                subscriber_task = asyncio.create_task(subscriber.run())
                for _ in range(100):
                    if subscriber.subscriber_connected:
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(subscriber.subscriber_connected)

                await store_a.add_event(
                    task_id, "Project Director", "cross_instance_commit",
                    "Committed by API instance A.",
                )
                hint = await asyncio.wait_for(listener.get(), timeout=2)
                canonical = await store_b.get(task_id)
                self.assertIsNotNone(canonical)
                assert canonical is not None
                self.assertEqual(canonical.version, hint.version)
                self.assertEqual(canonical.events[-1].id, hint.event_cursor)
                self.assertEqual(canonical.events[-1].kind, "cross_instance_commit")
            finally:
                if subscriber_task is not None:
                    subscriber_task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await subscriber_task
                if listener is not None and task_id is not None:
                    subscriber_hub.unsubscribe(task_id, listener)
                await publisher.close()
                await subscriber.close()
                await store_a.close()
                await store_b.close()
                await redis_publisher.aclose()
                await redis_subscriber.aclose()


if __name__ == "__main__":
    unittest.main()
