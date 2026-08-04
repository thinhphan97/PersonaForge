import asyncio
import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from avatar_chat.adapters.outbound.session_turn_locks import RedisSessionTurnLock
from personaforge_agent.adapters.outbound.turn_registry import RedisTurnRegistry


def redis_url() -> str:
    value = os.getenv("TEST_REDIS_URL")
    if not value:
        pytest.skip("TEST_REDIS_URL is required for Redis coordination integration")
    return value


@pytest.mark.asyncio
async def test_redis_session_lock_is_exclusive_and_renewable() -> None:
    url = redis_url()
    session_id = f"test-{uuid4()}"
    first_adapter = RedisSessionTurnLock(url, lease_seconds=3)
    second_adapter = RedisSessionTurnLock(url, lease_seconds=3)
    await first_adapter.startup()
    await second_adapter.startup()
    try:
        first = await first_adapter.acquire(session_id)
        assert first is not None
        assert await second_adapter.acquire(session_id) is None
        await asyncio.sleep(1.1)
        await first.ensure_valid()
        assert await second_adapter.acquire(session_id) is None
        await first.release()
        second = await second_adapter.acquire(session_id)
        assert second is not None
        await second.release()
    finally:
        await first_adapter.shutdown()
        await second_adapter.shutdown()


@pytest.mark.asyncio
async def test_redis_turn_registry_rejects_active_and_completed_turn() -> None:
    url = redis_url()
    turn_id = f"test-{uuid4()}"
    registry_a = RedisTurnRegistry(url, lease_seconds=3, result_retention_seconds=60)
    registry_b = RedisTurnRegistry(url, lease_seconds=3, result_retention_seconds=60)
    await registry_a.startup()
    await registry_b.startup()
    try:
        claim = await registry_a.claim(turn_id)
        assert claim is not None
        assert await registry_b.claim(turn_id) is None
        await asyncio.sleep(1.1)
        await claim.ensure_valid()
        await claim.complete("completed")
        assert await registry_b.claim(turn_id) is None
    finally:
        await registry_a.shutdown()
        await registry_b.shutdown()
        redis = Redis.from_url(url)
        await redis.delete(
            f"personaforge:agent-turn:claim:{turn_id}",
            f"personaforge:agent-turn:result:{turn_id}",
        )
        await redis.aclose()
