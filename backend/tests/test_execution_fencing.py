"""Opt-in integration tests against an isolated real Redis instance.

Set WORKFORCE_TEST_REDIS_URL to a disposable Redis database (CI uses a
dedicated service container and DB 15). Tests use unique key prefixes and
never FLUSHDB or FLUSHALL.
"""
import asyncio
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from redis.asyncio import Redis

from app.database import StaleExecutionFenceError
from app.schemas import AgentPlan, PlanResponse
from app.state import TaskStore
from app.worker_coordination import LeaseLostError, RedisTaskCoordinator


REDIS_URL = os.getenv("WORKFORCE_TEST_REDIS_URL", "").strip()


def make_plan() -> PlanResponse:
    return PlanResponse(
        task_id=str(uuid.uuid4()),
        instruction="Prepare a campus event delivery plan.",
        title="Campus event plan",
        category="event_planning",
        execution_mode="provider_planned",
        notice="Awaiting approval.",
        agents=[
            AgentPlan(
                id="director", name="Project Director", role="Project Manager",
                personality="organised", task="Coordinate the work.",
            ),
            AgentPlan(
                id="qa", name="Independent QA", role="QA Verifier",
                personality="skeptical", task="Review the deliverable.",
            ),
        ],
        subtasks=[],
    )


@unittest.skipUnless(REDIS_URL, "WORKFORCE_TEST_REDIS_URL is not configured")
class RealRedisExecutionFencingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = Redis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=3, socket_timeout=5,
        )
        await self.redis.ping()
        self.prefix = "workforce-integration-" + uuid.uuid4().hex
        self.coordinator = RedisTaskCoordinator(
            self.redis, lease_ttl_seconds=5,
            heartbeat_interval_seconds=0.1, key_prefix=self.prefix,
        )

    async def asyncTearDown(self):
        await self.redis.aclose()

    async def test_two_consumers_cannot_acquire_same_task_concurrently(self):
        other = RedisTaskCoordinator(
            self.redis, lease_ttl_seconds=5,
            heartbeat_interval_seconds=0.1, key_prefix=self.prefix,
        )
        first, second = await asyncio.gather(
            self.coordinator.acquire("shared-task"),
            other.acquire("shared-task"),
        )
        acquired = [lease for lease in (first, second) if lease is not None]
        self.assertEqual(len(acquired), 1)
        self.assertTrue(await self.coordinator.has_active_lease("shared-task"))
        self.assertTrue(await self.coordinator.release(acquired[0]))

    async def test_expired_lease_can_be_reacquired_with_higher_generation(self):
        coordinator = RedisTaskCoordinator(
            self.redis, lease_ttl_seconds=2,
            heartbeat_interval_seconds=0.1, key_prefix=self.prefix,
        )
        first = await coordinator.acquire("expiry-task")
        self.assertIsNotNone(first)
        assert first is not None
        await asyncio.sleep(2.25)
        second = await coordinator.acquire("expiry-task")
        self.assertIsNotNone(second)
        assert second is not None
        self.assertGreater(second.generation, first.generation)
        self.assertFalse(await coordinator.release(first))
        self.assertTrue(await coordinator.release(second))

    async def test_heartbeat_cancels_operation_after_redis_lease_is_lost(self):
        lease = await self.coordinator.acquire("heartbeat-task")
        self.assertIsNotNone(lease)
        assert lease is not None
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def operation():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        running = asyncio.create_task(
            self.coordinator.run_with_heartbeat(lease, operation())
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        lease_key, _ = self.coordinator._keys("heartbeat-task")
        await self.redis.delete(lease_key)
        with self.assertRaises(LeaseLostError):
            await asyncio.wait_for(running, timeout=2)
        self.assertTrue(cancelled.is_set())

    async def test_old_generation_cannot_write_after_lease_rollover(self):
        with tempfile.TemporaryDirectory() as directory:
            url = "sqlite+aiosqlite:///" + str(Path(directory) / "fencing.db")
            store = TaskStore(url)
            await store.initialize()
            plan = await store.create(make_plan())
            first = await self.coordinator.acquire(plan.task_id)
            self.assertIsNotNone(first)
            assert first is not None
            await store.activate_execution_lease(plan.task_id, first)
            await store.update(
                plan.task_id, lease=first, execution_status="running"
            )

            await self.coordinator.release(first)
            second = await self.coordinator.acquire(plan.task_id)
            self.assertIsNotNone(second)
            assert second is not None
            self.assertGreater(second.generation, first.generation)
            await store.activate_execution_lease(plan.task_id, second)

            with self.assertRaises(StaleExecutionFenceError):
                await store.update(
                    plan.task_id, lease=first, execution_status="failed"
                )
            latest = await store.get(plan.task_id)
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest.execution_fence, second.generation)
            self.assertEqual(latest.execution_status, "running")
            await self.coordinator.release(second)
            await store.close()


if __name__ == "__main__":
    unittest.main()
