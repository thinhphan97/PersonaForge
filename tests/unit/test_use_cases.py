from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest

from avatar_chat.adapters.outbound.memory import (
    InMemoryConversationRepository,
    InMemorySessionRepository,
    InMemorySessionShareRepository,
)
from avatar_chat.application.dto import (
    CharacterSummary,
    ChatEventType,
    MediaAccess,
    MediaProvisioning,
    ReplyChunk,
    WebRtcAnswer,
    WebRtcOffer,
)
from avatar_chat.application.session_access import SessionAccessPolicy
from avatar_chat.application.use_cases import (
    CreateSession,
    GetConversation,
    GetPublicConversation,
    HandleMessage,
    NegotiateStream,
    ProvisionMedia,
    ProvisionPublicMedia,
)
from avatar_chat.domain.errors import CharacterNotFound, SessionNotFound
from avatar_chat.domain.models import (
    AvatarState,
    ChatMessage,
    ChatMessageStatus,
    ChatRole,
    SessionStatus,
    SessionVisibility,
)

USER_ID = "user-1"


def access_policy() -> SessionAccessPolicy:
    return SessionAccessPolicy(InMemorySessionShareRepository())


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=timezone.utc)


class SequentialIds:
    def __init__(self) -> None:
        self.value = 0

    def new_id(self) -> str:
        self.value += 1
        return f"id-{self.value}"


class FakeCharacters:
    def get(self, character_id: str) -> CharacterSummary | None:
        if character_id != "default":
            return None
        return CharacterSummary("default", "Default")

    def list(self) -> tuple[CharacterSummary, ...]:
        return (CharacterSummary("default", "Default"),)


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

    async def negotiate(
        self,
        session_id: str,
        character_id: str,
        offer: WebRtcOffer,
    ) -> WebRtcAnswer:
        assert session_id and character_id == "default"
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
        self.access_count = 0
        self.public_access_count = 0
        self.closed: list[str] = []

    async def provision(
        self,
        session_id: str,
        viewer_user_id: str,
    ) -> MediaProvisioning:
        assert viewer_user_id == USER_ID
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

    async def issue_access(self, session_id: str, user_id: str) -> MediaAccess:
        assert user_id == USER_ID
        self.access_count += 1
        return MediaAccess(
            provider="livekit",
            server_url="ws://media.test",
            access_token="client-token",
            room_name=f"avatar-{session_id}",
        )

    async def issue_public_access(
        self,
        session_id: str,
        viewer_id: str,
    ) -> MediaAccess:
        assert viewer_id
        self.public_access_count += 1
        return MediaAccess(
            provider="livekit",
            server_url="ws://media.test",
            access_token="public-client-token",
            room_name=f"avatar-{session_id}",
        )

    async def revoke_participant(self, session_id: str, user_id: str) -> None:
        del session_id, user_id

    async def revoke_participants_except(
        self,
        session_id: str,
        allowed_user_ids: tuple[str, ...],
    ) -> None:
        del session_id, allowed_user_ids

    async def close(self, session_id: str) -> None:
        self.closed.append(session_id)


@pytest.mark.asyncio
async def test_create_session_rejects_unknown_character() -> None:
    with pytest.raises(CharacterNotFound):
        await CreateSession(
            InMemorySessionRepository(),
            SequentialIds(),
            FixedClock(),
            FakeCharacters(),
        ).execute(USER_ID, "missing")


@pytest.mark.asyncio
async def test_message_flow_changes_avatar_and_persists_history() -> None:
    sessions = InMemorySessionRepository()
    conversations = InMemoryConversationRepository()
    ids = SequentialIds()
    clock = FixedClock()
    avatar = FakeAvatarStream()

    created = await CreateSession(sessions, ids, clock, FakeCharacters()).execute(
        USER_ID, "default"
    )
    access = access_policy()
    answer = await NegotiateStream(sessions, avatar, clock, access).execute(
        USER_ID,
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
        access,
    )
    events = [
        event
        async for event in handler.execute(USER_ID, created.session_id, "Hello")
    ]

    assert answer.type == "answer"
    assert avatar.states == [
        AvatarState.IDLE,
        AvatarState.THINKING,
        AvatarState.SPEAKING,
        AvatarState.IDLE,
    ]
    assert [event.type for event in events] == [
        ChatEventType.USER_MESSAGE,
        ChatEventType.AVATAR_STATE,
        ChatEventType.ASSISTANT_STARTED,
        ChatEventType.AVATAR_STATE,
        ChatEventType.ASSISTANT_CHUNK,
        ChatEventType.ASSISTANT_CHUNK,
        ChatEventType.AVATAR_STATE,
        ChatEventType.ASSISTANT_COMPLETE,
    ]
    assert events[-2].avatar_state is AvatarState.IDLE
    assert events[-1].content == "Xin chào"
    assistant_events = [
        event
        for event in events
        if event.type
        in {
            ChatEventType.ASSISTANT_STARTED,
            ChatEventType.ASSISTANT_CHUNK,
            ChatEventType.ASSISTANT_COMPLETE,
        }
    ]
    assert len({event.message_id for event in assistant_events}) == 1
    assert [
        event.content
        for event in assistant_events
        if event.type is ChatEventType.ASSISTANT_CHUNK
    ] == ["Xin ", "Xin chào"]
    history = await conversations.list_recent(created.session_id, 10)
    assert [message.content for message in history] == ["Hello", "Xin chào"]
    assert [message.sequence for message in history] == [1, 2]
    assert history[1].status is ChatMessageStatus.COMPLETE
    assert history[1].revision == 3
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
    created = await CreateSession(sessions, ids, clock, FakeCharacters()).execute(
        USER_ID, "default"
    )

    access_policy_instance = access_policy()

    access = await ProvisionMedia(
        sessions,
        avatar,
        FakeMediaRooms(),
        clock,
        access_policy_instance,
    ).execute(USER_ID, created.session_id)

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
    created = await CreateSession(sessions, ids, clock, FakeCharacters()).execute(
        USER_ID, "default"
    )
    provision = ProvisionMedia(sessions, avatar, rooms, clock, access_policy())

    first = await provision.execute(USER_ID, created.session_id)
    second = await provision.execute(USER_ID, created.session_id)

    assert first.room_name == second.room_name
    assert len(avatar.started) == 1
    assert rooms.provision_count == 1
    assert rooms.access_count == 1
    session = await sessions.get(created.session_id)
    assert session is not None
    assert session.status is SessionStatus.READY


