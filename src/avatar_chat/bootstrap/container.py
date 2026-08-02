from dataclasses import dataclass
from typing import Protocol, cast

from avatar_chat.adapters.outbound.http_reply import HttpStreamingReplyAdapter
from avatar_chat.adapters.outbound.media.aiortc_stream import AiortcAvatarStreamAdapter
from avatar_chat.adapters.outbound.media.recorded_source import RecordedClipCatalog
from avatar_chat.adapters.outbound.memory import (
    InMemoryConversationRepository,
    InMemorySessionRepository,
)
from avatar_chat.adapters.outbound.replies import DelayedReplyAdapter, ScriptedReplyAdapter
from avatar_chat.adapters.outbound.sqlalchemy_repositories import (
    SqlAlchemyConversationRepository,
    SqlAlchemyDatabase,
    SqlAlchemySessionRepository,
)
from avatar_chat.adapters.outbound.system import SystemClock, UuidGenerator
from avatar_chat.application.ports import (
    AvatarStreamPort,
    ConversationRepository,
    ReplyGeneratorPort,
    SessionRepository,
)
from avatar_chat.application.use_cases import (
    CloseSession,
    CreateSession,
    GetConversation,
    HandleMessage,
    NegotiateStream,
)
from avatar_chat.bootstrap.settings import Settings


@dataclass(slots=True)
class InfrastructureLifecycle(Protocol):
    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...


@dataclass(slots=True)
class Container:
    create_session: CreateSession
    negotiate_stream: NegotiateStream
    handle_message: HandleMessage
    get_conversation: GetConversation
    close_session: CloseSession
    avatar_stream: AvatarStreamPort
    lifecycles: tuple[InfrastructureLifecycle, ...] = ()

    async def startup(self) -> None:
        for lifecycle in self.lifecycles:
            await lifecycle.startup()

    async def shutdown(self) -> None:
        await self.avatar_stream.shutdown()
        for lifecycle in reversed(self.lifecycles):
            await lifecycle.shutdown()


def build_container(settings: Settings) -> Container:
    lifecycles: tuple[InfrastructureLifecycle, ...] = ()
    sessions: SessionRepository
    conversations: ConversationRepository
    if settings.database_url:
        database = SqlAlchemyDatabase(settings.database_url)
        sessions = SqlAlchemySessionRepository(database.sessions)
        conversations = SqlAlchemyConversationRepository(database.sessions)
        lifecycles = (cast(InfrastructureLifecycle, database),)
    else:
        sessions = InMemorySessionRepository()
        conversations = InMemoryConversationRepository()
    ids = UuidGenerator()
    clock = SystemClock()
    catalog = RecordedClipCatalog(settings.avatar_clips)
    avatar_stream = AiortcAvatarStreamAdapter(
        catalog=catalog,
        ice_server_urls=settings.ice_server_urls,
        ice_username=settings.ice_username,
        ice_credential=settings.ice_credential,
        sync_debug_enabled=settings.av_sync_debug,
        sync_interval_seconds=settings.av_sync_interval_seconds,
        sync_pulse_duration_seconds=settings.av_sync_pulse_duration_seconds,
    )

    replies: ReplyGeneratorPort
    if settings.reply_provider == "http":
        if not settings.reply_http_url:
            raise ValueError("REPLY_HTTP_URL is required when REPLY_PROVIDER=http")
        base_replies: ReplyGeneratorPort = HttpStreamingReplyAdapter(
            url=settings.reply_http_url,
            token=settings.reply_http_token,
        )
    elif settings.reply_provider == "scripted":
        base_replies = ScriptedReplyAdapter()
    else:
        raise ValueError(f"Unsupported REPLY_PROVIDER: {settings.reply_provider}")
    replies = DelayedReplyAdapter(base_replies, settings.reply_chunk_delay_seconds)

    return Container(
        create_session=CreateSession(sessions, ids, clock),
        negotiate_stream=NegotiateStream(sessions, avatar_stream, clock),
        handle_message=HandleMessage(
            sessions,
            conversations,
            avatar_stream,
            replies,
            ids,
            clock,
            thinking_min_duration_seconds=settings.thinking_min_duration_seconds,
        ),
        get_conversation=GetConversation(conversations),
        close_session=CloseSession(sessions, avatar_stream, clock),
        avatar_stream=avatar_stream,
        lifecycles=lifecycles,
    )
