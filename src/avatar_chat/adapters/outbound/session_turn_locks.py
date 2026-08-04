import asyncio
import hashlib
from time import monotonic

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class PostgresAdvisorySessionTurnLock:
    """Hold a connection-scoped PostgreSQL advisory lock for one chat turn."""

    def __init__(
        self,
        engine: AsyncEngine,
        acquire_timeout_seconds: float = 0,
        retry_interval_seconds: float = 0.05,
    ) -> None:
        self._engine = engine
        self._timeout = max(0.0, acquire_timeout_seconds)
        self._retry_interval = max(0.01, retry_interval_seconds)

    async def acquire(self, session_id: str) -> "PostgresAdvisoryLockLease | None":
        connection = await self._engine.connect()
        lock_key = _advisory_key(session_id)
        deadline = monotonic() + self._timeout
        try:
            while True:
                acquired = await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": lock_key},
                )
                await connection.commit()
                if acquired is True:
                    return PostgresAdvisoryLockLease(connection, lock_key)
                if monotonic() >= deadline:
                    await connection.close()
                    return None
                await asyncio.sleep(
                    min(self._retry_interval, max(0.0, deadline - monotonic()))
                )
        except BaseException:
            await connection.close()
            raise


class PostgresAdvisoryLockLease:
    def __init__(self, connection: AsyncConnection, lock_key: int) -> None:
        self._connection = connection
        self._lock_key = lock_key
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            await self._connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": self._lock_key},
            )
            await self._connection.commit()
        finally:
            await self._connection.close()


def _advisory_key(session_id: str) -> int:
    digest = hashlib.blake2b(
        f"personaforge:session-turn:{session_id}".encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)
