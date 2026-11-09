"""Durable Redis Streams queue for approved workforce tasks.

The queue is intentionally enabled only when both DATABASE_URL and REDIS_URL
are configured. The FastAPI service owns one consumer loop for the preview
deployment; scale-out needs a separate worker service plus lease heartbeats.
"""
import asyncio
import logging
import os
import socket
import uuid
from typing import Any

from dotenv import load_dotenv
from redis.asyncio import Redis
from redis.exceptions import ResponseError

load_dotenv()
logger = logging.getLogger(__name__)

STREAM_NAME = os.getenv("WORKFORCE_TASK_STREAM", "workforce:approved-tasks")
DEAD_LETTER_STREAM = os.getenv("WORKFORCE_TASK_DLQ", "workforce:dead-tasks")
CONSUMER_GROUP = os.getenv("WORKFORCE_TASK_GROUP", "workforce-workers")
MAX_STREAM_LENGTH = 10_000
MIN_IDLE_CLAIM_MS = max(60_000, int(os.getenv("WORKFORCE_QUEUE_CLAIM_IDLE_MS", "900000")))
MAX_DELIVERIES = 3
CLAIM_SCAN_INTERVAL_SECONDS = max(15, int(os.getenv("WORKFORCE_QUEUE_CLAIM_SCAN_SECONDS", "60")))
TERMINAL_STATES = {"completed", "failed", "revision_required", "rejected", "cancelled"}


