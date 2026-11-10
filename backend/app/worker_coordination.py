"""Distributed task ownership using Redis leases and fencing generations.

Redis leases prevent concurrent execution; the monotonically increasing
fencing generation lets the database reject writes from an expired owner.
"""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, TypeVar

T = TypeVar("T")


class LeaseLostError(RuntimeError):
    """Raised when a worker no longer owns the task lease."""


@dataclass(frozen=True, slots=True)
class TaskLease:
    task_id: str
    owner_token: str
    generation: int


_ACQUIRE_LEASE_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    return 0
end
local generation = redis.call('INCR', KEYS[2])
local value = ARGV[1] .. ':' .. tostring(generation)
redis.call('SET', KEYS[1], value, 'EX', ARGV[2])
return generation
"""

_RENEW_LEASE_SCRIPT = """
local expected = ARGV[1] .. ':' .. ARGV[2]
if redis.call('GET', KEYS[1]) ~= expected then
    return 0
end
return redis.call('EXPIRE', KEYS[1], ARGV[3])
"""

_RELEASE_LEASE_SCRIPT = """
local expected = ARGV[1] .. ':' .. ARGV[2]
if redis.call('GET', KEYS[1]) ~= expected then
    return 0
end
return redis.call('DEL', KEYS[1])
"""


class RedisTaskCoordinator:
    """Atomic Redis task leases with heartbeat supervision.

    The Redis client is injected so the coordinator can share the queue's
    connection pool and can be tested with an isolated Redis database.
    """

    def __init__(
        self,
        redis_client: Any,
        *,
        lease_ttl_seconds: int = 30,
        heartbeat_interval_seconds: float = 10,
        key_prefix: str = "workforce",
    ) -> None:
        if lease_ttl_seconds < 2:
            raise ValueError("lease_ttl_seconds must be at least 2.")
        if heartbeat_interval_seconds <= 0 or heartbeat_interval_seconds >= lease_ttl_seconds:
            raise ValueError("heartbeat_interval_seconds must be positive and shorter than the lease TTL.")
        self.redis = redis_client
        self.lease_ttl_seconds = int(lease_ttl_seconds)
        self.heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        self.key_prefix = key_prefix.rstrip(":")

    def _keys(self, task_id: str) -> tuple[str, str]:
        # The shared Redis hash tag keeps both keys in one cluster slot.
        tag = "{" + task_id + "}"
        return (
            f"{self.key_prefix}:lease:{tag}",
            f"{self.key_prefix}:fence:{tag}",
        )

    async def acquire(self, task_id: str) -> TaskLease | None:
        task_id = str(task_id).strip()
        if not task_id:
            raise ValueError("task_id must not be empty.")
        lease_key, fence_key = self._keys(task_id)
        owner_token = secrets.token_urlsafe(32)
        generation = await self.redis.eval(
            _ACQUIRE_LEASE_SCRIPT,
            2,
            lease_key,
            fence_key,
            owner_token,
            self.lease_ttl_seconds,
        )
        generation = int(generation or 0)
        if generation <= 0:
            return None
        return TaskLease(task_id=task_id, owner_token=owner_token, generation=generation)

    async def has_active_lease(self, task_id: str) -> bool:
        """Return whether Redis currently has a lease key for this task."""
        lease_key, _ = self._keys(str(task_id))
        return bool(await self.redis.exists(lease_key))

    async def renew(self, lease: TaskLease) -> bool:
        lease_key, _ = self._keys(lease.task_id)
        result = await self.redis.eval(
            _RENEW_LEASE_SCRIPT,
            1,
            lease_key,
            lease.owner_token,
            lease.generation,
            self.lease_ttl_seconds,
        )
        return bool(result)

    async def release(self, lease: TaskLease) -> bool:
        lease_key, _ = self._keys(lease.task_id)
        result = await self.redis.eval(
            _RELEASE_LEASE_SCRIPT,
            1,
            lease_key,
            lease.owner_token,
            lease.generation,
        )
        return bool(result)

    async def run_with_heartbeat(self, lease: TaskLease, operation: Awaitable[T]) -> T:
        """Run an operation while renewing its lease.

        Verify ownership before starting the operation. If that check or a
        later heartbeat fails, cancel and await the operation before raising
        LeaseLostError. Database fencing remains the final protection against
        an already-resumed stale coroutine.
        """
        try:
            owned = await self.renew(lease)
        except asyncio.CancelledError:
            if asyncio.iscoroutine(operation):
                operation.close()
            raise
        except Exception as exc:
            if asyncio.iscoroutine(operation):
                operation.close()
            raise LeaseLostError(
                f"Initial lease verification failed for task {lease.task_id} ({type(exc).__name__})."
            ) from exc
        if not owned:
            if asyncio.iscoroutine(operation):
                operation.close()
            raise LeaseLostError(f"Lease ownership was lost before task {lease.task_id} started.")

        operation_task = asyncio.ensure_future(operation)
        stop = asyncio.Event()

        async def heartbeat() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_interval_seconds)
                    return
                except TimeoutError:
                    pass
                try:
                    owned = await self.renew(lease)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise LeaseLostError(
                        f"Lease renewal failed for task {lease.task_id} ({type(exc).__name__})."
                    ) from exc
                if not owned:
                    raise LeaseLostError(f"Lease ownership was lost for task {lease.task_id}.")

        heartbeat_task = asyncio.create_task(heartbeat(), name=f"lease-heartbeat-{lease.task_id}")
        try:
            done, _ = await asyncio.wait(
                {operation_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                # A lease failure wins even if the operation completed in the
                # same event-loop turn; the database fence rejects late writes.
                await heartbeat_task
            return await operation_task
        except LeaseLostError:
            if not operation_task.done():
                operation_task.cancel()
            try:
                await operation_task
            except (asyncio.CancelledError, Exception):
                pass
            raise
        finally:
            stop.set()
            if not heartbeat_task.done():
                heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, LeaseLostError):
                pass
            if not operation_task.done():
                operation_task.cancel()
                try:
                    await operation_task
                except (asyncio.CancelledError, Exception):
                    pass
