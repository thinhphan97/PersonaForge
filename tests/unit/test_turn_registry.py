import pytest

from personaforge_agent.adapters.outbound.turn_registry import InMemoryTurnRegistry


@pytest.mark.asyncio
async def test_in_memory_registry_rejects_active_and_completed_turns() -> None:
    registry = InMemoryTurnRegistry()
    first = await registry.claim("turn-1")
    assert first is not None
    assert await registry.claim("turn-1") is None

    await first.release()
    retry = await registry.claim("turn-1")
    assert retry is not None
    await retry.complete("completed")

    assert await registry.claim("turn-1") is None
