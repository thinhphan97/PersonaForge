from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from avatar_chat.adapters.outbound.sqlalchemy_repositories import (
    SqlAlchemyConversationRepository,
    SqlAlchemyDatabase,
    SqlAlchemySessionMutationUnitOfWork,
    SqlAlchemySessionRepository,
    SqlAlchemyUserRepository,
)
from avatar_chat.application.dto import (
    ChatEventType,
    ChatOutputEvent,
    RealtimeOutboxEvent,
)
from avatar_chat.domain.errors import ConcurrentSessionUpdate
from avatar_chat.domain.models import (
    AuditLog,
    AvatarSession,
    AvatarState,
    ChatMessage,
    ChatMessageStatus,
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
        stored_message = await conversations.append(message)
        draft = await conversations.append(
            ChatMessage(
                "m2",
                "s1",
                ChatRole.ASSISTANT,
                "",
                now,
                status=ChatMessageStatus.STREAMING,
            )
        )
        updated_draft = await conversations.update(
            replace(draft, content="Hi", revision=1),
            expected_revision=0,
        )
        loaded_session = await sessions.get("s1")
        loaded_messages = await conversations.list_recent("s1", 10)
        resumed = await conversations.list_after(
            "s1",
            stored_message.sequence,
            stored_message.revision,
            10,
        )
    finally:
        await database.shutdown()

    assert loaded_session is not None
    assert loaded_session.status is SessionStatus.READY
    assert loaded_session.avatar_state is AvatarState.IDLE
    assert loaded_messages[0].content == "Hello"
    assert loaded_messages[1].content == updated_draft.content == "Hi"
    assert loaded_messages[1].revision == 1
    assert [message.message_id for message in resumed] == ["m2"]


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


@pytest.mark.asyncio
async def test_sqlalchemy_admin_mutation_commits_outbox_atomically(tmp_path: Path) -> None:
    database = SqlAlchemyDatabase(f"sqlite+aiosqlite:///{tmp_path / 'outbox.db'}")
    await database.startup()
    sessions = SqlAlchemySessionRepository(database.sessions)
    mutations = SqlAlchemySessionMutationUnitOfWork(database.sessions)
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    await save_test_user(database, now)
    session = AvatarSession(
        "s-outbox",
        "default",
        "user-1",
        SessionVisibility.PRIVATE,
        SessionStatus.READY,
        AvatarState.IDLE,
        now,
        now,
    )
    await sessions.save(session)
    session.visibility = SessionVisibility.PUBLIC
    event = ChatOutputEvent(ChatEventType.SESSION_UPDATED, session_version=1)

    try:
        await mutations.save_session(
            session,
            0,
            AuditLog(
                "audit-outbox",
                "user-1",
                "session.visibility.changed",
                "avatar_session",
                "s-outbox",
                now,
            ),
            RealtimeOutboxEvent("event-outbox", "s-outbox", event, now),
        )
        pending = await mutations.list_pending()
        await mutations.mark_published("event-outbox", now)
        remaining = await mutations.list_pending()
        stored = await sessions.get("s-outbox")
    finally:
        await database.shutdown()

    assert stored is not None
    assert stored.visibility is SessionVisibility.PUBLIC
    assert stored.version == 1
    assert len(pending) == 1
    assert pending[0].event_id == "event-outbox"
    assert pending[0].session_id == "s-outbox"
    assert pending[0].event == event
    assert remaining == ()
