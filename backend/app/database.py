"""Async SQLAlchemy repository with Supabase Postgres support.

For Render (IPv4), use Supabase's shared pooler in session mode (port 5432).
Transaction pooler (port 6543) is also supported with asyncpg statement caching off.
DATABASE_URL remains backend-only.
"""
import json
from typing import Any

from sqlalchemy import Column, MetaData, String, Table, Text, insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


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
                # SQLAlchemy's asyncpg dialect and asyncpg's own cache are separate.
                query["prepared_statement_cache_size"] = "0"
        parsed = parsed.set(query=query)
    return parsed.render_as_string(hide_password=False)


class PersistentTaskRepository:
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
            schema=self.schema,
        )

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            if self.is_postgres:
                await connection.execute(text("CREATE SCHEMA IF NOT EXISTS workforce_private"))
            await connection.run_sync(self.metadata.create_all)
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

    async def load_all(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            result = await connection.execute(select(self.tasks.c.payload))
            payloads = result.scalars().all()
        records = []
        for payload in payloads:
            decoded = json.loads(payload)
            if isinstance(decoded, dict) and decoded.get("task_id"):
                records.append(decoded)
        return records

    async def save(self, record: dict[str, Any]) -> None:
        task_id = str(record["task_id"])
        values = {
            "payload": json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            "updated_at": str(record.get("updated_at", "")),
        }
        async with self.engine.begin() as connection:
            result = await connection.execute(
                update(self.tasks).where(self.tasks.c.task_id == task_id).values(**values)
            )
            if not result.rowcount:
                await connection.execute(insert(self.tasks).values(task_id=task_id, **values))

    async def close(self) -> None:
        await self.engine.dispose()
