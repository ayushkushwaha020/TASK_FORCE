import asyncio
import unittest

from app.worker_coordination import (
    LeaseLostError,
    RedisTaskCoordinator,
    _ACQUIRE_LEASE_SCRIPT,
    _RENEW_LEASE_SCRIPT,
    _RELEASE_LEASE_SCRIPT,
)


class FakeRedis:
    def __init__(self):
        self.leases = {}
        self.fences = {}
        self.deny_renew = False

    async def exists(self, key):
        return int(key in self.leases)

    async def eval(self, script, number_of_keys, *args):
        keys = args[:number_of_keys]
        values = args[number_of_keys:]
        if script == _ACQUIRE_LEASE_SCRIPT:
            lease_key, fence_key = keys
            owner_token, ttl = values
            if lease_key in self.leases:
                return 0
            generation = self.fences.get(fence_key, 0) + 1
            self.fences[fence_key] = generation
            self.leases[lease_key] = (owner_token, generation, int(ttl))
            return generation
        if script == _RENEW_LEASE_SCRIPT:
            (lease_key,) = keys
            owner_token, generation, ttl = values
            if self.deny_renew or self.leases.get(lease_key, ())[:2] != (owner_token, int(generation)):
                return 0
            self.leases[lease_key] = (owner_token, int(generation), int(ttl))
            return 1
        if script == _RELEASE_LEASE_SCRIPT:
            (lease_key,) = keys
            owner_token, generation = values
            if self.leases.get(lease_key, ())[:2] != (owner_token, int(generation)):
                return 0
            del self.leases[lease_key]
            return 1
        raise AssertionError("Unexpected Redis script.")


class RedisTaskCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.coordinator = RedisTaskCoordinator(
            self.redis,
            lease_ttl_seconds=2,
            heartbeat_interval_seconds=0.02,
        )

    async def test_acquire_returns_first_fencing_generation(self):
        lease = await self.coordinator.acquire("task-123")
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertEqual(lease.task_id, "task-123")
        self.assertEqual(lease.generation, 1)
        self.assertTrue(lease.owner_token)

    async def test_second_worker_cannot_acquire_an_occupied_lease(self):
        first = await self.coordinator.acquire("task-123")
        second = await self.coordinator.acquire("task-123")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    async def test_only_matching_owner_can_renew_or_release(self):
        lease = await self.coordinator.acquire("task-123")
        assert lease is not None
        from app.worker_coordination import TaskLease
        stranger = TaskLease("task-123", "not-the-owner", lease.generation)
        self.assertFalse(await self.coordinator.renew(stranger))
        self.assertFalse(await self.coordinator.release(stranger))
        self.assertTrue(await self.coordinator.renew(lease))
        self.assertTrue(await self.coordinator.release(lease))

    async def test_reacquire_after_release_gets_a_higher_generation(self):
        first = await self.coordinator.acquire("task-123")
        assert first is not None
        self.assertTrue(await self.coordinator.release(first))
        second = await self.coordinator.acquire("task-123")
        assert second is not None
        self.assertGreater(second.generation, first.generation)


    async def test_has_active_lease_reports_current_redis_key(self):
        lease = await self.coordinator.acquire("task-123")
        assert lease is not None
        self.assertTrue(await self.coordinator.has_active_lease("task-123"))
        self.assertTrue(await self.coordinator.release(lease))
        self.assertFalse(await self.coordinator.has_active_lease("task-123"))

    async def test_heartbeat_loss_cancels_operation_and_raises(self):
        lease = await self.coordinator.acquire("task-123")
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

        running = asyncio.create_task(self.coordinator.run_with_heartbeat(lease, operation()))
        await asyncio.wait_for(started.wait(), timeout=0.5)
        self.redis.deny_renew = True
        with self.assertRaises(LeaseLostError):
            await asyncio.wait_for(running, timeout=0.5)
        self.assertTrue(cancelled.is_set())


    async def test_heartbeat_verifies_lease_before_starting_operation(self):
        lease = await self.coordinator.acquire("task-123")
        assert lease is not None
        self.redis.deny_renew = True
        started = asyncio.Event()

        async def operation():
            started.set()

        with self.assertRaises(LeaseLostError):
            await self.coordinator.run_with_heartbeat(lease, operation())
        self.assertFalse(started.is_set())

    async def test_heartbeat_stops_cleanly_when_operation_completes(self):
        lease = await self.coordinator.acquire("task-123")
        assert lease is not None
        result = await self.coordinator.run_with_heartbeat(lease, asyncio.sleep(0, result="done"))
        self.assertEqual(result, "done")


if __name__ == "__main__":
    unittest.main()
