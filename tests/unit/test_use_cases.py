from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest

from avatar_chat.adapters.outbound.memory import (
    InMemoryConversationRepository,
    InMemorySessionRepository,
)
from avatar_chat.application.dto import (
    ChatEventType,
    ReplyChunk,
    WebRtcAnswer,
    WebRtcOffer,
)
from avatar_chat.application.use_cases import CreateSession, HandleMessage, NegotiateStream
from avatar_chat.domain.models import AvatarState, ChatMessage


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=timezone.utc)


class SequentialIds:
    def __init__(self) -> None:
        self.value = 0

    def new_id(self) -> str:
        self.value += 1
        return f"id-{self.value}"


class FakeAvatarStream:
    def __init__(self) -> None:
        self.states: list[AvatarState] = []

    async def negotiate(self, session_id: str, offer: WebRtcOffer) -> WebRtcAnswer:
        assert session_id
        return WebRtcAnswer(sdp=f"answer-for-{offer.sdp}", type="answer")

    async def change_state(self, session_id: str, state: AvatarState) -> None:
        assert session_id
        self.states.append(state)

    async def close(self, session_id: str) -> None:
        del session_id

    async def shutdown(self) -> None:
        return None


class FakeReplies:
    async def stream_reply(
        self,
        session_id: str,
        messages: tuple[ChatMessage, ...],
    ) -> AsyncIterator[ReplyChunk]:
        assert session_id
        assert messages[-1].content == "Hello"
        yield ReplyChunk("Xin ")
        yield ReplyChunk("chào")


@pytest.mark.asyncio
async def test_message_flow_changes_avatar_and_persists_history() -> None:
    sessions = InMemorySessionRepository()
    conversations = InMemoryConversationRepository()
    ids = SequentialIds()
    clock = FixedClock()
    avatar = FakeAvatarStream()

    created = await CreateSession(sessions, ids, clock).execute("default")
    answer = await NegotiateStream(sessions, avatar, clock).execute(
        created.session_id,
        WebRtcOffer("offer", "offer"),
    )
    handler = HandleMessage(
        sessions,
        conversations,
        avatar,
        FakeReplies(),
        ids,
        clock,
    )
    events = [event async for event in handler.execute(created.session_id, "Hello")]

    assert answer.type == "answer"
    assert avatar.states == [
        AvatarState.IDLE,
        AvatarState.THINKING,
        AvatarState.SPEAKING,
        AvatarState.IDLE,
    ]
    assert [event.type for event in events] == [
        ChatEventType.AVATAR_STATE,
        ChatEventType.AVATAR_STATE,
        ChatEventType.ASSISTANT_CHUNK,
        ChatEventType.ASSISTANT_CHUNK,
        ChatEventType.AVATAR_STATE,
        ChatEventType.ASSISTANT_COMPLETE,
    ]
    assert events[-2].avatar_state is AvatarState.IDLE
    assert events[-1].content == "Xin chào"
    history = await conversations.list_recent(created.session_id, 10)
    assert [message.content for message in history] == ["Hello", "Xin chào"]
    session = await sessions.get(created.session_id)
    assert session is not None
    assert session.state is AvatarState.IDLE


class FailingReplies:
    async def stream_reply(
        self,
        session_id: str,
        messages: tuple[ChatMessage, ...],
    ) -> AsyncIterator[ReplyChunk]:
        del session_id, messages
        raise RuntimeError("provider failed")
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_provider_failure_restores_idle_state() -> None:
    sessions = InMemorySessionRepository()
    conversations = InMemoryConversationRepository()
    ids = SequentialIds()
    clock = FixedClock()
    avatar = FakeAvatarStream()
    created = await CreateSession(sessions, ids, clock).execute("default")
    await NegotiateStream(sessions, avatar, clock).execute(
        created.session_id,
        WebRtcOffer("offer", "offer"),
    )
    handler = HandleMessage(
        sessions,
        conversations,
        avatar,
        FailingReplies(),
        ids,
        clock,
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        _ = [event async for event in handler.execute(created.session_id, "Hello")]

    session = await sessions.get(created.session_id)
    assert session is not None
    assert session.state is AvatarState.IDLE
    assert avatar.states[-1] is AvatarState.IDLE
