from datetime import datetime, timezone
from pathlib import Path

import pytest

from avatar_chat.adapters.outbound.sqlalchemy_repositories import (
    SqlAlchemyConversationRepository,
    SqlAlchemyDatabase,
    SqlAlchemySessionRepository,
)
from avatar_chat.domain.models import AvatarSession, AvatarState, ChatMessage, ChatRole


@pytest.mark.asyncio
async def test_sqlalchemy_adapters_persist_session_and_messages(tmp_path: Path) -> None:
    database = SqlAlchemyDatabase(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.startup()
    sessions = SqlAlchemySessionRepository(database.sessions)
    conversations = SqlAlchemyConversationRepository(database.sessions)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session = AvatarSession("s1", "default", AvatarState.IDLE, now, now)
    message = ChatMessage("m1", "s1", ChatRole.USER, "Hello", now)

    try:
        await sessions.save(session)
        await conversations.append(message)
        loaded_session = await sessions.get("s1")
        loaded_messages = await conversations.list_recent("s1", 10)
    finally:
        await database.shutdown()

    assert loaded_session is not None
    assert loaded_session.state is AvatarState.IDLE
    assert loaded_messages[0].content == "Hello"

