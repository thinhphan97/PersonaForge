from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from avatar_chat.domain.errors import InvalidStateTransition


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


@dataclass(slots=True)
class ChatMessage:
    message_id: str
    session_id: str
    role: ChatRole
    content: str
    created_at: datetime


@dataclass(slots=True)
class AvatarSession:
    session_id: str
    character_id: str
    status: SessionStatus
    avatar_state: AvatarState
    created_at: datetime
    updated_at: datetime
    version: int = 0

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
