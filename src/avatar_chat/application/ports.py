from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Protocol

from avatar_chat.application.dto import (
    AccessTokenClaims,
    AgentAuthorizationScope,
    AgentRuntimeEvent,
    AgentTurnRequest,
    CharacterSummary,
    ChatOutputEvent,
    MediaAccess,
    MediaProvisioning,
    RealtimeOutboxEvent,
    RefreshTokenClaims,
    ReplyChunk,
    WebRtcAnswer,
    WebRtcOffer,
)
from avatar_chat.domain.models import (
    AgentTurn,
    AuditLog,
    AuthSession,
    AvatarSession,
    AvatarState,
    ChatMessage,
    SessionShare,
    User,
)


class SessionRepository(Protocol):
    async def get(self, session_id: str) -> AvatarSession | None: ...

    async def save(
        self,
        session: AvatarSession,
        expected_version: int | None = None,
    ) -> None: ...

    async def delete(self, session_id: str) -> None: ...

    async def list_for_user(self, user_id: str) -> tuple[AvatarSession, ...]: ...

    async def list_public(self, limit: int) -> tuple[AvatarSession, ...]: ...


class ConversationRepository(Protocol):
    async def append(self, message: ChatMessage) -> ChatMessage: ...

    async def update(
        self,
        message: ChatMessage,
        expected_revision: int,
    ) -> ChatMessage: ...

    async def list_recent(self, session_id: str, limit: int) -> tuple[ChatMessage, ...]: ...

    async def list_after(
        self,
        session_id: str,
        sequence: int,
        revision: int,
        limit: int,
    ) -> tuple[ChatMessage, ...]: ...


class AgentTurnRepository(Protocol):
    async def get(self, turn_id: str) -> AgentTurn | None: ...

    async def save(self, turn: AgentTurn) -> None: ...


class CharacterCatalogPort(Protocol):
    def get(self, character_id: str) -> CharacterSummary | None: ...

    def list(self) -> tuple[CharacterSummary, ...]: ...


class UserRepository(Protocol):
    async def get(self, user_id: str) -> User | None: ...

    async def get_by_email(self, normalized_email: str) -> User | None: ...

    async def get_by_username(self, normalized_username: str) -> User | None: ...

    async def save(self, user: User) -> None: ...


class AuthSessionRepository(Protocol):
    async def get(self, auth_session_id: str) -> AuthSession | None: ...

    async def get_by_token_hash(self, token_hash: str) -> AuthSession | None: ...

    async def save(self, session: AuthSession) -> None: ...

    async def replace(
        self,
        auth_session_id: str,
        expected_token_hash: str,
        replacement: AuthSession,
        replaced_at: datetime,
    ) -> bool: ...

    async def revoke(
        self,
        auth_session_id: str,
        revoked_at: datetime,
        replaced_by_session_id: str | None = None,
    ) -> None: ...

    async def revoke_all(self, user_id: str, revoked_at: datetime) -> None: ...


class SessionShareRepository(Protocol):
    async def get(self, session_id: str, user_id: str) -> SessionShare | None: ...

    async def list_for_session(self, session_id: str) -> tuple[SessionShare, ...]: ...

    async def list_for_user(self, user_id: str) -> tuple[SessionShare, ...]: ...

    async def save(self, share: SessionShare) -> None: ...

    async def delete(self, session_id: str, user_id: str) -> None: ...


class AuditLogRepository(Protocol):
    async def append(self, event: AuditLog) -> None: ...


class SessionMutationPort(Protocol):
    async def save_session(
        self,
        session: AvatarSession,
        expected_version: int,
        audit: AuditLog,
        outbox: RealtimeOutboxEvent,
    ) -> None: ...

    async def save_share(
        self,
        share: SessionShare,
        audit: AuditLog,
        outbox: RealtimeOutboxEvent,
    ) -> None: ...

    async def delete_share(
        self,
        session_id: str,
        user_id: str,
        audit: AuditLog,
        outbox: RealtimeOutboxEvent,
    ) -> None: ...