class RedisStreamTaskQueue:
    def __init__(self, redis_url: str | None = None, redis_client: Any | None = None) -> None:
        url = (redis_url if redis_url is not None else os.getenv("REDIS_URL", "")).strip()
        if redis_client is None and not url:
            raise ValueError("REDIS_URL is required to enable the durable task queue.")
        self.redis = redis_client or Redis.from_url(
            url, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=5, socket_timeout=35,
            health_check_interval=30, retry_on_timeout=True,
        )
        self.consumer = f"{socket.gethostname()}-{uuid.uuid4().hex[:10]}"
        self._last_claim_scan = 0.0
        self._claim_cursor = "0-0"

    async def initialize(self) -> None:
        await self.redis.ping()
        try:
            await self.redis.xgroup_create(
                name=STREAM_NAME, groupname=CONSUMER_GROUP, id="0-0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc).upper():
                raise

    async def enqueue(self, task_id: str) -> str:
        return await self.redis.xadd(
            STREAM_NAME,
            {"task_id": task_id, "enqueued_by": "approval_api"},
        )

    async def _delivery_count(self, message_id: str) -> int:
        pending = await self.redis.xpending_range(
            STREAM_NAME, CONSUMER_GROUP, message_id, message_id, 1
        )
        if not pending:
            return 1
        item = pending[0]
        return int(item.get("times_delivered", 1))

    async def _ack(self, message_id: str) -> None:
        await self.redis.xack(STREAM_NAME, CONSUMER_GROUP, message_id)
        # Keep the audit in Postgres; delete the acknowledged queue entry so the
        # primary stream stays small without trimming any unacknowledged jobs.
        try:
            await self.redis.xdel(STREAM_NAME, message_id)
        except Exception:
            logger.warning("Could not delete acknowledged queue entry %s.", message_id)

    async def _dead_letter(self, message_id: str, task_id: str, reason: str) -> None:
        await self.redis.xadd(
            DEAD_LETTER_STREAM,
            {"task_id": task_id, "source_message_id": message_id, "reason": reason[:500]},
            maxlen=MAX_STREAM_LENGTH,
            approximate=True,
        )
        await self._ack(message_id)

    async def _process_message(self, message_id: str, fields: dict[str, Any]) -> None:
        from .execution import execute_task
        from .state import TASKS

        task_id = str(fields.get("task_id", "")).strip()
        if not task_id:
            await self._dead_letter(message_id, "", "Queue message is missing task_id.")
            return

        task = await TASKS.get(task_id)
        if task is None:
            await self._dead_letter(message_id, task_id, "The persisted task snapshot was not found.")
            return
        if task.execution_status in TERMINAL_STATES:
            await self._ack(message_id)
            return
        if task.execution_status != "queued":
            # A message can be redelivered after the state transition was committed.
            # Ack only if it is no longer awaiting execution; startup recovery
            # converts interrupted queued/running states back to queued.
            await self._ack(message_id)
            return

        deliveries = await self._delivery_count(message_id)
        if deliveries > MAX_DELIVERIES:
            await TASKS.update(task_id, execution_status="failed", qa_status="rejected")
            await TASKS.add_event(
                task_id, "Workforce Recovery", "queue_dead_lettered",
                f"The approved task exceeded {MAX_DELIVERIES} queue deliveries and was stopped for human review.",
            )
            await self._dead_letter(message_id, task_id, "Maximum queue deliveries exceeded.")
            return

        await TASKS.add_event(
            task_id, "Workforce Queue", "queue_job_started",
            f"Durable worker claimed the approved task (delivery {deliveries}/{MAX_DELIVERIES}).",
        )
        # execute_task persists its final status before returning. If the process
        # is killed during execution, the unacked stream entry can be reclaimed.
        await execute_task(task_id)
        final_task = await TASKS.get(task_id)
        if final_task and final_task.execution_status in TERMINAL_STATES:
            await self._ack(message_id)
        else:
            logger.warning("Task %s did not reach a terminal state; leaving message pending.", task_id)

    async def _claim_stale(self) -> list[tuple[str, dict[str, Any]]]:
        result = await self.redis.xautoclaim(
            STREAM_NAME, CONSUMER_GROUP, self.consumer,
            min_idle_time=MIN_IDLE_CLAIM_MS, start_id=self._claim_cursor, count=5,
        )
        if not result or len(result) < 2:
            self._claim_cursor = "0-0"
            return []
        # XAUTOCLAIM provides a cursor so pending lists larger than COUNT are
        # scanned across successive passes rather than restarting at 0-0.
        self._claim_cursor = str(result[0] or "0-0")
        return [(message_id, fields) for message_id, fields in result[1]]

    async def _read_new(self) -> list[tuple[str, dict[str, Any]]]:
        batches = await self.redis.xreadgroup(
            CONSUMER_GROUP, self.consumer, {STREAM_NAME: ">"},
            count=1, block=30000,
        )
        messages: list[tuple[str, dict[str, Any]]] = []
        for _stream, entries in batches or []:
            messages.extend(entries)
        return messages

    async def run(self) -> None:
        logger.info("Workforce Redis Streams worker started as %s", self.consumer)
        while True:
            try:
                now = asyncio.get_running_loop().time()
                messages: list[tuple[str, dict[str, Any]]] = []
                if now - self._last_claim_scan >= CLAIM_SCAN_INTERVAL_SECONDS:
                    messages = await self._claim_stale()
                    self._last_claim_scan = now
                if not messages:
                    messages = await self._read_new()
                for message_id, fields in messages:
                    try:
                        await self._process_message(message_id, fields)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Leave the message pending. It will be reclaimed after
                        # MIN_IDLE_CLAIM_MS and dead-lettered after bounded deliveries.
                        logger.exception("Durable task message %s failed before acknowledgement.", message_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Redis Streams worker loop failed; retrying.")
                await asyncio.sleep(3)

    async def status(self) -> dict[str, Any]:
        stream_length = await self.redis.xlen(STREAM_NAME)
        pending = await self.redis.xpending(STREAM_NAME, CONSUMER_GROUP)
        return {
            "backend": "redis_streams",
            "configured": True,
            "stream_length": int(stream_length),
            "pending_messages": int(pending.get("pending", 0)) if isinstance(pending, dict) else 0,
            "consumer": self.consumer,
            "max_deliveries": MAX_DELIVERIES,
            "claim_idle_ms": MIN_IDLE_CLAIM_MS,
            "claim_scan_interval_seconds": CLAIM_SCAN_INTERVAL_SECONDS,
            "idle_read_block_ms": 30000,
        }

    async def close(self) -> None:
        await self.redis.aclose()


QUEUE_CONFIGURED = bool(os.getenv("REDIS_URL", "").strip())
TASK_QUEUE = RedisStreamTaskQueue() if QUEUE_CONFIGURED else None
