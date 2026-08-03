from dataclasses import dataclass
from enum import Enum

from avatar_chat.domain.models import (
    AuthSession,
    AvatarState,
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


@dataclass(frozen=True, slots=True)
class PublicSessionInfo:
    session_id: str
    character_id: str
    status: SessionStatus


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
    AVATAR_STATE = "avatar_state"
    USER_MESSAGE = "user_message"
    ASSISTANT_CHUNK = "assistant_chunk"
    ASSISTANT_COMPLETE = "assistant_complete"


@dataclass(frozen=True, slots=True)
class ChatOutputEvent:
    type: ChatEventType
    content: str | None = None
    avatar_state: AvatarState | None = None
