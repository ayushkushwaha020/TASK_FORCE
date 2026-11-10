"""Cross-instance task-change hints for SSE consumers.

PostgreSQL remains canonical. Redis Pub/Sub messages contain only a task ID,
committed version, and latest event cursor; clients always reread TaskStore.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

TASK_EVENT_CHANNEL = "workforce:task-events"
MAX_TASK_ID_LENGTH = 64
MAX_EVENT_CURSOR_LENGTH = 128
MAX_EVENT_HINT_BYTES = 1024


@dataclass(frozen=True, slots=True)
class TaskChange:
    task_id: str
    version: int
    event_cursor: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "version": self.version,
            "event_cursor": self.event_cursor,
        }


def decode_task_change(payload: Any) -> TaskChange | None:
    """Decode the strict Pub/Sub allowlist; reject malformed or oversized hints."""
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_EVENT_HINT_BYTES:
            return None
        value = json.loads(payload)
        if not isinstance(value, dict) or set(value) != {"task_id", "version", "event_cursor"}:
            return None
        task_id = value["task_id"]
        version = value["version"]
        event_cursor = value["event_cursor"]
        if not isinstance(task_id, str) or not task_id or len(task_id) > MAX_TASK_ID_LENGTH:
            return None
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            return None
        if event_cursor is not None and (
            not isinstance(event_cursor, str) or len(event_cursor) > MAX_EVENT_CURSOR_LENGTH
        ):
            return None
        return TaskChange(task_id=task_id, version=version, event_cursor=event_cursor)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


class TaskEventHub:
    """Per-process listener fanout with bounded, latest-hint coalescing queues."""

    def __init__(self) -> None:
        self._listeners: dict[str, set[asyncio.Queue[TaskChange]]] = {}

    def subscribe(self, task_id: str) -> asyncio.Queue[TaskChange]:
        queue: asyncio.Queue[TaskChange] = asyncio.Queue(maxsize=1)
        self._listeners.setdefault(task_id, set()).add(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue[TaskChange]) -> None:
        listeners = self._listeners.get(task_id)
        if not listeners:
            return
        listeners.discard(queue)
        if not listeners:
            self._listeners.pop(task_id, None)

    def notify(self, change: TaskChange) -> None:
        for queue in tuple(self._listeners.get(change.task_id, ())):
            next_change = change
            if queue.full():
                try:
                    pending = queue.get_nowait()
                    # Delayed/out-of-order publications must not replace a
                    # newer hint already waiting for this listener.
                    if pending.version > next_change.version:
                        next_change = pending
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(next_change)
            except asyncio.QueueFull:
                pass

    @property
    def listener_count(self) -> int:
        return sum(len(listeners) for listeners in self._listeners.values())


class RedisTaskEventBus:
    """Redis Pub/Sub bridge; publish failures never roll back committed task data."""

    def __init__(
        self,
        redis_client: Any,
        hub: TaskEventHub,
        channel: str = TASK_EVENT_CHANNEL,
    ) -> None:
        self.redis = redis_client
        self.hub = hub
        self.channel = channel
        self.subscriber_connected = False
        self.publish_failures = 0
        self.last_error: str | None = None
        self._publish_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def notify(self, change: TaskChange) -> None:
        # Wake local SSE listeners immediately, then fan out a compact hint.
        self.hub.notify(change)
        if self._closed:
            return
        try:
            task = asyncio.create_task(self._publish(change), name="workforce-task-event-publish")
        except RuntimeError:
            logger.warning("No active event loop; task-change hint will be recovered by SSE reconciliation.")
            return
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_tasks.discard)

    async def _publish(self, change: TaskChange) -> None:
        payload = json.dumps(change.to_payload(), separators=(",", ":"), ensure_ascii=True)
        try:
            await self.redis.publish(self.channel, payload)
            if self.subscriber_connected:
                self.last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.publish_failures += 1
            self.last_error = type(exc).__name__
            logger.warning(
                "Task-change Pub/Sub publish failed (%s); 15-second SSE reconciliation remains active.",
                type(exc).__name__,
            )

    async def drain(self) -> None:
        pending = tuple(self._publish_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def run(self) -> None:
        """Maintain one subscriber per API process with bounded reconnect backoff."""
        retry_seconds = 1
        while not self._closed:
            pubsub = None
            connected_at = None
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe(self.channel)
                self.subscriber_connected = True
                connected_at = asyncio.get_running_loop().time()
                self.last_error = None
                while not self._closed:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=1.0,
                    )
                    if not message or message.get("type") != "message":
                        continue
                    change = decode_task_change(message.get("data"))
                    if change is not None:
                        self.hub.notify(change)
                    else:
                        logger.warning("Discarded malformed workforce task-change Pub/Sub hint.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.subscriber_connected = False
                self.last_error = type(exc).__name__
                logger.warning(
                    "Task-change Pub/Sub subscriber disconnected (%s); retrying in %s second(s).",
                    type(exc).__name__,
                    retry_seconds,
                )
            finally:
                self.subscriber_connected = False
                if connected_at is not None:
                    # Reset backoff only after a stable session; a flapping
                    # connection must continue increasing its retry delay.
                    if asyncio.get_running_loop().time() - connected_at >= 30:
                        retry_seconds = 1
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:
                        logger.debug("Could not close Pub/Sub subscription cleanly.", exc_info=True)
            if not self._closed:
                await asyncio.sleep(retry_seconds)
                retry_seconds = min(15, retry_seconds * 2)

    async def close(self) -> None:
        self._closed = True
        await self.drain()

    def status(self) -> dict[str, Any]:
        state = (
            "degraded" if self.last_error
            else "connected" if self.subscriber_connected
            else "connecting"
        )
        return {
            "transport": "redis_pubsub",
            "status": state,
            "subscriber_connected": self.subscriber_connected,
            "publish_failures": self.publish_failures,
            "last_error": self.last_error,
            "channel": self.channel,
        }
