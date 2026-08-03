from datetime import datetime, timezone
from pathlib import Path

import pytest

from avatar_chat.adapters.outbound.sqlalchemy_repositories import (
    SqlAlchemyConversationRepository,
    SqlAlchemyDatabase,
    SqlAlchemySessionRepository,
    SqlAlchemyUserRepository,
)
from avatar_chat.domain.errors import ConcurrentSessionUpdate
from avatar_chat.domain.models import (
    AvatarSession,
    AvatarState,
    ChatMessage,
    ChatRole,
    SessionStatus,
    SessionVisibility,
    User,
    UserStatus,
)


async def save_test_user(database: SqlAlchemyDatabase, now: datetime) -> None:
    await SqlAlchemyUserRepository(database.sessions).save(
        User(
            "user-1",
            "test@example.com",
            "test@example.com",
            "tester",
            "tester",
            "hash",
            UserStatus.ACTIVE,
            now,
            now,
        )
    )


@pytest.mark.asyncio
async def test_sqlalchemy_adapters_persist_session_and_messages(tmp_path: Path) -> None:
    database = SqlAlchemyDatabase(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.startup()
    sessions = SqlAlchemySessionRepository(database.sessions)
    conversations = SqlAlchemyConversationRepository(database.sessions)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await save_test_user(database, now)
    session = AvatarSession(
        "s1",
        "default",
        "user-1",
        SessionVisibility.PRIVATE,
        SessionStatus.READY,
        AvatarState.IDLE,
        now,
        now,
    )
    message = ChatMessage("m1", "s1", ChatRole.USER, "Hello", now)

    try:
        await sessions.save(session)
        await conversations.append(message)
        loaded_session = await sessions.get("s1")
        loaded_messages = await conversations.list_recent("s1", 10)
    finally:
        await database.shutdown()

    assert loaded_session is not None
    assert loaded_session.status is SessionStatus.READY
    assert loaded_session.avatar_state is AvatarState.IDLE
    assert loaded_messages[0].content == "Hello"


@pytest.mark.asyncio
async def test_sqlalchemy_session_repository_uses_optimistic_version(
    tmp_path: Path,
) -> None:
    database = SqlAlchemyDatabase(f"sqlite+aiosqlite:///{tmp_path / 'version.db'}")
    await database.startup()
    repository = SqlAlchemySessionRepository(database.sessions)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await save_test_user(database, now)
    original = AvatarSession(
        "s1",
        "default",
        "user-1",
        SessionVisibility.PRIVATE,
        SessionStatus.READY,
        AvatarState.IDLE,
        now,
        now,
    )
    try:
        await repository.save(original)
        first = await repository.get("s1")
        stale = await repository.get("s1")
        assert first is not None and stale is not None
        first.start_thinking(now)
        await repository.save(first, expected_version=0)
        stale.start_thinking(now)

        with pytest.raises(ConcurrentSessionUpdate):
            await repository.save(stale, expected_version=0)
    finally:
        await database.shutdown()
