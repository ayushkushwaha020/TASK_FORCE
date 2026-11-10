import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.engine import make_url

from app.database import (
    PersistentTaskRepository,
    StaleExecutionFenceError,
    TaskWriteConflict,
    normalize_database_url,
)


class DatabaseUrlTests(unittest.TestCase):
    def test_supabase_session_pooler_uses_ssl_and_asyncpg(self):
        normalized = normalize_database_url(
            "postgresql://postgres.project-ref:password@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
        )
        url = make_url(normalized)
        self.assertEqual(url.drivername, "postgresql+asyncpg")
        self.assertEqual(url.query.get("ssl"), "require")
        self.assertNotIn("sslmode", url.query)
        self.assertNotIn("statement_cache_size", url.query)

    def test_supabase_transaction_pooler_disables_statement_cache(self):
        normalized = normalize_database_url(
            "postgresql://postgres.project-ref:password@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require"
        )
        url = make_url(normalized)
        self.assertEqual(url.query.get("ssl"), "require")
        self.assertEqual(url.query.get("prepared_statement_cache_size"), "0")

    def test_local_sqlite_url_is_normalized(self):
        self.assertEqual(
            normalize_database_url("sqlite:///./workforce.db"),
            "sqlite+aiosqlite:///./workforce.db",
        )

    def test_supabase_task_schema_is_private_and_uses_rls(self):
        repository = PersistentTaskRepository(
            "postgresql://postgres.project-ref:password@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
        )
        self.assertEqual(repository.backend, "supabase_postgresql")
        self.assertEqual(repository.schema, "workforce_private")


class PersistentTaskRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "tasks.db"
        self.database_url = "sqlite+aiosqlite:///" + str(self.database_path)

    def tearDown(self):
        self.tempdir.cleanup()

    def make_record(self, task_id="task-1"):
        return {
            "task_id": task_id,
            "title": "Fence test",
            "execution_status": "queued",
            "version": 0,
            "execution_fence": 0,
            "updated_at": "2026-11-10T00:00:00+00:00",
        }

    async def test_atomic_insert_update_and_optimistic_conflict(self):
        repository = PersistentTaskRepository(self.database_url)
        await repository.initialize()
        record = self.make_record()
        self.assertEqual(await repository.save(record), 1)
        first = await repository.get("task-1")
        self.assertEqual(first["version"], 1)

        stale_copy = copy.deepcopy(first)
        first["execution_status"] = "running"
        self.assertEqual(await repository.save(first), 2)

        stale_copy["execution_status"] = "failed"
        with self.assertRaises(TaskWriteConflict):
            await repository.save(stale_copy)

        latest = await repository.get("task-1")
        self.assertEqual(latest["version"], 2)
        self.assertEqual(latest["execution_status"], "running")
        await repository.close()

    async def test_stale_execution_fence_is_rejected_even_with_current_version(self):
        repository = PersistentTaskRepository(self.database_url)
        await repository.initialize()
        await repository.save(self.make_record())

        current = await repository.get("task-1")
        self.assertEqual(
            await repository.activate_fence("task-1", generation=12, expected_version=current["version"]),
            2,
        )
        current = await repository.get("task-1")
        current["execution_status"] = "running"
        self.assertEqual(await repository.save(current, execution_fence=12), 3)

        stale = await repository.get("task-1")
        stale["execution_status"] = "failed"
        with self.assertRaises(StaleExecutionFenceError) as raised:
            await repository.save(stale, execution_fence=11)
        self.assertEqual(raised.exception.requested_fence, 11)
        self.assertEqual(raised.exception.current_fence, 12)

        latest = await repository.get("task-1")
        self.assertEqual(latest["execution_status"], "running")
        self.assertEqual(latest["execution_fence"], 12)
        self.assertEqual(latest["version"], 3)
        await repository.close()

    async def test_fencing_generation_can_advance_but_never_regress(self):
        repository = PersistentTaskRepository(self.database_url)
        await repository.initialize()
        await repository.save(self.make_record())

        record = await repository.get("task-1")
        with self.assertRaises(StaleExecutionFenceError):
            await repository.save(record, execution_fence=99)
        self.assertEqual(await repository.activate_fence("task-1", 4, record["version"]), 2)
        record = await repository.get("task-1")
        self.assertEqual(await repository.activate_fence("task-1", 9, record["version"]), 3)
        record = await repository.get("task-1")
        with self.assertRaises(StaleExecutionFenceError):
            await repository.save(record, execution_fence=8)

        latest = await repository.get("task-1")
        self.assertEqual(latest["execution_fence"], 9)
        await repository.close()

    async def test_two_repository_instances_cannot_overwrite_same_version(self):
        first = PersistentTaskRepository(self.database_url)
        second = PersistentTaskRepository(self.database_url)
        await first.initialize()
        await second.initialize()
        await first.save(self.make_record())
        snapshot_a = await first.get("task-1")
        snapshot_b = await second.get("task-1")

        snapshot_a["execution_status"] = "running"
        snapshot_b["execution_status"] = "failed"
        results = await asyncio.gather(
            first.save(snapshot_a),
            second.save(snapshot_b),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(item, int) for item in results), 1)
        self.assertEqual(sum(isinstance(item, TaskWriteConflict) for item in results), 1)
        latest = await second.get("task-1")
        self.assertEqual(latest["version"], 2)
        self.assertIn(latest["execution_status"], {"running", "failed"})
        await first.close()
        await second.close()


    async def test_activate_fence_requires_a_strictly_newer_generation(self):
        repository = PersistentTaskRepository(self.database_url)
        await repository.initialize()
        await repository.save(self.make_record())
        first = await repository.get("task-1")
        self.assertEqual(
            await repository.activate_fence("task-1", generation=3, expected_version=first["version"]),
            2,
        )
        with self.assertRaises(StaleExecutionFenceError):
            await repository.activate_fence("task-1", generation=3, expected_version=2)
        latest = await repository.get("task-1")
        self.assertEqual(latest["execution_fence"], 3)
        self.assertEqual(latest["version"], 2)
        await repository.close()

    async def test_existing_snapshot_is_not_resurrected_after_row_removal(self):
        repository = PersistentTaskRepository(self.database_url)
        await repository.initialize()
        await repository.save(self.make_record())
        stale_snapshot = await repository.get("task-1")

        async with repository.engine.begin() as connection:
            await connection.execute(
                delete(repository.tasks).where(repository.tasks.c.task_id == "task-1")
            )
        with self.assertRaises(TaskWriteConflict):
            await repository.save(stale_snapshot)
        self.assertIsNone(await repository.get("task-1"))
        await repository.close()

    async def test_duplicate_create_does_not_overwrite_existing_task(self):
        repository = PersistentTaskRepository(self.database_url)
        await repository.initialize()
        await repository.save(self.make_record())
        duplicate = self.make_record()
        duplicate["title"] = "must not replace original"
        with self.assertRaises(TaskWriteConflict):
            await repository.save(duplicate)
        latest = await repository.get("task-1")
        self.assertEqual(latest["title"], "Fence test")
        self.assertEqual(latest["version"], 1)
        await repository.close()

    async def test_loads_legacy_table_and_adds_concurrency_columns(self):
        legacy_engine = create_async_engine(self.database_url)
        async with legacy_engine.begin() as connection:
            await connection.execute(text(
                "CREATE TABLE workforce_tasks ("
                "task_id VARCHAR(64) PRIMARY KEY, "
                "payload TEXT NOT NULL, "
                "updated_at VARCHAR(64) NOT NULL)"
            ))
            legacy_payload = json.dumps({
                "task_id": "legacy-task",
                "title": "Legacy row",
                "execution_status": "awaiting_approval",
            })
            await connection.execute(text(
                "INSERT INTO workforce_tasks (task_id, payload, updated_at) "
                "VALUES ('legacy-task', :payload, '2026-11-09T00:00:00+00:00')"
            ), {"payload": legacy_payload})
        await legacy_engine.dispose()

        repository = PersistentTaskRepository(self.database_url)
        await repository.initialize()
        record = await repository.get("legacy-task")
        self.assertIsNotNone(record)
        self.assertEqual(record["version"], 1)
        self.assertEqual(record["execution_fence"], 0)
        record["title"] = "Migrated row"
        self.assertEqual(await repository.save(record), 2)
        await repository.close()

    async def test_loaded_payload_metadata_matches_authoritative_columns(self):
        repository = PersistentTaskRepository(self.database_url)
        await repository.initialize()
        await repository.save(self.make_record())
        record = await repository.get("task-1")
        await repository.activate_fence("task-1", 3, record["version"])
        loaded = await repository.load_all()
        self.assertEqual(loaded[0]["version"], 2)
        self.assertEqual(loaded[0]["execution_fence"], 3)
        await repository.close()


if __name__ == "__main__":
    unittest.main()
