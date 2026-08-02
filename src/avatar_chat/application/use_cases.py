import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from avatar_chat.application.dto import (
    ChatEventType,
    ChatOutputEvent,
    CreateSessionResult,
    WebRtcAnswer,
    WebRtcOffer,
)
from avatar_chat.application.ports import (
    AvatarStreamPort,
    Clock,
    ConversationRepository,
    IdGenerator,
    ReplyGeneratorPort,
    SessionRepository,
)
from avatar_chat.domain.errors import SessionBusy, SessionNotFound
from avatar_chat.domain.models import AvatarSession, AvatarState, ChatMessage, ChatRole


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
            state=AvatarState.CREATED,
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
        answer = await self.avatar.negotiate(session_id, offer)
        session.connected(self.clock.now())
        await self.avatar.change_state(session_id, AvatarState.IDLE)
        await self.sessions.save(session)
        return answer


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

        async with lock:
            session = await _require_session(self.sessions, session_id)
            user_message = ChatMessage(
                message_id=self.ids.new_id(),
                session_id=session_id,
                role=ChatRole.USER,
                content=normalized,
                created_at=self.clock.now(),
            )
            await self.conversations.append(user_message)

            session.start_thinking(self.clock.now())
            await self.sessions.save(session)
            await self.avatar.change_state(session_id, AvatarState.THINKING)
            yield ChatOutputEvent(
                type=ChatEventType.AVATAR_STATE,
                avatar_state=AvatarState.THINKING,
            )
            if self.thinking_min_duration_seconds > 0:
                await asyncio.sleep(self.thinking_min_duration_seconds)

            try:
                history = await self.conversations.list_recent(
                    session_id,
                    self.history_limit,
                )
                session.start_speaking(self.clock.now())
                await self.sessions.save(session)
                await self.avatar.change_state(session_id, AvatarState.SPEAKING)
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
                await self._restore_idle(session)
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

    async def _restore_idle(self, session: AvatarSession) -> None:
        if session.state in {AvatarState.THINKING, AvatarState.SPEAKING}:
            session.become_idle(self.clock.now())
            await self.sessions.save(session)
            await self.avatar.change_state(session.session_id, AvatarState.IDLE)


@dataclass(slots=True)
class GetConversation:
    conversations: ConversationRepository

    async def execute(self, session_id: str, limit: int = 50) -> tuple[ChatMessage, ...]:
        return await self.conversations.list_recent(session_id, limit)


@dataclass(slots=True)
class CloseSession:
    sessions: SessionRepository
    avatar: AvatarStreamPort
    clock: Clock

    async def execute(self, session_id: str) -> None:
        session = await self.sessions.get(session_id)
        if session is None:
            await self.avatar.close(session_id)
            return
        session.close(self.clock.now())
        await self.sessions.save(session)
        await self.avatar.close(session_id)


async def _require_session(
    sessions: SessionRepository,
    session_id: str,
) -> AvatarSession:
    session = await sessions.get(session_id)
    if session is None:
        raise SessionNotFound(session_id)
    return session
