from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from avatar_chat.domain.errors import InvalidStateTransition


class AvatarState(str, Enum):
    CREATED = "created"
    CONNECTED = "connected"
    IDLE = "idle"
    THINKING = "thinking"
    SPEAKING = "speaking"
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
    state: AvatarState
    created_at: datetime
    updated_at: datetime

    def connected(self, now: datetime) -> None:
        self._transition(AvatarState.CONNECTED, {AvatarState.CREATED}, now)
        self._transition(AvatarState.IDLE, {AvatarState.CONNECTED}, now)

    def start_thinking(self, now: datetime) -> None:
        self._transition(AvatarState.THINKING, {AvatarState.IDLE}, now)

    def start_speaking(self, now: datetime) -> None:
        self._transition(AvatarState.SPEAKING, {AvatarState.THINKING}, now)

    def become_idle(self, now: datetime) -> None:
        self._transition(
            AvatarState.IDLE,
            {AvatarState.THINKING, AvatarState.SPEAKING},
            now,
        )

    def close(self, now: datetime) -> None:
        if self.state is AvatarState.CLOSED:
            return
        self.state = AvatarState.CLOSED
        self.updated_at = now

    def _transition(
        self,
        target: AvatarState,
        allowed: set[AvatarState],
        now: datetime,
    ) -> None:
        if self.state not in allowed:
            raise InvalidStateTransition(self.state, target)
        self.state = target
        self.updated_at = now
