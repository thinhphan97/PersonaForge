from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol, cast

from avatar_chat.adapters.outbound.http_reply import HttpStreamingReplyAdapter
from avatar_chat.adapters.outbound.jwt_tokens import PyJwtTokenAdapter
from avatar_chat.adapters.outbound.media.aiortc_stream import AiortcAvatarStreamAdapter
from avatar_chat.adapters.outbound.media.livekit_room import LiveKitMediaRoomAdapter
from avatar_chat.adapters.outbound.media.recorded_source import RecordedCharacterCatalog
from avatar_chat.adapters.outbound.media.redis_render import RedisAvatarStreamAdapter
from avatar_chat.adapters.outbound.memory import (
    InMemoryAuditLogRepository,
    InMemoryAuthSessionRepository,
    InMemoryConversationRepository,
    InMemorySessionRepository,
    InMemorySessionShareRepository,
    InMemoryUserRepository,
)
from avatar_chat.adapters.outbound.replies import DelayedReplyAdapter, ScriptedReplyAdapter
from avatar_chat.adapters.outbound.sqlalchemy_repositories import (
    SqlAlchemyAuditLogRepository,
    SqlAlchemyAuthSessionRepository,
    SqlAlchemyConversationRepository,
    SqlAlchemyDatabase,
    SqlAlchemySessionRepository,
    SqlAlchemySessionShareRepository,
    SqlAlchemyUserRepository,
)
from avatar_chat.adapters.outbound.system import (
    Argon2PasswordHasher,
    SecureTokenGenerator,
    SystemClock,
    UuidGenerator,
)
from avatar_chat.application.auth_use_cases import (
    AuthenticateAccessToken,
    LoginUser,
    LogoutAll,
    LogoutUser,
    RefreshAccessToken,
    RegisterUser,
)
from avatar_chat.application.ports import (
    AuditLogRepository,
    AuthSessionRepository,
    AvatarStreamPort,
    ConversationRepository,
    ReplyGeneratorPort,
    SessionRepository,
    SessionShareRepository,
    UserRepository,
)
from avatar_chat.application.session_access import (
    ChangeSessionVisibility,
    ListOwnedSessions,
    ListPublicSessions,
    ListSessionShares,
    ListSharedSessions,
    RevokeSessionShare,
    SessionAccessPolicy,
    ShareSession,
)
from avatar_chat.application.use_cases import (
    CloseSession,
    CreateSession,
    GetConversation,
    GetPublicConversation,
    GetPublicSession,
    HandleMessage,
    ListCharacters,
    NegotiateStream,
    ProvisionMedia,
    ProvisionPublicMedia,
)
from avatar_chat.bootstrap.settings import Settings


@dataclass(slots=True)
class InfrastructureLifecycle(Protocol):
    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...