@pytest.mark.asyncio
async def test_public_media_requires_public_ready_session() -> None:
    sessions = InMemorySessionRepository()
    ids = SequentialIds()
    clock = FixedClock()
    avatar = FakeAvatarStream()
    rooms = FakeMediaRooms()
    private = await CreateSession(sessions, ids, clock, FakeCharacters()).execute(
        USER_ID, "default"
    )
    with pytest.raises(SessionNotFound):
        await ProvisionPublicMedia(sessions, rooms, ids).execute(private.session_id)

    public = await CreateSession(sessions, ids, clock, FakeCharacters()).execute(
        USER_ID,
        "default",
        SessionVisibility.PUBLIC,
    )
    await ProvisionMedia(sessions, avatar, rooms, clock, access_policy()).execute(
        USER_ID,
        public.session_id,
    )
    access = await ProvisionPublicMedia(sessions, rooms, ids).execute(public.session_id)

    assert access.access_token == "public-client-token"
    assert rooms.public_access_count == 1


@pytest.mark.asyncio
async def test_public_conversation_is_read_only_and_requires_public_session() -> None:
    sessions = InMemorySessionRepository()
    conversations = InMemoryConversationRepository()
    ids = SequentialIds()
    clock = FixedClock()
    private = await CreateSession(sessions, ids, clock, FakeCharacters()).execute(
        USER_ID,
        "default",
    )
    with pytest.raises(SessionNotFound):
        await GetPublicConversation(sessions, conversations).execute(private.session_id)

    public = await CreateSession(sessions, ids, clock, FakeCharacters()).execute(
        USER_ID,
        "default",
        SessionVisibility.PUBLIC,
    )
    await conversations.append(
        ChatMessage(
            "message-1",
            public.session_id,
            ChatRole.ASSISTANT,
            "Xin chào public viewer",
            clock.now(),
        )
    )
    history = await GetPublicConversation(sessions, conversations).execute(
        public.session_id
    )

    assert [message.content for message in history] == ["Xin chào public viewer"]


@pytest.mark.asyncio
async def test_media_start_failure_marks_session_failed_and_cleans_up() -> None:
    sessions = InMemorySessionRepository()
    ids = SequentialIds()
    clock = FixedClock()
    avatar = FakeAvatarStream()
    avatar.fail_start = True
    rooms = FakeMediaRooms()
    created = await CreateSession(sessions, ids, clock, FakeCharacters()).execute(
        USER_ID, "default"
    )

    with pytest.raises(RuntimeError, match="worker failed to start"):
        await ProvisionMedia(
            sessions, avatar, rooms, clock, access_policy()
        ).execute(USER_ID, created.session_id)

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
    created = await CreateSession(sessions, ids, clock, FakeCharacters()).execute(
        USER_ID, "default"
    )
    access = access_policy()
    await NegotiateStream(sessions, avatar, clock, access).execute(
        USER_ID,
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
        access,
    )

    events = []
    stream = handler.execute(USER_ID, created.session_id, "Hello")
    with pytest.raises(RuntimeError, match="provider failed"):
        while True:
            events.append(await anext(stream))

    session = await sessions.get(created.session_id)
    assert session is not None
    assert session.status is SessionStatus.READY
    assert session.avatar_state is AvatarState.IDLE
    failed_history = await conversations.list_recent(created.session_id, 10)
    assert failed_history[-1].status is ChatMessageStatus.FAILED
    assert [event.type for event in events[-2:]] == [
        ChatEventType.AVATAR_STATE,
        ChatEventType.ASSISTANT_FAILED,
    ]
    assert events[-1].revision == failed_history[-1].revision
    assert avatar.states[-1] is AvatarState.IDLE


@pytest.mark.asyncio
async def test_thinking_render_failure_restores_core_and_renderer_to_idle() -> None:
    sessions = InMemorySessionRepository()
    conversations = InMemoryConversationRepository()
    ids = SequentialIds()
    clock = FixedClock()
    avatar = FakeAvatarStream()
    created = await CreateSession(sessions, ids, clock, FakeCharacters()).execute(
        USER_ID, "default"
    )
    access = access_policy()
    await NegotiateStream(sessions, avatar, clock, access).execute(
        USER_ID,
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
        access,
    )

    with pytest.raises(RuntimeError, match="failed to render thinking"):
        _ = [
            event
            async for event in handler.execute(USER_ID, created.session_id, "Hello")
        ]

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
        await GetConversation(
            sessions, conversations, access_policy()
        ).execute(USER_ID, "missing")
