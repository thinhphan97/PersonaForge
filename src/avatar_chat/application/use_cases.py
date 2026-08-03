import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from avatar_chat.application.dto import (
    ChatEventType,
    ChatOutputEvent,
    CreateSessionResult,
    MediaAccess,
    WebRtcAnswer,
    WebRtcOffer,
)
from avatar_chat.application.ports import (
    AvatarStreamPort,
    Clock,
    ConversationRepository,
    IdGenerator,
    MediaRoomPort,
    ReplyGeneratorPort,
    SessionRepository,
)
from avatar_chat.domain.errors import (
    ConcurrentSessionUpdate,
    SessionBusy,
    SessionNotFound,
)
from avatar_chat.domain.models import (
    AvatarSession,
    AvatarState,
    ChatMessage,
    ChatRole,
    SessionStatus,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CreateSession:
    sessions: SessionRepository
    ids: IdGenerator
    clock: Clock

    async def execute(self, character_id: str) -> CreateSessionResult:
        now = self.clock.now()
        session = AvatarSession(
            session_id=self.ids.new_id(),
            character_id=character_id,
            status=SessionStatus.CREATED,
            avatar_state=AvatarState.IDLE,
            created_at=now,
            updated_at=now,
        )
        await self.sessions.save(session)
        return CreateSessionResult(session.session_id, session.character_id)


@dataclass(slots=True)
class NegotiateStream:
    sessions: SessionRepository
    avatar: AvatarStreamPort
    clock: Clock

    async def execute(self, session_id: str, offer: WebRtcOffer) -> WebRtcAnswer:
        session = await _require_session(self.sessions, session_id)
        if session.status is SessionStatus.READY:
            return await self.avatar.negotiate(session_id, offer)
        if session.status is SessionStatus.MEDIA_STARTING:
            raise SessionBusy(session_id)

        await _transition_session(
            self.sessions,
            session,
            lambda: session.start_media(self.clock.now()),
        )
        try:
            answer = await self.avatar.negotiate(session_id, offer)
            await _transition_session(
                self.sessions,
                session,
                lambda: session.media_ready(self.clock.now()),
            )
            await self.avatar.change_state(
                session_id,
                AvatarState.IDLE,
                session.version,
            )
            return answer
        except BaseException:
            await _mark_media_failed_safely(self.sessions, session, self.clock)
            await _close_media_safely(self.avatar, None, session_id)
            raise


@dataclass(slots=True)
class ProvisionMedia:
    sessions: SessionRepository
    avatar: AvatarStreamPort
    rooms: MediaRoomPort
    clock: Clock

    async def execute(self, session_id: str) -> MediaAccess:
        session = await _require_session(self.sessions, session_id)
        if session.status is SessionStatus.READY:
            return (await self.rooms.provision(session_id)).client
        if session.status is SessionStatus.MEDIA_STARTING:
            raise SessionBusy(session_id)

        await _transition_session(
            self.sessions,
            session,
            lambda: session.start_media(self.clock.now()),
        )
        try:
            provisioning = await self.rooms.provision(session_id)
            await self.avatar.start(
                session_id=session.session_id,
                character_id=session.character_id,
                room_name=provisioning.client.room_name,
                publisher_token=provisioning.publisher_token,
            )
            await _transition_session(
                self.sessions,
                session,
                lambda: session.media_ready(self.clock.now()),
            )
            await self.avatar.change_state(
                session_id,
                AvatarState.IDLE,
                session.version,
            )
        except BaseException:
            await _mark_media_failed_safely(self.sessions, session, self.clock)
            await _close_media_safely(self.avatar, self.rooms, session_id)
            raise
        return provisioning.client


@dataclass(slots=True)
class HandleMessage:
    sessions: SessionRepository
    conversations: ConversationRepository
    avatar: AvatarStreamPort
    replies: ReplyGeneratorPort
    ids: IdGenerator
    clock: Clock
    history_limit: int = 30
    thinking_min_duration_seconds: float = 0
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)

    async def execute(
        self,
        session_id: str,
        content: str,
    ) -> AsyncIterator[ChatOutputEvent]:
        normalized = content.strip()
        if not normalized:
            return

        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise SessionBusy(session_id)

        try:
            async with lock:
                session = await _require_session(self.sessions, session_id)
                owns_avatar_state = False
                try:
                    await _transition_session(
                        self.sessions,
                        session,
                        lambda: session.start_thinking(self.clock.now()),
                    )
                    owns_avatar_state = True
                    await self.avatar.change_state(
                        session_id,
                        AvatarState.THINKING,
                        session.version,
                    )
                    user_message = ChatMessage(
                        message_id=self.ids.new_id(),
                        session_id=session_id,
                        role=ChatRole.USER,
                        content=normalized,
                        created_at=self.clock.now(),
                    )
                    await self.conversations.append(user_message)
                    yield ChatOutputEvent(
                        type=ChatEventType.AVATAR_STATE,
                        avatar_state=AvatarState.THINKING,
                    )
                    if self.thinking_min_duration_seconds > 0:
                        await asyncio.sleep(self.thinking_min_duration_seconds)

                    history = await self.conversations.list_recent(
                        session_id,
                        self.history_limit,
                    )
                    await _transition_session(
                        self.sessions,
                        session,
                        lambda: session.start_speaking(self.clock.now()),
                    )
                    await self.avatar.change_state(
                        session_id,
                        AvatarState.SPEAKING,
                        session.version,
                    )
                    yield ChatOutputEvent(
                        type=ChatEventType.AVATAR_STATE,
                        avatar_state=AvatarState.SPEAKING,
                    )

                    chunks: list[str] = []
                    async for chunk in self.replies.stream_reply(session_id, history):
                        chunks.append(chunk.text)
                        yield ChatOutputEvent(
                            type=ChatEventType.ASSISTANT_CHUNK,
                            content=chunk.text,
                        )

                    full_reply = "".join(chunks).strip()
                    if full_reply:
                        await self.conversations.append(
                            ChatMessage(
                                message_id=self.ids.new_id(),
                                session_id=session_id,
                                role=ChatRole.ASSISTANT,
                                content=full_reply,
                                created_at=self.clock.now(),
                            )
                        )
                except BaseException:
                    if owns_avatar_state:
                        await self._restore_idle_safely(session)
                    raise

                await self._restore_idle(session)
                yield ChatOutputEvent(
                    type=ChatEventType.AVATAR_STATE,
                    avatar_state=AvatarState.IDLE,
                )
                yield ChatOutputEvent(
                    type=ChatEventType.ASSISTANT_COMPLETE,
                    content=full_reply,
                )
        finally:
            if not lock.locked() and self._locks.get(session_id) is lock:
                self._locks.pop(session_id, None)

    async def _restore_idle(self, session: AvatarSession) -> None:
        if session.avatar_state in {AvatarState.THINKING, AvatarState.SPEAKING}:
            await _transition_session(
                self.sessions,
                session,
                lambda: session.become_idle(self.clock.now()),
            )
            await self.avatar.change_state(
                session.session_id,
                AvatarState.IDLE,
                session.version,
            )

    async def _restore_idle_safely(self, session: AvatarSession) -> None:
        try:
            await self._restore_idle(session)
        except BaseException:
            logger.exception(
                "Could not restore avatar to idle session=%s",
                session.session_id,
            )


