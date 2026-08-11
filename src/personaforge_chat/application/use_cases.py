import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace

from personaforge_chat.application.dto import (
    AgentAuthorizationScope,
    AgentRuntimeEventType,
    AgentTurnRequest,
    CharacterSummary,
    ChatEventType,
    ChatOutputEvent,
    CreateSessionResult,
    MediaAccess,
    PublicSessionInfo,
    RealtimeOutboxEvent,
    VoiceSummary,
    WebRtcAnswer,
    WebRtcOffer,
)
from personaforge_chat.application.ports import (
    AgentAuthorizationPort,
    AgentDelegationTokenPort,
    AgentRuntimePort,
    AgentTurnRepository,
    AvatarStreamPort,
    CharacterCatalogPort,
    Clock,
    ConversationRepository,
    IdGenerator,
    MediaRoomPort,
    SessionMutationPort,
    SessionRepository,
    SessionTurnLockLease,
    SessionTurnLockPort,
    VoiceCatalogPort,
)
from personaforge_chat.application.session_access import SessionAccessPolicy
from personaforge_chat.application.text_segmentation import StreamingTextSegmenter
from personaforge_chat.domain.errors import (
    CharacterNotFound,
    ConcurrentSessionUpdate,
    InvalidInput,
    SessionBusy,
    SessionClosed,
    SessionNotFound,
)
from personaforge_chat.domain.models import (
    AgentTurn,
    AgentTurnStatus,
    AuditLog,
    AvatarSession,
    AvatarState,
    ChatMessage,
    ChatMessageStatus,
    ChatRole,
    SessionAction,
    SessionStatus,
    SessionVisibility,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CreateSession:
    sessions: SessionRepository
    ids: IdGenerator
    clock: Clock
    characters: CharacterCatalogPort
    voices: VoiceCatalogPort

    async def execute(
        self,
        owner_user_id: str,
        character_id: str,
        visibility: SessionVisibility = SessionVisibility.PRIVATE,
        voice_id: str | None = None,
    ) -> CreateSessionResult:
        if self.characters.get(character_id) is None:
            raise CharacterNotFound(character_id)
        resolved_voice = (
            await self.voices.get(voice_id) if voice_id else await self.voices.default()
        )
        if resolved_voice is None:
            raise InvalidInput(f"Unknown or disabled system voice: {voice_id}")
        now = self.clock.now()
        session = AvatarSession(
            session_id=self.ids.new_id(),
            character_id=character_id,
            voice_id=resolved_voice.voice_id,
            owner_user_id=owner_user_id,
            visibility=visibility,
            status=SessionStatus.CREATED,
            avatar_state=AvatarState.IDLE,
            created_at=now,
            updated_at=now,
        )
        await self.sessions.save(session)
        return CreateSessionResult(session.session_id, session.character_id, session.voice_id)


@dataclass(slots=True)
class ListCharacters:
    characters: CharacterCatalogPort

    async def execute(self) -> tuple[CharacterSummary, ...]:
        return self.characters.list()


@dataclass(slots=True)
class ListVoices:
    voices: VoiceCatalogPort

    async def execute(self) -> tuple[VoiceSummary, ...]:
        return await self.voices.list()


@dataclass(slots=True)
class NegotiateStream:
    sessions: SessionRepository
    avatar: AvatarStreamPort
    clock: Clock
    access: SessionAccessPolicy

    async def execute(
        self,
        actor_user_id: str,
        session_id: str,
        offer: WebRtcOffer,
    ) -> WebRtcAnswer:
        session = await _require_session(self.sessions, session_id)
        await self.access.require(actor_user_id, session, SessionAction.MANAGE)
        _require_open_session(session)
        if session.status is SessionStatus.READY:
            return await self.avatar.negotiate(session_id, session.character_id, offer)
        if session.status is SessionStatus.MEDIA_STARTING:
            raise SessionBusy(session_id)

        await _transition_session(
            self.sessions,
            session,
            lambda: session.start_media(self.clock.now()),
        )
        try:
            answer = await self.avatar.negotiate(
                session_id,
                session.character_id,
                offer,
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
    access: SessionAccessPolicy
    voices: VoiceCatalogPort

    async def execute(
        self,
        actor_user_id: str,
        session_id: str,
        *,
        ensure_runtime: bool = False,
    ) -> MediaAccess:
        session = await _require_session(self.sessions, session_id)
        await self.access.require(actor_user_id, session, SessionAction.VIEW)
        _require_open_session(session)
        if session.status is SessionStatus.READY and not ensure_runtime:
            return await self.rooms.issue_access(session_id, actor_user_id)
        if ensure_runtime:
            await self.access.require(actor_user_id, session, SessionAction.MANAGE)
        if session.status is SessionStatus.READY and ensure_runtime:
            voice = await self.voices.get(session.voice_id)
            if voice is None:
                raise RuntimeError(f"Configured voice is unavailable: {session.voice_id}")
            provisioning = await self.rooms.provision(session_id, actor_user_id)
            await self.avatar.start(
                session_id=session.session_id,
                character_id=session.character_id,
                voice=voice,
                room_name=provisioning.client.room_name,
                publisher_token=provisioning.publisher_token,
            )
            return provisioning.client
        if session.status is SessionStatus.MEDIA_STARTING:
            raise SessionBusy(session_id)
        await self.access.require(actor_user_id, session, SessionAction.MANAGE)
        voice = await self.voices.get(session.voice_id)
        if voice is None:
            raise RuntimeError(f"Configured voice is unavailable: {session.voice_id}")

        await _transition_session(
            self.sessions,
            session,
            lambda: session.start_media(self.clock.now()),
        )
        try:
            provisioning = await self.rooms.provision(session_id, actor_user_id)
            await self.avatar.start(
                session_id=session.session_id,
                character_id=session.character_id,
                voice=voice,
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
class GetPublicSession:
    sessions: SessionRepository

    async def execute(self, session_id: str) -> PublicSessionInfo:
        session = await _require_session(self.sessions, session_id)
        if session.visibility is not SessionVisibility.PUBLIC:
            raise SessionNotFound(session_id)
        return PublicSessionInfo(
            session.session_id,
            session.character_id,
            session.voice_id,
            session.status,
            session.version,
        )


@dataclass(slots=True)
class ProvisionPublicMedia:
    sessions: SessionRepository
    rooms: MediaRoomPort
    ids: IdGenerator

    async def execute(self, session_id: str) -> MediaAccess:
        session = await _require_session(self.sessions, session_id)
        if session.visibility is not SessionVisibility.PUBLIC:
            raise SessionNotFound(session_id)
        _require_open_session(session)
        if session.status is not SessionStatus.READY:
            raise SessionBusy(session_id)
        return await self.rooms.issue_public_access(session_id, self.ids.new_id())


@dataclass(slots=True)
class HandleMessage:
    sessions: SessionRepository
    conversations: ConversationRepository
    avatar: AvatarStreamPort
    agent: AgentRuntimePort
    agent_turns: AgentTurnRepository
    delegation_tokens: AgentDelegationTokenPort
    ids: IdGenerator
    clock: Clock
    access: SessionAccessPolicy
    history_limit: int = 30
    thinking_min_duration_seconds: float = 0
    speech_minimum_characters: int = 12
    speech_target_characters: int = 40
    speech_maximum_characters: int = 120
    authorization: AgentAuthorizationPort | None = None
    turn_locks: SessionTurnLockPort | None = None
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)

    async def execute(
        self,
        actor_user_id: str,
        session_id: str,
        content: str,
    ) -> AsyncIterator[ChatOutputEvent]:
        normalized = content.strip()
        if not normalized:
            return

        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise SessionBusy(session_id)

        distributed_lease: SessionTurnLockLease | None = None
        try:
            if self.turn_locks is not None:
                distributed_lease = await self.turn_locks.acquire(session_id)
                if distributed_lease is None:
                    raise SessionBusy(session_id)
                await distributed_lease.ensure_valid()
            async with lock:
                session = await _require_session(self.sessions, session_id)
                await self.access.require(actor_user_id, session, SessionAction.CHAT)
                _require_open_session(session)
                owns_avatar_state = False
                assistant_message: ChatMessage | None = None
                agent_turn: AgentTurn | None = None
                speech_task: asyncio.Task[None] | None = None
                speech_queue: asyncio.Queue[
                    tuple[int, str, asyncio.Future[None]] | None
                ] | None = None
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
                        author_user_id=actor_user_id,
                    )
                    user_message = await self.conversations.append(user_message)
                    yield ChatOutputEvent(
                        type=ChatEventType.USER_MESSAGE,
                        content=normalized,
                        message_id=user_message.message_id,
                        sequence=user_message.sequence,
                        revision=user_message.revision,
                        author_user_id=actor_user_id,
                    )
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
                    assistant_message = await self.conversations.append(
                        ChatMessage(
                            message_id=self.ids.new_id(),
                            session_id=session_id,
                            role=ChatRole.ASSISTANT,
                            content="",
                            created_at=self.clock.now(),
                            status=ChatMessageStatus.STREAMING,
                        )
                    )
                    yield ChatOutputEvent(
                        type=ChatEventType.ASSISTANT_STARTED,
                        content="",
                        message_id=assistant_message.message_id,
                        sequence=assistant_message.sequence,
                        revision=assistant_message.revision,
                    )
                    now = self.clock.now()
                    agent_turn = AgentTurn(
                        turn_id=self.ids.new_id(),
                        session_id=session_id,
                        user_id=actor_user_id,
                        agent_id=session.agent_id,
                        assistant_message_id=assistant_message.message_id,
                        status=AgentTurnStatus.RUNNING,
                        last_event_sequence=0,
                        started_at=now,
                        updated_at=now,
                    )
                    await self.agent_turns.save(agent_turn)
                    logger.info(
                        "Agent turn started",
                        extra={
                            "event": "chat.agent_turn.started",
                            "session_id": session_id,
                            "turn_id": agent_turn.turn_id,
                            "user_id": actor_user_id,
                            "input_chars": len(normalized),
                            "history_count": len(history),
                        },
                    )
                    authorization = (
                        await self.authorization.resolve(
                            actor_user_id,
                            session_id,
                            session.agent_id,
                        )
                        if self.authorization is not None
                        else AgentAuthorizationScope()
                    )
                    request = AgentTurnRequest(
                        turn_id=agent_turn.turn_id,
                        session_id=session_id,
                        user_id=actor_user_id,
                        agent_id=session.agent_id,
                        agent_config_version=session.version,
                        user_message=normalized,
                        history=history,
                        tool_permissions=authorization.tool_permissions,
                        knowledge_base_ids=authorization.knowledge_base_ids,
                    )
                    delegation_token = self.delegation_tokens.issue(
                        actor_user_id,
                        session_id,
                        agent_turn.turn_id,
                        session.agent_id,
                        request.tool_permissions,
                        request.knowledge_base_ids,
                        now,
                    )
                    speech_queue = asyncio.Queue(maxsize=32)
                    speech_turn_id = agent_turn.turn_id
                    speech_message_id = assistant_message.message_id

                    async def stream_speech() -> None:
                        assert speech_queue is not None
                        while True:
                            item = await speech_queue.get()
                            if item is None:
                                return
                            segment_sequence, segment_text, completion = item
                            try:
                                await self.avatar.speak(
                                    session_id,
                                    speech_turn_id,
                                    f"{speech_message_id}:{segment_sequence}",
                                    segment_text,
                                )
                            except BaseException as exc:
                                if not completion.done():
                                    completion.set_exception(exc)
                                raise
                            else:
                                if not completion.done():
                                    completion.set_result(None)

                    speech_task = asyncio.create_task(
                        stream_speech(),
                        name=f"tts-segments-{speech_turn_id}",
                    )

                    async def enqueue_speech(
                        item: tuple[int, str] | None,
                    ) -> asyncio.Future[None] | None:
                        """Apply backpressure without hanging if media playback fails."""

                        assert speech_queue is not None
                        assert speech_task is not None
                        completion: asyncio.Future[None] | None = None
                        queued_item: tuple[int, str, asyncio.Future[None]] | None
                        if item is None:
                            queued_item = None
                        else:
                            completion = asyncio.get_running_loop().create_future()
                            queued_item = (item[0], item[1], completion)
                        put_task = asyncio.create_task(speech_queue.put(queued_item))
                        done, _pending = await asyncio.wait(
                            {put_task, speech_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if speech_task in done:
                            if not put_task.done():
                                put_task.cancel()
                                await asyncio.gather(put_task, return_exceptions=True)
                            await speech_task
                            if item is not None:
                                raise RuntimeError(
                                    "Speech pipeline stopped before accepting all segments"
                                )
                            return completion
                        await put_task
                        return completion

                    text_segmenter = StreamingTextSegmenter(
                        minimum_characters=self.speech_minimum_characters,
                        target_characters=self.speech_target_characters,
                        maximum_characters=self.speech_maximum_characters,
                    )
                    speech_sequence = 0
                    chunks: list[str] = []
                    visible_reply_parts: list[str] = []
                    speaking = False
                    completed = False
                    pending_speech: list[asyncio.Future[None]] = []
                    async for event in self.agent.stream_turn(request, delegation_token):
                        if distributed_lease is not None:
                            await distributed_lease.ensure_valid()
                        if event.sequence <= agent_turn.last_event_sequence:
                            continue
                        agent_turn = replace(
                            agent_turn,
                            last_event_sequence=event.sequence,
                            updated_at=self.clock.now(),
                        )
                        await self.agent_turns.save(agent_turn)
                        if event.type is AgentRuntimeEventType.TURN_FAILED:
                            raise RuntimeError(
                                event.error_detail or event.error_code or "Agent turn failed"
                            )
                        if event.type is AgentRuntimeEventType.TURN_COMPLETED:
                            completed = True
                            continue
                        if event.type is not AgentRuntimeEventType.TEXT_DELTA:
                            continue
                        if not speaking:
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
                            speaking = True
                            yield ChatOutputEvent(
                                type=ChatEventType.AVATAR_STATE,
                                avatar_state=AvatarState.SPEAKING,
                            )
                        chunks.append(event.text)
                        emitted_segments = text_segmenter.feed(event.text)
                        for speech_text in emitted_segments:
                            completion = await enqueue_speech(
                                (speech_sequence, speech_text)
                            )
                            if completion is not None:
                                pending_speech.append(completion)
                            speech_sequence += 1
                        # Do not let the assistant transcript run ahead of the
                        # media clock.  A segment is exposed to the UI only after
                        # its corresponding audio/lipsync playback has completed.
                        # This keeps text, audio and expression output monotonic
                        # even when token generation is much faster than TTS.
                        if pending_speech:
                            await asyncio.gather(*pending_speech)
                            pending_speech.clear()
                            visible_reply_parts.extend(emitted_segments)
                        if emitted_segments:
                            visible_content = " ".join(visible_reply_parts).strip()
                            expected_revision = assistant_message.revision
                            assistant_message = replace(
                                assistant_message,
                                content=visible_content,
                                revision=expected_revision + 1,
                                updated_at=self.clock.now(),
                            )
                            assistant_message = await self.conversations.update(
                                assistant_message,
                                expected_revision,
                            )
                            yield ChatOutputEvent(
                                type=ChatEventType.ASSISTANT_CHUNK,
                                content=visible_content,
                                message_id=assistant_message.message_id,
                                sequence=assistant_message.sequence,
                                revision=assistant_message.revision,
                            )

                    if not completed:
                        raise RuntimeError("Agent stream ended before turn completion")
                    tail = text_segmenter.flush()
                    if tail:
                        completion = await enqueue_speech((speech_sequence, tail))
                        if completion is not None:
                            pending_speech.append(completion)
                    if pending_speech:
                        await asyncio.gather(*pending_speech)
                        pending_speech.clear()
                    if tail:
                        visible_reply_parts.append(tail)
                    await enqueue_speech(None)
                    if distributed_lease is not None:
                        await distributed_lease.ensure_valid()

                    full_reply = "".join(chunks).strip()
                    expected_revision = assistant_message.revision
                    assistant_message = replace(
                        assistant_message,
                        content=full_reply,
                        revision=expected_revision + 1,
                        status=ChatMessageStatus.COMPLETE,
                        updated_at=self.clock.now(),
                    )
                    assistant_message = await self.conversations.update(
                        assistant_message,
                        expected_revision,
                    )
                    completed_at = self.clock.now()
                    agent_turn = replace(
                        agent_turn,
                        status=AgentTurnStatus.COMPLETED,
                        updated_at=completed_at,
                        completed_at=completed_at,
                    )
                    await self.agent_turns.save(agent_turn)
                    logger.info(
                        "Agent turn completed",
                        extra={
                            "event": "chat.agent_turn.completed",
                            "session_id": session_id,
                            "turn_id": agent_turn.turn_id,
                            "user_id": actor_user_id,
                            "output_chars": len(full_reply),
                            "outcome": "success",
                        },
                    )

                    # The completion event is deliberately emitted only after the
                    # speech queue has drained. Consumers can therefore treat it
                    # as the single point where text and media are both complete.
                    if tail:
                        yield ChatOutputEvent(
                            type=ChatEventType.ASSISTANT_CHUNK,
                            content=" ".join(visible_reply_parts).strip(),
                            message_id=assistant_message.message_id,
                            sequence=assistant_message.sequence,
                            revision=assistant_message.revision,
                        )
                    yield ChatOutputEvent(
                        type=ChatEventType.ASSISTANT_COMPLETE,
                        content=full_reply,
                        message_id=assistant_message.message_id,
                        sequence=assistant_message.sequence,
                        revision=assistant_message.revision,
                    )

                    if speech_task is not None:
                        await speech_task
                except BaseException as exc:
                    if speech_task is not None and not speech_task.done():
                        speech_task.cancel()
                        await asyncio.gather(speech_task, return_exceptions=True)
                    if agent_turn is not None and agent_turn.status is AgentTurnStatus.RUNNING:
                        try:
                            await self.agent.cancel_turn(agent_turn.turn_id)
                        except BaseException:
                            logger.exception(
                                "Could not cancel failed agent turn=%s",
                                agent_turn.turn_id,
                            )
                        failed_at = self.clock.now()
                        status = (
                            AgentTurnStatus.CANCELLED
                            if isinstance(exc, asyncio.CancelledError)
                            else AgentTurnStatus.TIMED_OUT
                            if isinstance(exc, TimeoutError)
                            else AgentTurnStatus.FAILED
                        )
                        agent_turn = replace(
                            agent_turn,
                            status=status,
                            updated_at=failed_at,
                            completed_at=failed_at,
                            failure_code=type(exc).__name__,
                            failure_detail=str(exc)[:1000],
                        )
                        try:
                            await self.agent_turns.save(agent_turn)
                        except BaseException:
                            logger.exception(
                                "Could not persist failed agent turn=%s",
                                agent_turn.turn_id,
                            )
                    failed_message: ChatMessage | None = None
                    if (
                        assistant_message is not None
                        and assistant_message.status is ChatMessageStatus.STREAMING
                    ):
                        try:
                            expected_revision = assistant_message.revision
                            assistant_message = replace(
                                assistant_message,
                                revision=expected_revision + 1,
                                status=ChatMessageStatus.FAILED,
                                updated_at=self.clock.now(),
                            )
                            failed_message = await self.conversations.update(
                                assistant_message,
                                expected_revision,
                            )
                        except BaseException:
                            logger.exception(
                                "Could not mark assistant message failed message=%s",
                                assistant_message.message_id,
                            )
                    if owns_avatar_state:
                        await self._restore_idle_safely(session)
                    if isinstance(exc, Exception):
                        if owns_avatar_state:
                            yield ChatOutputEvent(
                                type=ChatEventType.AVATAR_STATE,
                                avatar_state=AvatarState.IDLE,
                            )
                        if failed_message is not None:
                            yield ChatOutputEvent(
                                type=ChatEventType.ASSISTANT_FAILED,
                                content=failed_message.content,
                                message_id=failed_message.message_id,
                                sequence=failed_message.sequence,
                                revision=failed_message.revision,
                            )
                    raise

                await self._restore_idle(session)
                assert assistant_message is not None
                yield ChatOutputEvent(
                    type=ChatEventType.AVATAR_STATE,
                    avatar_state=AvatarState.IDLE,
                )
        finally:
            if distributed_lease is not None:
                try:
                    await distributed_lease.release()
                except BaseException:
                    logger.exception(
                        "Could not release distributed session turn lock session=%s",
                        session_id,
                    )
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
    access: SessionAccessPolicy

    async def execute(
        self,
        actor_user_id: str,
        session_id: str,
        limit: int = 50,
    ) -> tuple[ChatMessage, ...]:
        session = await _require_session(self.sessions, session_id)
        await self.access.require(actor_user_id, session, SessionAction.VIEW)
        return await self.conversations.list_recent(session_id, limit)

    async def execute_after(
        self,
        actor_user_id: str,
        session_id: str,
        sequence: int,
        revision: int,
        limit: int = 100,
    ) -> tuple[ChatMessage, ...]:
        session = await _require_session(self.sessions, session_id)
        await self.access.require(actor_user_id, session, SessionAction.VIEW)
        return await self.conversations.list_after(
            session_id,
            sequence,
            revision,
            limit,
        )


@dataclass(slots=True)
class GetPublicConversation:
    sessions: SessionRepository
    conversations: ConversationRepository

    async def execute(
        self,
        session_id: str,
        limit: int = 50,
    ) -> tuple[ChatMessage, ...]:
        session = await _require_session(self.sessions, session_id)
        if session.visibility is not SessionVisibility.PUBLIC:
            raise SessionNotFound(session_id)
        return await self.conversations.list_recent(session_id, limit)

    async def execute_after(
        self,
        session_id: str,
        sequence: int,
        revision: int,
        limit: int = 100,
    ) -> tuple[ChatMessage, ...]:
        session = await _require_session(self.sessions, session_id)
        if session.visibility is not SessionVisibility.PUBLIC:
            raise SessionNotFound(session_id)
        return await self.conversations.list_after(
            session_id,
            sequence,
            revision,
            limit,
        )


@dataclass(slots=True)
class CloseSession:
    sessions: SessionRepository
    avatar: AvatarStreamPort
    clock: Clock
    rooms: MediaRoomPort | None = None
    access: SessionAccessPolicy | None = None
    mutations: SessionMutationPort | None = None
    ids: IdGenerator | None = None

    async def execute(self, actor_user_id: str, session_id: str) -> None:
        try:
            for _attempt in range(3):
                session = await self.sessions.get(session_id)
                if session is None:
                    break
                if self.access is not None:
                    await self.access.require(
                        actor_user_id,
                        session,
                        SessionAction.MANAGE,
                    )
                if session.status is SessionStatus.CLOSED:
                    break
                expected_version = session.version
                session.close(self.clock.now())
                try:
                    if self.mutations is not None and self.ids is not None:
                        await self.mutations.save_session(
                            session,
                            expected_version,
                            AuditLog(
                                audit_id=self.ids.new_id(),
                                actor_user_id=actor_user_id,
                                action="session.closed",
                                resource_type="avatar_session",
                                resource_id=session_id,
                                created_at=self.clock.now(),
                            ),
                            RealtimeOutboxEvent(
                                event_id=self.ids.new_id(),
                                session_id=session_id,
                                event=ChatOutputEvent(
                                    type=ChatEventType.SESSION_CLOSED,
                                    session_version=expected_version + 1,
                                ),
                                created_at=self.clock.now(),
                            ),
                        )
                    else:
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


def _require_open_session(session: AvatarSession) -> None:
    if session.status is SessionStatus.CLOSED:
        raise SessionClosed(session.session_id)
