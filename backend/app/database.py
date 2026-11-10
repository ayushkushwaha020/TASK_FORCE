"""Async SQLAlchemy repository with optimistic versions and execution fencing.

For Render (IPv4), prefer Supabase's shared transaction pooler (port 6543).
Session pooler (port 5432) is also supported. DATABASE_URL remains backend-only.
"""
import json
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    MetaData,
    String,
    Table,
    Text,
    and_,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


class TaskWriteConflict(RuntimeError):
    """Raised when a task snapshot was changed by another writer."""

    def __init__(
        self,
        task_id: str,
        expected_version: int,
        current_version: int | None,
    ) -> None:
        self.task_id = task_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Task {task_id!r} changed concurrently "
            f"(expected version {expected_version}, current version {current_version})."
        )


class StaleExecutionFenceError(TaskWriteConflict):
    """Raised when a worker attempts to write with an expired fence generation."""

    def __init__(
        self,
        task_id: str,
        expected_version: int,
        current_version: int | None,
        requested_fence: int,
        current_fence: int,
    ) -> None:
        self.requested_fence = requested_fence
        self.current_fence = current_fence
        super().__init__(task_id, expected_version, current_version)
        self.args = (
            f"Task {task_id!r} rejected execution fence {requested_fence}; "
            f"the active fence is {current_fence}.",
        )


def normalize_database_url(database_url: str) -> str:
    parsed = make_url(database_url.strip())
    if parsed.drivername in {"postgres", "postgresql"}:
        parsed = parsed.set(drivername="postgresql+asyncpg")
    elif parsed.drivername == "sqlite":
        parsed = parsed.set(drivername="sqlite+aiosqlite")
    if parsed.drivername == "postgresql+asyncpg":
        query = dict(parsed.query)
        sslmode = query.pop("sslmode", None)
        if "supabase" in (parsed.host or "").lower():
            query.setdefault("ssl", sslmode or "require")
            # Supabase shared transaction pooler does not support prepared statements.
            if parsed.port == 6543:
                query["prepared_statement_cache_size"] = "0"
        parsed = parsed.set(query=query)
    return parsed.render_as_string(hide_password=False)


