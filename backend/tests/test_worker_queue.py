import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from redis.exceptions import ResponseError

from app.worker_queue import RedisStreamTaskQueue


class FakeRedis:
    def __init__(self, existing_group=False, delivery_count=1):
        self.existing_group = existing_group
        self.delivery_count = delivery_count
        self.groups = []
        self.entries = []
        self.acks = []
        self.dead_letters = []
        self.pinged = False
        self.closed = False

    async def ping(self):
        self.pinged = True
        return True

    async def xgroup_create(self, **kwargs):
        if self.existing_group:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        self.groups.append(kwargs)

    async def xadd(self, name, fields, **kwargs):
        fields = dict(fields)
        self.entries.append((name, fields, kwargs))
        if "dead" in name:
            self.dead_letters.append(fields)
        return "1000-0"

    async def xpending_range(self, name, group, min_id, max_id, count):
        return [{"times_delivered": self.delivery_count}]

    async def xack(self, name, group, message_id):
        self.acks.append(message_id)
        return 1

    async def xdel(self, name, message_id):
        return 1

    async def xautoclaim(self, *args, **kwargs):
        return ("0-0", [], [])

    async def xreadgroup(self, *args, **kwargs):
        return []

    async def xlen(self, name):
        return 1

    async def xpending(self, name, group):
        return {"pending": 0}

    async def aclose(self):
        self.closed = True


class RedisQueueUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_creates_stream_group(self):
        redis = FakeRedis()
        queue = RedisStreamTaskQueue(redis_client=redis)
        await queue.initialize()
        self.assertTrue(redis.pinged)
        self.assertEqual(redis.groups[0]["name"], "workforce:approved-tasks")
        self.assertTrue(redis.groups[0]["mkstream"])

    async def test_initialize_accepts_existing_consumer_group(self):
        redis = FakeRedis(existing_group=True)
        queue = RedisStreamTaskQueue(redis_client=redis)
        await queue.initialize()
        self.assertTrue(redis.pinged)

    async def test_enqueue_writes_task_id_to_stream(self):
        redis = FakeRedis()
        queue = RedisStreamTaskQueue(redis_client=redis)
        message_id = await queue.enqueue("task-123")
        self.assertEqual(message_id, "1000-0")
        self.assertEqual(redis.entries[0][1]["task_id"], "task-123")

    async def test_process_acknowledges_completed_task(self):
        redis = FakeRedis()
        queue = RedisStreamTaskQueue(redis_client=redis)
        task = SimpleNamespace(execution_status="queued")
        fake_store = SimpleNamespace(
            get=AsyncMock(return_value=task),
            add_event=AsyncMock(),
        )

        async def execute(task_id):
            task.execution_status = "completed"

        with patch("app.state.TASKS", fake_store), patch("app.execution.execute_task", new=execute):
            await queue._process_message("1000-0", {"task_id": "task-123"})

        self.assertEqual(redis.acks, ["1000-0"])
        fake_store.add_event.assert_awaited_once()

    async def test_missing_task_is_dead_lettered_and_acked(self):
        redis = FakeRedis()
        queue = RedisStreamTaskQueue(redis_client=redis)
        fake_store = SimpleNamespace(get=AsyncMock(return_value=None))
        with patch("app.state.TASKS", fake_store):
            await queue._process_message("1000-0", {"task_id": "missing-task"})
        self.assertEqual(redis.acks, ["1000-0"])
        self.assertEqual(redis.dead_letters[0]["task_id"], "missing-task")

    async def test_overdelivered_message_moves_to_dead_letter_stream(self):
        redis = FakeRedis(delivery_count=4)
        queue = RedisStreamTaskQueue(redis_client=redis)
        fake_store = SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(execution_status="queued")),
            update=AsyncMock(),
            add_event=AsyncMock(),
        )
        with patch("app.state.TASKS", fake_store):
            await queue._process_message("1000-0", {"task_id": "task-123"})
        fake_store.update.assert_awaited_once_with("task-123", execution_status="failed", qa_status="rejected")
        self.assertEqual(redis.dead_letters[0]["task_id"], "task-123")
        self.assertEqual(redis.acks, ["1000-0"])

    async def test_status_reports_stream_and_pending_counts(self):
        redis = FakeRedis()
        queue = RedisStreamTaskQueue(redis_client=redis)
        result = await queue.status()
        self.assertEqual(result["backend"], "redis_streams")
        self.assertEqual(result["stream_length"], 1)
        self.assertEqual(result["pending_messages"], 0)

    async def test_close_closes_redis_client(self):
        redis = FakeRedis()
        queue = RedisStreamTaskQueue(redis_client=redis)
        await queue.close()
        self.assertTrue(redis.closed)


if __name__ == "__main__":
    unittest.main()