@dataclass(slots=True)
class Container:
    register_user: RegisterUser
    login_user: LoginUser
    authenticate_token: AuthenticateAccessToken
    refresh_access_token: RefreshAccessToken
    logout_user: LogoutUser
    logout_all: LogoutAll
    create_session: CreateSession
    list_characters: ListCharacters
    negotiate_stream: NegotiateStream
    handle_message: HandleMessage
    get_conversation: GetConversation
    close_session: CloseSession
    avatar_stream: AvatarStreamPort
    media_provider: str
    list_owned_sessions: ListOwnedSessions
    list_shared_sessions: ListSharedSessions
    list_public_sessions: ListPublicSessions
    change_visibility: ChangeSessionVisibility
    share_session: ShareSession
    list_session_shares: ListSessionShares
    revoke_session_share: RevokeSessionShare
    refresh_cookie_name: str
    auth_cookie_secure: bool
    refresh_cookie_max_age: int
    allowed_origins: tuple[str, ...]
    provision_media: ProvisionMedia | None = None
    get_public_session: GetPublicSession | None = None
    get_public_conversation: GetPublicConversation | None = None
    provision_public_media: ProvisionPublicMedia | None = None
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
    users: UserRepository
    auth_sessions: AuthSessionRepository
    shares: SessionShareRepository
    audit: AuditLogRepository
    if settings.database_url:
        database = SqlAlchemyDatabase(
            settings.database_url,
            auto_create_schema=settings.auto_create_schema,
        )
        sessions = SqlAlchemySessionRepository(database.sessions)
        conversations = SqlAlchemyConversationRepository(database.sessions)
        users = SqlAlchemyUserRepository(database.sessions)
        auth_sessions = SqlAlchemyAuthSessionRepository(database.sessions)
        shares = SqlAlchemySessionShareRepository(database.sessions)
        audit = SqlAlchemyAuditLogRepository(database.sessions)
        lifecycles = (cast(InfrastructureLifecycle, database),)
    else:
        sessions = InMemorySessionRepository()
        conversations = InMemoryConversationRepository()
        users = InMemoryUserRepository()
        auth_sessions = InMemoryAuthSessionRepository()
        shares = InMemorySessionShareRepository()
        audit = InMemoryAuditLogRepository()
    ids = UuidGenerator()
    clock = SystemClock()
    passwords = Argon2PasswordHasher()
    tokens = SecureTokenGenerator()
    access_ttl = timedelta(minutes=settings.access_token_ttl_minutes)
    refresh_ttl = timedelta(days=settings.refresh_token_ttl_days)
    jwt_tokens = PyJwtTokenAdapter(
        settings.jwt_secret,
        settings.jwt_issuer,
        settings.jwt_audience,
        access_ttl,
        refresh_ttl,
    )
    access = SessionAccessPolicy(shares)
    characters = RecordedCharacterCatalog(
        settings.characters_root,
        settings.avatar_clips,
    )
    provision_media: ProvisionMedia | None = None
    rooms: LiveKitMediaRoomAdapter | None = None
    if settings.media_provider == "aiortc":
        avatar_stream: AvatarStreamPort = AiortcAvatarStreamAdapter(
            catalog=characters,
            ice_server_urls=settings.ice_server_urls,
            ice_username=settings.ice_username,
            ice_credential=settings.ice_credential,
            sync_debug_enabled=settings.av_sync_debug,
            sync_interval_seconds=settings.av_sync_interval_seconds,
            sync_pulse_duration_seconds=settings.av_sync_pulse_duration_seconds,
        )
    elif settings.media_provider == "livekit":
        if not all(
            (settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret)
        ):
            raise ValueError(
                "LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required "
                "when MEDIA_PROVIDER=livekit"
            )
        livekit_url = cast(str, settings.livekit_url)
        rooms = LiveKitMediaRoomAdapter(
            server_url=livekit_url,
            api_key=cast(str, settings.livekit_api_key),
            api_secret=cast(str, settings.livekit_api_secret),
            api_url=settings.livekit_api_url,
        )
        avatar_stream = RedisAvatarStreamAdapter(
            redis_url=settings.redis_url,
            worker_id=settings.render_worker_id,
            livekit_url=settings.livekit_worker_url or livekit_url,
        )
        provision_media = ProvisionMedia(sessions, avatar_stream, rooms, clock, access)
    else:
        raise ValueError(f"Unsupported MEDIA_PROVIDER: {settings.media_provider}")

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
        register_user=RegisterUser(
            users,
            auth_sessions,
            passwords,
            jwt_tokens,
            tokens,
            ids,
            clock,
            access_ttl,
            refresh_ttl,
        ),
        login_user=LoginUser(
            users,
            auth_sessions,
            passwords,
            jwt_tokens,
            tokens,
            ids,
            clock,
            access_ttl,
            refresh_ttl,
            passwords.hash("authentication-dummy-password"),
        ),
        authenticate_token=AuthenticateAccessToken(
            users, auth_sessions, jwt_tokens, clock
        ),
        refresh_access_token=RefreshAccessToken(
            users,
            auth_sessions,
            jwt_tokens,
            tokens,
            ids,
            clock,
            access_ttl,
            refresh_ttl,
        ),
        logout_user=LogoutUser(auth_sessions, clock),
        logout_all=LogoutAll(auth_sessions, clock),
        create_session=CreateSession(sessions, ids, clock, characters),
        list_characters=ListCharacters(characters),
        negotiate_stream=NegotiateStream(sessions, avatar_stream, clock, access),
        handle_message=HandleMessage(
            sessions,
            conversations,
            avatar_stream,
            replies,
            ids,
            clock,
            access,
            thinking_min_duration_seconds=settings.thinking_min_duration_seconds,
        ),
        get_conversation=GetConversation(sessions, conversations, access),
        close_session=CloseSession(sessions, avatar_stream, clock, rooms, access),
        avatar_stream=avatar_stream,
        media_provider=settings.media_provider,
        list_owned_sessions=ListOwnedSessions(sessions),
        list_shared_sessions=ListSharedSessions(sessions, shares),
        list_public_sessions=ListPublicSessions(sessions, shares),
        change_visibility=ChangeSessionVisibility(
            sessions, access, audit, ids, clock, shares, rooms
        ),
        share_session=ShareSession(
            sessions, users, shares, access, audit, ids, clock
        ),
        list_session_shares=ListSessionShares(sessions, users, shares, access),
        revoke_session_share=RevokeSessionShare(
            sessions, shares, access, audit, ids, clock, rooms
        ),
        refresh_cookie_name=settings.refresh_cookie_name,
        auth_cookie_secure=settings.auth_cookie_secure,
        refresh_cookie_max_age=int(refresh_ttl.total_seconds()),
        allowed_origins=settings.allowed_origins,
        provision_media=provision_media,
        get_public_session=GetPublicSession(sessions),
        get_public_conversation=GetPublicConversation(sessions, conversations),
        provision_public_media=(
            ProvisionPublicMedia(sessions, rooms, ids) if rooms is not None else None
        ),
        lifecycles=lifecycles,
    )
