from datetime import datetime, timezone

import pytest

from avatar_chat.adapters.outbound.memory import (
    InMemoryAuditLogRepository,
    InMemorySessionMutationUnitOfWork,
    InMemorySessionRepository,
    InMemorySessionShareRepository,
)
from avatar_chat.adapters.outbound.outbox import RealtimeOutboxDispatcher
from avatar_chat.application.dto import (
    ChatEventType,
    ChatOutputEvent,
    RealtimeOutboxEvent,
)
from avatar_chat.domain.models import (
    AuditLog,
    AvatarSession,
    AvatarState,
    SessionStatus,
    SessionVisibility,
)


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class RecoveringEventBus:
    def __init__(self) -> None:
        self.fail = True
        self.received: list[tuple[str, ChatOutputEvent]] = []

    async def publish(self, session_id: str, event: ChatOutputEvent) -> None:
        if self.fail:
            raise ConnectionError("Redis unavailable")
        self.received.append((session_id, event))

    async def subscribe(self, handler: object) -> None:
        del handler

    async def shutdown(self) -> None:
        return None


@pytest.mark.asyncio
async def test_outbox_retries_committed_event_after_bus_recovers() -> None:
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    sessions = InMemorySessionRepository()
    shares = InMemorySessionShareRepository()
    audit = InMemoryAuditLogRepository()
    unit = InMemorySessionMutationUnitOfWork(sessions, shares, audit)
    bus = RecoveringEventBus()
    dispatcher = RealtimeOutboxDispatcher(unit, bus, FixedClock(now))  # type: ignore[arg-type]
    session = AvatarSession(
        "session-1",
        "default",
        "owner-1",
        SessionVisibility.PRIVATE,
        SessionStatus.READY,
        AvatarState.IDLE,
        now,
        now,
    )
    await sessions.save(session)
    session.visibility = SessionVisibility.PUBLIC
    event = ChatOutputEvent(ChatEventType.SESSION_UPDATED, session_version=1)
    await unit.save_session(
        session,
        0,
        AuditLog(
            "audit-1",
            "owner-1",
            "session.visibility.changed",
            "avatar_session",
            "session-1",
            now,
        ),
        RealtimeOutboxEvent("event-1", "session-1", event, now),
    )

    await dispatcher.flush()
    assert len(await unit.list_pending()) == 1

    bus.fail = False
    await dispatcher.flush()

    assert bus.received == [("session-1", event)]
    assert await unit.list_pending() == ()
    assert len(audit.items) == 1
    assert (await sessions.get("session-1")).visibility is SessionVisibility.PUBLIC  # type: ignore[union-attr]