class RealtimeOutboxRepository(Protocol):
    async def list_pending(self, limit: int = 100) -> tuple[RealtimeOutboxEvent, ...]: ...

    async def mark_published(self, event_id: str, published_at: datetime) -> None: ...


class PasswordHasherPort(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, password: str, password_hash: str) -> bool: ...


class SecureTokenPort(Protocol):
    def generate(self) -> str: ...

    def hash(self, token: str) -> str: ...


RealtimeEventHandler = Callable[[str, ChatOutputEvent], Awaitable[None]]


class RealtimeEventBusPort(Protocol):
    async def subscribe(self, handler: RealtimeEventHandler) -> None: ...

    async def publish(self, session_id: str, event: ChatOutputEvent) -> None: ...

    async def shutdown(self) -> None: ...


class JwtTokenPort(Protocol):
    def issue_access(
        self,
        user_id: str,
        session_id: str,
        issued_at: datetime,
    ) -> str: ...

    def issue_refresh(
        self,
        user_id: str,
        session_id: str,
        family_id: str,
        issued_at: datetime,
    ) -> str: ...

    def decode_access(self, token: str) -> AccessTokenClaims: ...

    def decode_refresh(self, token: str) -> RefreshTokenClaims: ...


class AvatarStreamPort(Protocol):
    async def start(
        self,
        session_id: str,
        character_id: str,
        room_name: str,
        publisher_token: str,
    ) -> None: ...

    async def negotiate(
        self,
        session_id: str,
        character_id: str,
        offer: WebRtcOffer,
    ) -> WebRtcAnswer: ...

    async def change_state(
        self,
        session_id: str,
        state: AvatarState,
        state_version: int,
    ) -> None: ...

    async def speak(
        self,
        session_id: str,
        turn_id: str,
        utterance_id: str,
        text: str,
    ) -> None: ...

    async def close(self, session_id: str) -> None: ...

    async def shutdown(self) -> None: ...


class MediaRoomPort(Protocol):
    async def provision(
        self,
        session_id: str,
        viewer_user_id: str,
    ) -> MediaProvisioning: ...

    async def issue_access(self, session_id: str, user_id: str) -> MediaAccess: ...

    async def issue_public_access(
        self,
        session_id: str,
        viewer_id: str,
    ) -> MediaAccess: ...

    async def revoke_participant(self, session_id: str, user_id: str) -> None: ...

    async def revoke_participants_except(
        self,
        session_id: str,
        allowed_user_ids: tuple[str, ...],
    ) -> None: ...

    async def close(self, session_id: str) -> None: ...


class ReplyGeneratorPort(Protocol):
    def stream_reply(
        self,
        session_id: str,
        messages: tuple[ChatMessage, ...],
    ) -> AsyncIterator[ReplyChunk]: ...


class AgentRuntimePort(Protocol):
    def stream_turn(
        self,
        request: AgentTurnRequest,
        delegation_token: str,
    ) -> AsyncIterator[AgentRuntimeEvent]: ...

    async def cancel_turn(self, turn_id: str) -> bool: ...

    async def shutdown(self) -> None: ...


class AgentDelegationTokenPort(Protocol):
    def issue(
        self,
        user_id: str,
        session_id: str,
        turn_id: str,
        agent_id: str,
        tool_permissions: tuple[str, ...],
        knowledge_base_ids: tuple[str, ...],
        issued_at: datetime,
    ) -> str: ...


class AgentAuthorizationPort(Protocol):
    async def resolve(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
    ) -> AgentAuthorizationScope: ...


class SessionTurnLockLease(Protocol):
    async def ensure_valid(self) -> None: ...

    async def release(self) -> None: ...


class SessionTurnLockPort(Protocol):
    async def acquire(self, session_id: str) -> SessionTurnLockLease | None: ...


class IdGenerator(Protocol):
    def new_id(self) -> str: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
