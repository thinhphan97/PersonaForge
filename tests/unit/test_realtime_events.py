from types import SimpleNamespace

import pytest

from avatar_chat.adapters.inbound.fastapi_app import ChatConnectionHub
from avatar_chat.adapters.outbound.realtime_events import (
    InMemoryRealtimeEventBus,
    _event_from_payload,
    _event_payload,
)
from avatar_chat.application.dto import ChatEventType, ChatOutputEvent
from avatar_chat.domain.errors import AccessDenied


class FakeWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []
        self.closed = False

    async def send_json(self, payload: dict[str, object]) -> None:
        self.payloads.append(payload)

    async def close(self, code: int, reason: str) -> None:
        del code, reason
        self.closed = True


class FakeAuthenticator:
    async def execute(self, token: str) -> SimpleNamespace:
        return SimpleNamespace(user=SimpleNamespace(user_id=token))


class FakeConversationAccess:
    async def execute(
        self,
        user_id: str,
        session_id: str,
        limit: int,
    ) -> tuple[()]:
        del session_id, limit
        if user_id == "revoked-user":
            raise AccessDenied()
        return ()


@pytest.mark.asyncio
async def test_chat_hub_broadcasts_to_all_authorized_local_clients() -> None:
    hub = ChatConnectionHub()
    first = FakeWebSocket()
    second = FakeWebSocket()
    revoked = FakeWebSocket()
    hub.add("session-1", first, "user-1")  # type: ignore[arg-type]
    hub.add("session-1", second, "user-2")  # type: ignore[arg-type]
    hub.add("session-1", revoked, "revoked-user")  # type: ignore[arg-type]
    container = SimpleNamespace(
        authenticate_token=FakeAuthenticator(),
        get_conversation=FakeConversationAccess(),
    )
    event = ChatOutputEvent(
        ChatEventType.USER_MESSAGE,
        content="Hello",
        message_id="message-1",
        sequence=1,
        revision=0,
    )

    await hub.broadcast("session-1", event, container)  # type: ignore[arg-type]

    assert first.payloads[0]["message_id"] == "message-1"
    assert second.payloads == first.payloads
    assert revoked.payloads == []
    assert revoked.closed is True


@pytest.mark.asyncio
async def test_in_memory_event_bus_fans_out_and_event_serialization_round_trips() -> None:
    bus = InMemoryRealtimeEventBus()
    received: list[tuple[str, ChatOutputEvent]] = []

    async def receive(session_id: str, event: ChatOutputEvent) -> None:
        received.append((session_id, event))

    event = ChatOutputEvent(
        ChatEventType.ASSISTANT_CHUNK,
        content="Xin chào",
        message_id="message-2",
        sequence=2,
        revision=3,
    )
    await bus.subscribe(receive)
    await bus.publish("session-1", event)
    restored = _event_from_payload(_event_payload(event))
    closed = ChatOutputEvent(ChatEventType.SESSION_CLOSED)

    assert received == [("session-1", event)]
    assert restored == event
    assert _event_from_payload(_event_payload(closed)) == closed
