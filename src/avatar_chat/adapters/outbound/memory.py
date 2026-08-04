import asyncio
from collections import defaultdict
from copy import deepcopy
from datetime import datetime

from avatar_chat.application.dto import RealtimeOutboxEvent
from avatar_chat.domain.errors import ConcurrentMessageUpdate, ConcurrentSessionUpdate
from avatar_chat.domain.models import (
    AgentTurn,
    AuditLog,
    AuthSession,
    AvatarSession,
    ChatMessage,
    SessionShare,
    SessionVisibility,
    User,
)


class InMemoryAgentTurnRepository:
    def __init__(self) -> None:
        self._items: dict[str, AgentTurn] = {}
        self._lock = asyncio.Lock()

    async def get(self, turn_id: str) -> AgentTurn | None:
        async with self._lock:
            turn = self._items.get(turn_id)
            return deepcopy(turn) if turn else None

    async def save(self, turn: AgentTurn) -> None:
        async with self._lock:
            self._items[turn.turn_id] = deepcopy(turn)


class InMemorySessionRepository:
    def __init__(self) -> None:
        self._items: dict[str, AvatarSession] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> AvatarSession | None:
        async with self._lock:
            session = self._items.get(session_id)
            return deepcopy(session) if session else None

    async def save(
        self,
        session: AvatarSession,
        expected_version: int | None = None,
    ) -> None:
        async with self._lock:
            current = self._items.get(session.session_id)
            if expected_version is not None:
                if current is None or current.version != expected_version:
                    raise ConcurrentSessionUpdate(session.session_id)
                session.version = expected_version + 1
            elif current is not None:
                raise ConcurrentSessionUpdate(session.session_id)
            self._items[session.session_id] = deepcopy(session)

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._items.pop(session_id, None)

    async def list_for_user(self, user_id: str) -> tuple[AvatarSession, ...]:
        async with self._lock:
            return tuple(
                deepcopy(session)
                for session in self._items.values()
                if session.owner_user_id == user_id
            )

    async def list_public(self, limit: int) -> tuple[AvatarSession, ...]:
        async with self._lock:
            sessions = [
                session
                for session in self._items.values()
                if session.visibility is SessionVisibility.PUBLIC
            ]
            sessions.sort(key=lambda item: item.created_at, reverse=True)
            return tuple(deepcopy(sessions[: max(0, limit)]))


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self._items: dict[str, list[ChatMessage]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def append(self, message: ChatMessage) -> ChatMessage:
        async with self._lock:
            stored = deepcopy(message)
            stored.sequence = len(self._items[message.session_id]) + 1
            stored.updated_at = stored.updated_at or stored.created_at
            self._items[message.session_id].append(stored)
            return deepcopy(stored)

    async def update(
        self,
        message: ChatMessage,
        expected_revision: int,
    ) -> ChatMessage:
        async with self._lock:
            for index, current in enumerate(self._items.get(message.session_id, [])):
                if current.message_id != message.message_id:
                    continue
                if current.revision != expected_revision:
                    raise ConcurrentMessageUpdate(message.message_id)
                stored = deepcopy(message)
                self._items[message.session_id][index] = stored
                return deepcopy(stored)
        raise ConcurrentMessageUpdate(message.message_id)

    async def list_recent(self, session_id: str, limit: int) -> tuple[ChatMessage, ...]:
        async with self._lock:
            selected = self._items.get(session_id, [])[-max(0, limit) :]
            return tuple(deepcopy(selected))

    async def list_after(
        self,
        session_id: str,
        sequence: int,
        revision: int,
        limit: int,
    ) -> tuple[ChatMessage, ...]:
        async with self._lock:
            selected = [
                message
                for message in self._items.get(session_id, [])
                if message.sequence > sequence
                or (message.sequence == sequence and message.revision > revision)
            ][: max(0, limit)]
            return tuple(deepcopy(selected))


class InMemoryUserRepository:
    def __init__(self) -> None:
        self._items: dict[str, User] = {}
        self._lock = asyncio.Lock()

    async def get(self, user_id: str) -> User | None:
        async with self._lock:
            user = self._items.get(user_id)
            return deepcopy(user) if user else None

    async def get_by_email(self, normalized_email: str) -> User | None:
        async with self._lock:
            return next(
                (
                    deepcopy(user)
                    for user in self._items.values()
                    if user.normalized_email == normalized_email
                ),
                None,
            )

    async def get_by_username(self, normalized_username: str) -> User | None:
        async with self._lock:
            return next(
                (
                    deepcopy(user)
                    for user in self._items.values()
                    if user.normalized_username == normalized_username
                ),
                None,
            )

    async def save(self, user: User) -> None:
        async with self._lock:
            self._items[user.user_id] = deepcopy(user)


class InMemoryAuthSessionRepository:
    def __init__(self) -> None:
        self._items: dict[str, AuthSession] = {}
        self._lock = asyncio.Lock()

    async def get(self, auth_session_id: str) -> AuthSession | None:
        async with self._lock:
            session = self._items.get(auth_session_id)
            return deepcopy(session) if session else None

    async def get_by_token_hash(self, token_hash: str) -> AuthSession | None:
        async with self._lock:
            session = next(
                (
                    item
                    for item in self._items.values()
                    if item.token_hash == token_hash
                ),
                None,
            )
            return deepcopy(session) if session else None

    async def save(self, session: AuthSession) -> None:
        async with self._lock:
            self._items[session.auth_session_id] = deepcopy(session)

    async def replace(
        self,
        auth_session_id: str,
        expected_token_hash: str,
        replacement: AuthSession,
        replaced_at: datetime,
    ) -> bool:
        async with self._lock:
            current = self._items.get(auth_session_id)
            if (
                current is None
                or current.revoked_at is not None
                or current.token_hash != expected_token_hash
            ):
                return False
            current.revoked_at = replaced_at
            current.replaced_by_session_id = replacement.auth_session_id
            self._items[replacement.auth_session_id] = deepcopy(replacement)
            return True

    async def revoke(
        self,
        auth_session_id: str,
        revoked_at: datetime,
        replaced_by_session_id: str | None = None,
    ) -> None:
        async with self._lock:
            session = self._items.get(auth_session_id)
            if session is not None:
                session.revoked_at = revoked_at
                session.replaced_by_session_id = replaced_by_session_id

    async def revoke_all(self, user_id: str, revoked_at: datetime) -> None:
        async with self._lock:
            for session in self._items.values():
                if session.user_id == user_id:
                    session.revoked_at = revoked_at


class InMemorySessionShareRepository:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], SessionShare] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: str, user_id: str) -> SessionShare | None:
        async with self._lock:
            share = self._items.get((session_id, user_id))
            return deepcopy(share) if share else None

    async def list_for_session(self, session_id: str) -> tuple[SessionShare, ...]:
        async with self._lock:
            return tuple(
                deepcopy(share)
                for share in self._items.values()
                if share.session_id == session_id
            )

    async def list_for_user(self, user_id: str) -> tuple[SessionShare, ...]:
        async with self._lock:
            return tuple(
                deepcopy(share)
                for share in self._items.values()
                if share.grantee_user_id == user_id
            )

    async def save(self, share: SessionShare) -> None:
        async with self._lock:
            self._items[(share.session_id, share.grantee_user_id)] = deepcopy(share)

    async def delete(self, session_id: str, user_id: str) -> None:
        async with self._lock:
            self._items.pop((session_id, user_id), None)