@dataclass(slots=True)
class GetConversation:
    sessions: SessionRepository
    conversations: ConversationRepository

    async def execute(self, session_id: str, limit: int = 50) -> tuple[ChatMessage, ...]:
        await _require_session(self.sessions, session_id)
        return await self.conversations.list_recent(session_id, limit)


@dataclass(slots=True)
class CloseSession:
    sessions: SessionRepository
    avatar: AvatarStreamPort
    clock: Clock
    rooms: MediaRoomPort | None = None

    async def execute(self, session_id: str) -> None:
        try:
            for _attempt in range(3):
                session = await self.sessions.get(session_id)
                if session is None or session.status is SessionStatus.CLOSED:
                    break
                expected_version = session.version
                session.close(self.clock.now())
                try:
                    await self.sessions.save(session, expected_version)
                    break
                except ConcurrentSessionUpdate:
                    continue
            else:
                raise ConcurrentSessionUpdate(session_id)
        finally:
            try:
                await self.avatar.close(session_id)
            finally:
                if self.rooms is not None:
                    await self.rooms.close(session_id)


async def _transition_session(
    sessions: SessionRepository,
    session: AvatarSession,
    transition: Callable[[], None],
) -> None:
    expected_version = session.version
    transition()
    await sessions.save(session, expected_version)


async def _mark_media_failed_safely(
    sessions: SessionRepository,
    session: AvatarSession,
    clock: Clock,
) -> None:
    if session.status is SessionStatus.CLOSED:
        return
    try:
        await _transition_session(
            sessions,
            session,
            lambda: session.media_failed(clock.now()),
        )
    except Exception:
        logger.exception("Could not mark media failed session=%s", session.session_id)


async def _close_media_safely(
    avatar: AvatarStreamPort,
    rooms: MediaRoomPort | None,
    session_id: str,
) -> None:
    try:
        await avatar.close(session_id)
    except Exception:
        logger.exception("Could not close avatar stream session=%s", session_id)
    if rooms is not None:
        try:
            await rooms.close(session_id)
        except Exception:
            logger.exception("Could not close media room session=%s", session_id)


async def _require_session(
    sessions: SessionRepository,
    session_id: str,
) -> AvatarSession:
    session = await sessions.get(session_id)
    if session is None:
        raise SessionNotFound(session_id)
    return session
