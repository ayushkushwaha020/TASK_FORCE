import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app.schemas import AgentPlan, PlanResponse, ResearchSource, SubtaskPlan
from app.state import TaskStore
from app.worker_coordination import TaskLease


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

    async def test_interrupted_task_requires_human_review_when_lease_is_gone(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"REDIS_URL": "rediss://default:secret@example.upstash.io:6379"}):
            url = "sqlite+aiosqlite:///" + str(Path(directory) / "tasks.db")
            first = TaskStore(url)
            await first.initialize()
            plan = await first.create(make_plan())
            await first.update(plan.task_id, approval_status="approved", execution_status="running")
            await first.close()

            second = TaskStore(url)
            async def no_active_lease(_task_id):
                return False

            await second.initialize(active_lease_checker=no_active_lease)
            restored = await second.get(plan.task_id)
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.execution_status, "revision_required")
            self.assertTrue(any(event.kind == "execution_requires_human_review" for event in restored.events))
            await second.close()

    async def test_redis_queue_requires_persistent_task_storage(self):
        with patch.dict("os.environ", {"REDIS_URL": "rediss://default:secret@example.upstash.io:6379", "DATABASE_URL": ""}):
            store = TaskStore("")
            self.assertEqual(store.storage_backend, "memory")
            self.assertFalse(store.durable_queue_configured)

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

    async def test_second_store_reads_latest_committed_version(self):
        with tempfile.TemporaryDirectory() as directory:
            url = "sqlite+aiosqlite:///" + str(Path(directory) / "shared.db")
            first = TaskStore(url)
            second = TaskStore(url)
            await first.initialize()
            await second.initialize()

            plan = await first.create(make_plan())
            initial_version = plan.version
            updated = await second.update(
                plan.task_id,
                approval_status="approved",
                execution_status="queued",
            )
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertGreater(updated.version, initial_version)

            observed = await first.get(plan.task_id)
            self.assertIsNotNone(observed)
            assert observed is not None
            self.assertEqual(observed.version, updated.version)
            self.assertEqual(observed.approval_status, "approved")
            self.assertEqual(observed.execution_status, "queued")
            await first.close()
            await second.close()

    async def test_task_store_rejects_stale_worker_fence(self):
        from app.database import StaleExecutionFenceError

        with tempfile.TemporaryDirectory() as directory:
            url = "sqlite+aiosqlite:///" + str(Path(directory) / "fenced.db")
            first = TaskStore(url)
            second = TaskStore(url)
            await first.initialize()
            await second.initialize()
            plan = await first.create(make_plan())

            current_lease = TaskLease(task_id=plan.task_id, owner_token="owner-7", generation=7)
            self.assertTrue(await first.activate_execution_lease(plan.task_id, current_lease))
            current = await first.update(
                plan.task_id,
                lease=current_lease,
                execution_status="running",
            )
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current.execution_fence, 7)

            with self.assertRaises(StaleExecutionFenceError):
                await second.update(
                    plan.task_id,
                    lease=TaskLease(task_id=plan.task_id, owner_token="owner-6", generation=6),
                    execution_status="failed",
                )

            latest = await first.get(plan.task_id)
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest.execution_fence, 7)
            self.assertEqual(latest.execution_status, "running")
            await first.close()
            await second.close()

    async def test_lease_activation_and_all_mutations_reject_old_generation(self):
        from app.database import StaleExecutionFenceError

        with tempfile.TemporaryDirectory() as directory:
            url = "sqlite+aiosqlite:///" + str(Path(directory) / "lease-mutations.db")
            current_store = TaskStore(url)
            stale_store = TaskStore(url)
            await current_store.initialize()
            await stale_store.initialize()
            plan = await current_store.create(make_plan())

            old_lease = TaskLease(plan.task_id, "old-owner", 1)
            self.assertTrue(await current_store.activate_execution_lease(plan.task_id, old_lease))
            await current_store.update(
                plan.task_id,
                lease=old_lease,
                execution_status="running",
            )

            new_lease = TaskLease(plan.task_id, "new-owner", 2)
            self.assertTrue(await current_store.activate_execution_lease(plan.task_id, new_lease))

            source = ResearchSource(
                source_id="source-1",
                query="campus event planning",
                title="Event planning guide",
                url="https://example.com/guide",
                snippet="A short planning guide.",
            )
            stale_operations = [
                lambda: stale_store.update(
                    plan.task_id, lease=old_lease, execution_status="failed"
                ),
                lambda: stale_store.update_agent(
                    plan.task_id, "director", lease=old_lease, status="failed"
                ),
                lambda: stale_store.update_subtask(
                    plan.task_id, "ops", lease=old_lease, status="failed"
                ),
                lambda: stale_store.add_event(
                    plan.task_id, "Worker", "stale", "must not persist", lease=old_lease
                ),
                lambda: stale_store.add_memory(
                    plan.task_id, "Worker", "stale", "must not persist", lease=old_lease
                ),
                lambda: stale_store.add_source(plan.task_id, source, lease=old_lease),
                lambda: stale_store.add_message(
                    plan.task_id, "Worker", "QA", "stale", "must not persist", lease=old_lease
                ),
            ]
            for operation in stale_operations:
                with self.subTest(operation=operation):
                    with self.assertRaises(StaleExecutionFenceError):
                        await operation()

            latest = await current_store.get(plan.task_id)
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest.execution_fence, 2)
            self.assertEqual(latest.execution_status, "running")
            self.assertFalse(any(event.kind == "stale" for event in latest.events))
            self.assertFalse(any(item.title == "stale" for item in latest.shared_memory))
            self.assertFalse(any(message.kind == "stale" for message in latest.messages))
            self.assertEqual(latest.research_sources, [])
            await current_store.close()
            await stale_store.close()

    async def test_running_task_with_live_redis_lease_is_not_recovered(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"REDIS_URL": "rediss://default:secret@example.upstash.io:6379"}
        ):
            url = "sqlite+aiosqlite:///" + str(Path(directory) / "active-worker.db")
            first = TaskStore(url)
            await first.initialize()
            plan = await first.create(make_plan())
            await first.update(plan.task_id, approval_status="approved", execution_status="running")
            await first.close()

            second = TaskStore(url)
            async def active_lease(_task_id):
                return True

            await second.initialize(active_lease_checker=active_lease)
            restored = await second.get(plan.task_id)
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.execution_status, "running")
            self.assertFalse(
                any(event.kind == "execution_requires_human_review" for event in restored.events)
            )
            await second.close()

    async def test_scoped_worker_lease_is_inherited_by_task_store_mutations(self):
        from app.database import StaleExecutionFenceError

        with tempfile.TemporaryDirectory() as directory:
            url = "sqlite+aiosqlite:///" + str(Path(directory) / "scoped-lease.db")
            store = TaskStore(url)
            await store.initialize()
            plan = await store.create(make_plan())
            current = TaskLease(plan.task_id, "new-owner", 2)
            stale = TaskLease(plan.task_id, "old-owner", 1)
            await store.activate_execution_lease(plan.task_id, current)

            with store.execution_lease(stale):
                child = asyncio.create_task(
                    store.add_event(plan.task_id, "Worker", "stale", "blocked by inherited fence")
                )
                with self.assertRaises(StaleExecutionFenceError):
                    await child

            with self.assertRaises(StaleExecutionFenceError):
                await store.add_event(plan.task_id, "Worker", "unscoped", "no lease context")

            latest = await store.get(plan.task_id)
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertFalse(any(event.kind == "stale" for event in latest.events))
            await store.close()


if __name__ == "__main__":
    unittest.main()
