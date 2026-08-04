import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from avatar_chat.adapters.outbound.session_turn_locks import (
    PostgresAdvisorySessionTurnLock,
)


@pytest.mark.asyncio
async def test_postgres_advisory_lock_is_exclusive_across_adapters() -> None:
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is required for PostgreSQL lock integration")
    engine = create_async_engine(database_url)
    first_adapter = PostgresAdvisorySessionTurnLock(engine)
    second_adapter = PostgresAdvisorySessionTurnLock(engine)
    try:
        first = await first_adapter.acquire("session-1")
        assert first is not None
        assert await second_adapter.acquire("session-1") is None
        await first.release()
        second = await second_adapter.acquire("session-1")
        assert second is not None
        await second.release()
    finally:
        await engine.dispose()
