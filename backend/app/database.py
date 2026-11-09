"""Async SQLAlchemy repository for durable Workforce task snapshots."""
import json
from typing import Any

from sqlalchemy import Column, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.ext.asyncio import create_async_engine


def normalize_database_url(url: str) -> str:
    value = url.strip()
    if value.startswith("postgres://"):
        return "postgresql+asyncpg://" + value[len("postgres://"):]
    if value.startswith("postgresql://"):
        return "postgresql+asyncpg://" + value[len("postgresql://"):]
    if value.startswith("sqlite:///") and not value.startswith("sqlite+aiosqlite:///"):
        return "sqlite+aiosqlite:///" + value[len("sqlite:///"):]
    return value


class PersistentTaskRepository:
    def __init__(self, database_url: str) -> None:
        normalized = normalize_database_url(database_url)
        if not normalized.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
            raise ValueError("DATABASE_URL must use PostgreSQL or SQLite.")
        self.backend = "postgresql" if normalized.startswith("postgresql+") else "sqlite"
        self.engine = create_async_engine(normalized, pool_pre_ping=True)
        self.metadata = MetaData()
        self.tasks = Table(
            "workforce_tasks", self.metadata,
            Column("task_id", String(64), primary_key=True),
            Column("payload", Text, nullable=False),
            Column("updated_at", String(64), nullable=False),
        )

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(self.metadata.create_all)

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
