from datetime import datetime, timezone

import pytest

from avatar_chat.adapters.outbound.memory import (
    InMemoryConversationRepository,
    InMemorySessionRepository,
)
from avatar_chat.domain.errors import ConcurrentSessionUpdate
from avatar_chat.domain.models import (
    AvatarSession,
    AvatarState,
    ChatMessage,
    ChatRole,
    SessionStatus,
    SessionVisibility,
)


@pytest.mark.asyncio
async def test_session_repository_does_not_leak_mutable_entities() -> None:
    repository = InMemorySessionRepository()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    original = AvatarSession(
        "s1",
        "default",
        "user-1",
        SessionVisibility.PRIVATE,
        SessionStatus.CREATED,
        AvatarState.IDLE,
        now,
        now,
    )
    await repository.save(original)

    loaded = await repository.get("s1")
    assert loaded is not None
    loaded.status = SessionStatus.CLOSED

    loaded_again = await repository.get("s1")
    assert loaded_again is not None
    assert loaded_again.status is SessionStatus.CREATED


@pytest.mark.asyncio
async def test_conversation_repository_returns_recent_messages_in_order() -> None:
    repository = InMemoryConversationRepository()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(3):
        await repository.append(
            ChatMessage(str(index), "s1", ChatRole.USER, str(index), now)
        )

    messages = await repository.list_recent("s1", 2)

    assert [message.content for message in messages] == ["1", "2"]


@pytest.mark.asyncio
async def test_session_repository_rejects_stale_version() -> None:
    repository = InMemorySessionRepository()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    original = AvatarSession(
        "s1",
        "default",
        "user-1",
        SessionVisibility.PRIVATE,
        SessionStatus.CREATED,
        AvatarState.IDLE,
        now,
        now,
    )
    await repository.save(original)
    first = await repository.get("s1")
    stale = await repository.get("s1")
    assert first is not None and stale is not None

    first.start_media(now)
    await repository.save(first, expected_version=0)
    stale.start_media(now)

    with pytest.raises(ConcurrentSessionUpdate):
        await repository.save(stale, expected_version=0)
