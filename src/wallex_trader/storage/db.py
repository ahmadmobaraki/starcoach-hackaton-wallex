from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from wallex_trader.config import Settings
from wallex_trader.storage.models import Base

logger = logging.getLogger("trader.db")


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        is_sqlite = dsn.startswith("sqlite")
        kwargs: dict[str, Any] = {"echo": False, "pool_pre_ping": True}
        if not is_sqlite:
            kwargs.update({"pool_size": 10, "max_overflow": 10, "pool_recycle": 1800})
        self.engine: AsyncEngine = create_async_engine(dsn, **kwargs)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        if is_sqlite:
            event.listen(self.engine.sync_engine, "connect", _sqlite_pragmas)

    async def init(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def healthcheck(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("database healthcheck failed: %s", exc)
            return False


def _sqlite_pragmas(dbapi_connection: object, _: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_database(settings: Settings) -> Database:
    return Database(settings.postgres_dsn)
