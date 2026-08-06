from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from personaforge_chat.domain.errors import InvalidStateTransition


class AvatarState(str, Enum):
    IDLE = "idle"
    THINKING = "thinking"
    SPEAKING = "speaking"


class SessionStatus(str, Enum):
    CREATED = "created"
    MEDIA_STARTING = "media_starting"
    READY = "ready"
    FAILED = "failed"
    CLOSED = "closed"


class ChatRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessageStatus(str, Enum):
    STREAMING = "streaming"
    COMPLETE = "complete"
    FAILED = "failed"


class UserStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class SessionVisibility(str, Enum):
    PRIVATE = "private"
    PUBLIC = "public"


class SharePermission(str, Enum):
    VIEWER = "viewer"
    INTERACTOR = "interactor"


class AgentTurnStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class SessionAction(str, Enum):
    VIEW = "view"
    CHAT = "chat"
    MANAGE = "manage"


@dataclass(slots=True)
class User:
    user_id: str
    email: str
    normalized_email: str
    username: str
    normalized_username: str
    password_hash: str
    status: UserStatus
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class AuthSession:
    auth_session_id: str
    user_id: str
    family_id: str
    token_hash: str
    csrf_token: str
    expires_at: datetime
    created_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None = None
    replaced_by_session_id: str | None = None


@dataclass(slots=True)
class SessionShare:
    share_id: str
    session_id: str
    grantee_user_id: str
    permission: SharePermission
    created_by_user_id: str
    created_at: datetime


@dataclass(slots=True)
class AuditLog:
    audit_id: str
    actor_user_id: str
    action: str
    resource_type: str
    resource_id: str
    created_at: datetime


@dataclass(slots=True)
class ChatMessage:
    message_id: str
    session_id: str
    role: ChatRole
    content: str
    created_at: datetime
    author_user_id: str | None = None
    sequence: int = 0
    revision: int = 0
    status: ChatMessageStatus = ChatMessageStatus.COMPLETE
    updated_at: datetime | None = None


@dataclass(slots=True)
class AvatarSession:
    session_id: str
    character_id: str
    owner_user_id: str
    visibility: SessionVisibility
    status: SessionStatus
    avatar_state: AvatarState
    created_at: datetime
    updated_at: datetime
    version: int = 0
    agent_id: str = "default"
    def start_media(self, now: datetime) -> None:
        self._transition_status(
            SessionStatus.MEDIA_STARTING,
            {SessionStatus.CREATED, SessionStatus.FAILED},
            now,
        )

    def media_ready(self, now: datetime) -> None:
        self._transition_status(
            SessionStatus.READY,
            {SessionStatus.MEDIA_STARTING},
            now,
        )
        self.avatar_state = AvatarState.IDLE

    def media_failed(self, now: datetime) -> None:
        if self.status is SessionStatus.CLOSED:
            return
        self.status = SessionStatus.FAILED
        self.avatar_state = AvatarState.IDLE
        self.updated_at = now

    def start_thinking(self, now: datetime) -> None:
        self._require_ready()
        self._transition_avatar(AvatarState.THINKING, {AvatarState.IDLE}, now)

    def start_speaking(self, now: datetime) -> None:
        self._require_ready()
        self._transition_avatar(AvatarState.SPEAKING, {AvatarState.THINKING}, now)

    def become_idle(self, now: datetime) -> None:
        self._require_ready()
        self._transition_avatar(
            AvatarState.IDLE,
            {AvatarState.THINKING, AvatarState.SPEAKING},
            now,
        )

    def close(self, now: datetime) -> None:
        if self.status is SessionStatus.CLOSED:
            return
        self.status = SessionStatus.CLOSED
        self.avatar_state = AvatarState.IDLE
        self.updated_at = now

    def _require_ready(self) -> None:
        if self.status is not SessionStatus.READY:
            raise InvalidStateTransition(self.status, SessionStatus.READY)

    def _transition_status(
        self,
        target: SessionStatus,
        allowed: set[SessionStatus],
        now: datetime,
    ) -> None:
        if self.status not in allowed:
            raise InvalidStateTransition(self.status, target)
        self.status = target
        self.updated_at = now

    def _transition_avatar(
        self,
        target: AvatarState,
        allowed: set[AvatarState],
        now: datetime,
    ) -> None:
        if self.avatar_state not in allowed:
            raise InvalidStateTransition(self.avatar_state, target)
        self.avatar_state = target
        self.updated_at = now


@dataclass(slots=True)
class AgentTurn:
    turn_id: str
    session_id: str
    user_id: str
    agent_id: str
    assistant_message_id: str
    status: AgentTurnStatus
    last_event_sequence: int
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    failure_code: str | None = None
    failure_detail: str | None = None
