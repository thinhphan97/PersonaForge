from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from avatar_chat.domain.models import (
    AuthSession,
    AvatarState,
    ChatMessage,
    SessionStatus,
    SessionVisibility,
    SharePermission,
)


@dataclass(frozen=True, slots=True)
class WebRtcOffer:
    sdp: str
    type: str


@dataclass(frozen=True, slots=True)
class WebRtcAnswer:
    sdp: str
    type: str


@dataclass(frozen=True, slots=True)
class CreateSessionResult:
    session_id: str
    character_id: str


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: str
    email: str
    username: str


@dataclass(frozen=True, slots=True)
class AuthResult:
    user: AuthenticatedUser
    access_token: str
    refresh_token: str
    csrf_token: str
    access_expires_in: int


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class RefreshTokenClaims:
    user_id: str
    session_id: str
    family_id: str


@dataclass(frozen=True, slots=True)
class AuthContext:
    user: AuthenticatedUser
    auth_session: AuthSession


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    character_id: str
    owner_user_id: str
    visibility: SessionVisibility
    status: SessionStatus
    access_role: str
    can_chat: bool
    version: int


@dataclass(frozen=True, slots=True)
class PublicSessionInfo:
    session_id: str
    character_id: str
    status: SessionStatus
    version: int


@dataclass(frozen=True, slots=True)
class ShareSummary:
    user_id: str
    username: str
    permission: SharePermission


@dataclass(frozen=True, slots=True)
class CharacterSummary:
    character_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class MediaAccess:
    provider: str
    server_url: str
    access_token: str
    room_name: str


@dataclass(frozen=True, slots=True)
class MediaProvisioning:
    client: MediaAccess
    publisher_token: str


@dataclass(frozen=True, slots=True)
class ReplyChunk:
    text: str


class ChatEventType(str, Enum):
    SESSION_CLOSED = "session_closed"
    SESSION_UPDATED = "session_updated"
    SESSION_SHARES_UPDATED = "session_shares_updated"
    AVATAR_STATE = "avatar_state"
    USER_MESSAGE = "user_message"
    ASSISTANT_STARTED = "assistant_started"
    ASSISTANT_CHUNK = "assistant_chunk"
    ASSISTANT_COMPLETE = "assistant_complete"
    ASSISTANT_FAILED = "assistant_failed"


@dataclass(frozen=True, slots=True)
class ChatOutputEvent:
    type: ChatEventType
    content: str | None = None
    avatar_state: AvatarState | None = None
    message_id: str | None = None
    sequence: int | None = None
    revision: int | None = None
    author_user_id: str | None = None
    session_version: int | None = None


@dataclass(frozen=True, slots=True)
class RealtimeOutboxEvent:
    event_id: str
    session_id: str
    event: ChatOutputEvent
    created_at: datetime


class AgentRuntimeEventType(str, Enum):
    TURN_STARTED = "turn_started"
    HEARTBEAT = "heartbeat"
    TEXT_DELTA = "text_delta"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"


@dataclass(frozen=True, slots=True)
class AgentTurnRequest:
    turn_id: str
    session_id: str
    user_id: str
    agent_id: str
    agent_config_version: int
    user_message: str
    history: tuple[ChatMessage, ...]
    tool_permissions: tuple[str, ...] = ()
    knowledge_base_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentRuntimeEvent:
    type: AgentRuntimeEventType
    sequence: int
    text: str = ""
    stage: str = ""
    error_code: str = ""
    error_detail: str = ""


@dataclass(frozen=True, slots=True)
class AgentAuthorizationScope:
    tool_permissions: tuple[str, ...] = ()
    knowledge_base_ids: tuple[str, ...] = ()