class PersistentTaskRepository:
    """Canonical task snapshot repository with atomic CAS and fencing writes."""

    def __init__(self, database_url: str) -> None:
        normalized = normalize_database_url(database_url)
        if not normalized.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
            raise ValueError("DATABASE_URL must use PostgreSQL or SQLite.")
        self.is_postgres = normalized.startswith("postgresql+")
        host = (make_url(normalized).host or "").lower()
        self.backend = (
            "supabase_postgresql" if self.is_postgres and "supabase" in host
            else "postgresql" if self.is_postgres
            else "sqlite"
        )
        engine_options: dict[str, Any] = {"pool_pre_ping": True}
        if self.is_postgres:
            # Keep a small app-side pool for Supabase Free compute.
            engine_options.update(pool_size=3, max_overflow=1, pool_timeout=15)
            parsed = make_url(normalized)
            if parsed.port == 6543:
                # The shared transaction pooler does not support prepared statements.
                engine_options["connect_args"] = {"statement_cache_size": 0}
        self.engine = create_async_engine(normalized, **engine_options)
        self.schema = "workforce_private" if self.is_postgres else None
        self.metadata = MetaData()
        self.tasks = Table(
            "workforce_tasks",
            self.metadata,
            Column("task_id", String(64), primary_key=True),
            Column("payload", Text, nullable=False),
            Column("updated_at", String(64), nullable=False),
            Column("version", BigInteger, nullable=False, server_default=text("1")),
            Column("execution_fence", BigInteger, nullable=False, server_default=text("0")),
            schema=self.schema,
        )

    def _upgrade_legacy_columns(self, sync_connection: Any) -> None:
        """Add version/fence columns to databases created by earlier V3 builds."""
        columns = {
            column["name"]
            for column in inspect(sync_connection).get_columns(
                "workforce_tasks", schema=self.schema
            )
        }
        table_name = (
            '"workforce_private"."workforce_tasks"'
            if self.schema
            else '"workforce_tasks"'
        )
        if "version" not in columns:
            sync_connection.execute(text(
                f"ALTER TABLE {table_name} ADD COLUMN version BIGINT NOT NULL DEFAULT 1"
            ))
        if "execution_fence" not in columns:
            sync_connection.execute(text(
                f"ALTER TABLE {table_name} ADD COLUMN execution_fence BIGINT NOT NULL DEFAULT 0"
            ))

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            if self.is_postgres:
                await connection.execute(text("CREATE SCHEMA IF NOT EXISTS workforce_private"))
            await connection.run_sync(self.metadata.create_all)
            # create_all does not alter an existing table; upgrade old V3 databases in place.
            await connection.run_sync(self._upgrade_legacy_columns)
            if self.backend == "supabase_postgresql":
                # Keep task payloads out of Supabase's exposed public Data API surface.
                await connection.execute(text(
                    "ALTER TABLE workforce_private.workforce_tasks ENABLE ROW LEVEL SECURITY"
                ))
                await connection.execute(text(
                    "REVOKE ALL ON SCHEMA workforce_private FROM PUBLIC, anon, authenticated"
                ))
                await connection.execute(text(
                    "REVOKE ALL ON TABLE workforce_private.workforce_tasks FROM PUBLIC, anon, authenticated"
                ))

    @staticmethod
    def _decode_row(row: Any) -> dict[str, Any] | None:
        decoded = json.loads(row.payload)
        if not isinstance(decoded, dict) or not decoded.get("task_id"):
            return None
        # The dedicated columns are authoritative for concurrency metadata.
        decoded["version"] = int(row.version)
        decoded["execution_fence"] = int(row.execution_fence)
        return decoded

    async def load_all(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            result = await connection.execute(select(
                self.tasks.c.payload,
                self.tasks.c.version,
                self.tasks.c.execution_fence,
            ))
            rows = result.all()
        records = []
        for row in rows:
            decoded = self._decode_row(row)
            if decoded is not None:
                records.append(decoded)
        return records

    async def get(self, task_id: str) -> dict[str, Any] | None:
        """Read the latest committed snapshot, not a process-local cached copy."""
        async with self.engine.connect() as connection:
            result = await connection.execute(
                select(
                    self.tasks.c.payload,
                    self.tasks.c.version,
                    self.tasks.c.execution_fence,
                ).where(self.tasks.c.task_id == str(task_id))
            )
            row = result.first()
        return self._decode_row(row) if row is not None else None

    async def save(
        self,
        record: dict[str, Any],
        execution_fence: int | None = None,
        *,
        expected_version: int | None = None,
    ) -> int:
        """Atomically insert/update one snapshot using version and fence predicates.

        New records use version 0 and are inserted as version 1. Existing records
        must carry the version they were read at. Normal writes must match the
        active execution fence exactly; only activate_fence may advance it.
        """
        task_id = str(record["task_id"])
        if expected_version is None:
            expected_version = int(record.get("version", 0) or 0)
        else:
            expected_version = int(expected_version)
        requested_fence = int(
            record.get("execution_fence", 0)
            if execution_fence is None
            else execution_fence
        )
        if expected_version < 0 or requested_fence < 0:
            raise ValueError("Task version and execution fence must be non-negative.")
        if expected_version == 0 and requested_fence != 0:
            raise ValueError("New tasks must start with execution fence 0; activate a lease after insert.")

        next_version = expected_version + 1
        next_record = dict(record)
        next_record["version"] = next_version
        next_record["execution_fence"] = requested_fence
        values = {
            "task_id": task_id,
            "payload": json.dumps(
                next_record, ensure_ascii=False, separators=(",", ":")
            ),
            "updated_at": str(record.get("updated_at", "")),
            "version": next_version,
            "execution_fence": requested_fence,
        }
        if expected_version == 0:
            # Creation is insert-only: a duplicate task ID must never overwrite a row.
            dialect_insert = postgresql_insert if self.is_postgres else sqlite_insert
            statement = (
                dialect_insert(self.tasks)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[self.tasks.c.task_id])
                .returning(self.tasks.c.version)
            )
        else:
            # Existing snapshots are update-only so a deleted row cannot be resurrected
            # by a stale process holding an old task snapshot.
            statement = (
                update(self.tasks)
                .where(
                    and_(
                        self.tasks.c.task_id == task_id,
                        self.tasks.c.version == expected_version,
                        self.tasks.c.execution_fence == requested_fence,
                    )
                )
                .values(
                    payload=values["payload"],
                    updated_at=values["updated_at"],
                    version=self.tasks.c.version + 1,
                )
                .returning(self.tasks.c.version)
            )

        async with self.engine.begin() as connection:
            result = await connection.execute(statement)
            saved_version = result.scalar_one_or_none()
            if saved_version is not None:
                return int(saved_version)

            current = await connection.execute(
                select(
                    self.tasks.c.version,
                    self.tasks.c.execution_fence,
                ).where(self.tasks.c.task_id == task_id)
            )
            current_row = current.first()
            current_version = int(current_row.version) if current_row else None
            current_fence = int(current_row.execution_fence) if current_row else 0
            if current_row is not None and requested_fence != current_fence:
                raise StaleExecutionFenceError(
                    task_id=task_id,
                    expected_version=expected_version,
                    current_version=current_version,
                    requested_fence=requested_fence,
                    current_fence=current_fence,
                )
            raise TaskWriteConflict(
                task_id=task_id,
                expected_version=expected_version,
                current_version=current_version,
            )

    async def activate_fence(
        self,
        task_id: str,
        generation: int,
        expected_version: int,
    ) -> int:
        """Atomically persist a strictly newer worker generation before execution."""
        record = await self.get(task_id)
        if record is None:
            raise TaskWriteConflict(task_id, expected_version, None)
        current_version = int(record["version"])
        current_fence = int(record["execution_fence"])
        if generation <= current_fence:
            raise StaleExecutionFenceError(
                task_id, expected_version, current_version,
                generation, current_fence,
            )
        if current_version != expected_version:
            raise TaskWriteConflict(task_id, expected_version, current_version)

        next_version = expected_version + 1
        next_record = dict(record)
        next_record["version"] = next_version
        next_record["execution_fence"] = int(generation)
        values = {
            "payload": json.dumps(
                next_record, ensure_ascii=False, separators=(",", ":")
            ),
            "updated_at": str(next_record.get("updated_at", "")),
            "version": self.tasks.c.version + 1,
            "execution_fence": int(generation),
        }
        statement = (
            update(self.tasks)
            .where(
                and_(
                    self.tasks.c.task_id == str(task_id),
                    self.tasks.c.version == int(expected_version),
                    self.tasks.c.execution_fence < int(generation),
                )
            )
            .values(**values)
            .returning(self.tasks.c.version)
        )
        async with self.engine.begin() as connection:
            result = await connection.execute(statement)
            saved_version = result.scalar_one_or_none()
            if saved_version is not None:
                return int(saved_version)
            latest = await connection.execute(
                select(
                    self.tasks.c.version,
                    self.tasks.c.execution_fence,
                ).where(self.tasks.c.task_id == str(task_id))
            )
            latest_row = latest.first()
            if latest_row is None:
                raise TaskWriteConflict(task_id, expected_version, None)
            latest_version = int(latest_row.version)
            latest_fence = int(latest_row.execution_fence)
            if int(generation) <= latest_fence:
                raise StaleExecutionFenceError(
                    task_id, expected_version, latest_version,
                    int(generation), latest_fence,
                )
            raise TaskWriteConflict(task_id, expected_version, latest_version)

    async def close(self) -> None:
        await self.engine.dispose()
