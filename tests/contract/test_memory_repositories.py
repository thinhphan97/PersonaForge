from dataclasses import replace
from datetime import datetime, timezone

import pytest

from personaforge_chat.adapters.outbound.memory import (
    InMemoryConversationRepository,
    InMemorySessionRepository,
)
from personaforge_chat.domain.errors import ConcurrentMessageUpdate, ConcurrentSessionUpdate
from personaforge_chat.domain.models import (
    AvatarSession,
    AvatarState,
    ChatMessage,
    ChatMessageStatus,
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
async def test_conversation_repository_updates_draft_and_resumes_from_cursor() -> None:
    repository = InMemoryConversationRepository()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    user = await repository.append(
        ChatMessage("user", "s1", ChatRole.USER, "Hello", now)
    )
    draft = await repository.append(
        ChatMessage(
            "assistant",
            "s1",
            ChatRole.ASSISTANT,
            "",
            now,
            status=ChatMessageStatus.STREAMING,
        )
    )
    updated = await repository.update(
        replace(draft, content="Xin chào", revision=1),
        expected_revision=0,
    )

    after_user = await repository.list_after("s1", user.sequence, user.revision, 10)
    after_draft = await repository.list_after("s1", draft.sequence, draft.revision, 10)

    assert after_user == (updated,)
    assert after_draft == (updated,)
    with pytest.raises(ConcurrentMessageUpdate):
        await repository.update(replace(updated, revision=2), expected_revision=0)


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
