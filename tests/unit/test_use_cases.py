from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest

from avatar_chat.adapters.outbound.memory import (
    InMemoryConversationRepository,
    InMemorySessionRepository,
)
from avatar_chat.application.dto import (
    ChatEventType,
    MediaAccess,
    MediaProvisioning,
    ReplyChunk,
    WebRtcAnswer,
    WebRtcOffer,
)
from avatar_chat.application.use_cases import (
    CreateSession,
    GetConversation,
    HandleMessage,
    NegotiateStream,
    ProvisionMedia,
)
from avatar_chat.domain.errors import SessionNotFound
from avatar_chat.domain.models import AvatarState, ChatMessage, SessionStatus


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
        self.started: list[tuple[str, str, str, str]] = []
        self.closed: list[str] = []
        self.fail_start = False
        self.fail_state_once: AvatarState | None = None

    async def start(
        self,
        session_id: str,
        character_id: str,
        room_name: str,
        publisher_token: str,
    ) -> None:
        if self.fail_start:
            raise RuntimeError("worker failed to start")
        self.started.append(
            (session_id, character_id, room_name, publisher_token)
        )

    async def negotiate(self, session_id: str, offer: WebRtcOffer) -> WebRtcAnswer:
        assert session_id
        return WebRtcAnswer(sdp=f"answer-for-{offer.sdp}", type="answer")

    async def change_state(
        self,
        session_id: str,
        state: AvatarState,
        state_version: int,
    ) -> None:
        assert session_id
        assert state_version > 0
        if self.fail_state_once is state:
            self.fail_state_once = None
            raise RuntimeError(f"failed to render {state.value}")
        self.states.append(state)

    async def close(self, session_id: str) -> None:
        self.closed.append(session_id)

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


class FakeMediaRooms:
    def __init__(self) -> None:
        self.provision_count = 0
        self.closed: list[str] = []

    async def provision(self, session_id: str) -> MediaProvisioning:
        self.provision_count += 1
        return MediaProvisioning(
            client=MediaAccess(
                provider="livekit",
                server_url="ws://media.test",
                access_token="client-token",
                room_name=f"avatar-{session_id}",
            ),
            publisher_token="publisher-token",
        )

    async def close(self, session_id: str) -> None:
        self.closed.append(session_id)


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
    assert session.status is SessionStatus.READY
    assert session.avatar_state is AvatarState.IDLE


@pytest.mark.asyncio
async def test_livekit_media_provision_starts_worker_and_connects_session() -> None:
    sessions = InMemorySessionRepository()
    ids = SequentialIds()
    clock = FixedClock()
    avatar = FakeAvatarStream()
    created = await CreateSession(sessions, ids, clock).execute("default")

    access = await ProvisionMedia(
        sessions,
        avatar,
        FakeMediaRooms(),
        clock,
    ).execute(created.session_id)

    assert access.provider == "livekit"
    assert avatar.started == [
        (
            created.session_id,
            "default",
            f"avatar-{created.session_id}",
            "publisher-token",
        )
    ]
    assert avatar.states == [AvatarState.IDLE]
    session = await sessions.get(created.session_id)
    assert session is not None
    assert session.status is SessionStatus.READY
    assert session.avatar_state is AvatarState.IDLE


@pytest.mark.asyncio
async def test_livekit_media_provision_is_idempotent_after_ready() -> None:
    sessions = InMemorySessionRepository()
    ids = SequentialIds()
    clock = FixedClock()
    avatar = FakeAvatarStream()
    rooms = FakeMediaRooms()
    created = await CreateSession(sessions, ids, clock).execute("default")
    provision = ProvisionMedia(sessions, avatar, rooms, clock)

    first = await provision.execute(created.session_id)
    second = await provision.execute(created.session_id)

    assert first.room_name == second.room_name
    assert len(avatar.started) == 1
    assert rooms.provision_count == 2
    session = await sessions.get(created.session_id)
    assert session is not None
    assert session.status is SessionStatus.READY


@pytest.mark.asyncio
async def test_media_start_failure_marks_session_failed_and_cleans_up() -> None:
    sessions = InMemorySessionRepository()
    ids = SequentialIds()
    clock = FixedClock()
    avatar = FakeAvatarStream()
    avatar.fail_start = True
    rooms = FakeMediaRooms()
    created = await CreateSession(sessions, ids, clock).execute("default")

    with pytest.raises(RuntimeError, match="worker failed to start"):
        await ProvisionMedia(sessions, avatar, rooms, clock).execute(created.session_id)

    session = await sessions.get(created.session_id)
    assert session is not None
    assert session.status is SessionStatus.FAILED
    assert avatar.closed == [created.session_id]
    assert rooms.closed == [created.session_id]


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
    assert session.status is SessionStatus.READY
    assert session.avatar_state is AvatarState.IDLE
    assert avatar.states[-1] is AvatarState.IDLE


@pytest.mark.asyncio
async def test_thinking_render_failure_restores_core_and_renderer_to_idle() -> None:
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
    avatar.fail_state_once = AvatarState.THINKING
    handler = HandleMessage(
        sessions,
        conversations,
        avatar,
        FakeReplies(),
        ids,
        clock,
    )

    with pytest.raises(RuntimeError, match="failed to render thinking"):
        _ = [event async for event in handler.execute(created.session_id, "Hello")]

    session = await sessions.get(created.session_id)
    assert session is not None
    assert session.status is SessionStatus.READY
    assert session.avatar_state is AvatarState.IDLE
    assert avatar.states[-1] is AvatarState.IDLE
    assert await conversations.list_recent(created.session_id, 10) == ()
    assert handler._locks == {}


@pytest.mark.asyncio
async def test_get_conversation_rejects_unknown_session() -> None:
    sessions = InMemorySessionRepository()
    conversations = InMemoryConversationRepository()

    with pytest.raises(SessionNotFound):
        await GetConversation(sessions, conversations).execute("missing")