class InMemoryAuditLogRepository:
    def __init__(self) -> None:
        self.items: list[AuditLog] = []
        self._lock = asyncio.Lock()

    async def append(self, event: AuditLog) -> None:
        async with self._lock:
            self.items.append(deepcopy(event))


class InMemorySessionMutationUnitOfWork:
    """Atomic administration mutations plus an at-least-once realtime outbox."""

    def __init__(
        self,
        sessions: InMemorySessionRepository,
        shares: InMemorySessionShareRepository,
        audit: InMemoryAuditLogRepository,
    ) -> None:
        self._sessions = sessions
        self._shares = shares
        self._audit = audit
        self._outbox: dict[str, RealtimeOutboxEvent] = {}
        self._lock = asyncio.Lock()

    async def save_session(
        self,
        session: AvatarSession,
        expected_version: int,
        audit: AuditLog,
        outbox: RealtimeOutboxEvent,
    ) -> None:
        async with self._lock, self._sessions._lock, self._audit._lock:
            current = self._sessions._items.get(session.session_id)
            if current is None or current.version != expected_version:
                raise ConcurrentSessionUpdate(session.session_id)
            session.version = expected_version + 1
            self._sessions._items[session.session_id] = deepcopy(session)
            self._audit.items.append(deepcopy(audit))
            self._outbox[outbox.event_id] = deepcopy(outbox)

    async def save_share(
        self,
        share: SessionShare,
        audit: AuditLog,
        outbox: RealtimeOutboxEvent,
    ) -> None:
        async with self._lock, self._shares._lock, self._audit._lock:
            self._shares._items[(share.session_id, share.grantee_user_id)] = deepcopy(
                share
            )
            self._audit.items.append(deepcopy(audit))
            self._outbox[outbox.event_id] = deepcopy(outbox)

    async def delete_share(
        self,
        session_id: str,
        user_id: str,
        audit: AuditLog,
        outbox: RealtimeOutboxEvent,
    ) -> None:
        async with self._lock, self._shares._lock, self._audit._lock:
            self._shares._items.pop((session_id, user_id), None)
            self._audit.items.append(deepcopy(audit))
            self._outbox[outbox.event_id] = deepcopy(outbox)

    async def list_pending(self, limit: int = 100) -> tuple[RealtimeOutboxEvent, ...]:
        async with self._lock:
            values = sorted(self._outbox.values(), key=lambda item: item.created_at)
            return tuple(deepcopy(values[: max(0, limit)]))

    async def mark_published(self, event_id: str, published_at: datetime) -> None:
        del published_at
        async with self._lock:
            self._outbox.pop(event_id, None)
