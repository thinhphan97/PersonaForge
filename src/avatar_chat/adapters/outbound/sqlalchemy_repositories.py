import asyncio
from dataclasses import replace
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    and_,
    delete,
    or_,
    select,
    update,
)
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from avatar_chat.application.dto import (
    ChatEventType,
    ChatOutputEvent,
    RealtimeOutboxEvent,
)
from avatar_chat.domain.errors import ConcurrentMessageUpdate, ConcurrentSessionUpdate
from avatar_chat.domain.models import (
    AuditLog,
    AuthSession,
    AvatarSession,
    AvatarState,
    ChatMessage,
    ChatMessageStatus,
    ChatRole,
    SessionShare,
    SessionStatus,
    SessionVisibility,
    SharePermission,
    User,
    UserStatus,
)


class Base(DeclarativeBase):
    pass


class AvatarSessionRow(Base):
    __tablename__ = "avatar_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    character_id: Mapped[str] = mapped_column(String(100))
    owner_user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.user_id"),
        index=True,
    )
    visibility: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(32))
    avatar_state: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChatMessageRow(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (UniqueConstraint("session_id", "sequence"),)

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str]
    sequence: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    message_status: Mapped[str] = mapped_column(String(16))
    author_user_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("users.user_id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class UserRow(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    normalized_email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64))
    normalized_username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuthSessionRow(Base):
    __tablename__ = "auth_sessions"

    auth_session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.user_id"), index=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SessionShareRow(Base):
    __tablename__ = "session_shares"
    __table_args__ = (UniqueConstraint("session_id", "grantee_user_id"),)

    share_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("avatar_sessions.session_id"), index=True
    )
    grantee_user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id"), index=True
    )
    permission: Mapped[str] = mapped_column(String(16))
    created_by_user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLogRow(Base):
    __tablename__ = "audit_logs"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.user_id"), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    resource_type: Mapped[str] = mapped_column(String(50))
    resource_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class RealtimeOutboxRow(Base):
    __tablename__ = "realtime_outbox"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, object | None]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )


class SqlAlchemyDatabase:
    def __init__(
        self,
        url: str,
        auto_create_schema: bool = True,
        startup_timeout_seconds: float = 15.0,
    ) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self._auto_create_schema = auto_create_schema
        self._startup_timeout_seconds = startup_timeout_seconds

    async def startup(self) -> None:
        if not self._auto_create_schema:
            return
        await asyncio.wait_for(
            self._create_schema(),
            timeout=self._startup_timeout_seconds,
        )

    async def _create_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def shutdown(self) -> None:
        await self.engine.dispose()


class SqlAlchemySessionRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, session_id: str) -> AvatarSession | None:
        async with self._sessions() as database:
            row = await database.get(AvatarSessionRow, session_id)
            if row is None:
                return None
            return AvatarSession(
                session_id=row.session_id,
                character_id=row.character_id,
                owner_user_id=row.owner_user_id,
                visibility=SessionVisibility(row.visibility),
                status=SessionStatus(row.status),
                avatar_state=AvatarState(row.avatar_state),
                created_at=row.created_at,
                updated_at=row.updated_at,
                version=row.version,
            )

    async def save(
        self,
        session: AvatarSession,
        expected_version: int | None = None,
    ) -> None:
        async with self._sessions() as database:
            if expected_version is None:
                database.add(
                    AvatarSessionRow(
                        session_id=session.session_id,
                        character_id=session.character_id,
                        owner_user_id=session.owner_user_id,
                        visibility=session.visibility.value,
                        status=session.status.value,
                        avatar_state=session.avatar_state.value,
                        version=session.version,
                        created_at=session.created_at,
                        updated_at=session.updated_at,
                    )
                )
            else:
                result = await database.execute(
                    update(AvatarSessionRow)
                    .where(
                        AvatarSessionRow.session_id == session.session_id,
                        AvatarSessionRow.version == expected_version,
                    )
                    .values(
                        character_id=session.character_id,
                        owner_user_id=session.owner_user_id,
                        visibility=session.visibility.value,
                        status=session.status.value,
                        avatar_state=session.avatar_state.value,
                        version=expected_version + 1,
                        updated_at=session.updated_at,
                    )
                )
                cursor = result  # SQLAlchemy returns CursorResult for UPDATE.
                if not isinstance(cursor, CursorResult) or cursor.rowcount != 1:
                    await database.rollback()
                    raise ConcurrentSessionUpdate(session.session_id)
            await database.commit()
            if expected_version is not None:
                session.version = expected_version + 1

    async def delete(self, session_id: str) -> None:
        async with self._sessions() as database:
            await database.execute(
                delete(AvatarSessionRow).where(AvatarSessionRow.session_id == session_id)
            )
            await database.commit()

    async def list_for_user(self, user_id: str) -> tuple[AvatarSession, ...]:
        return await self._list(
            select(AvatarSessionRow)
            .where(AvatarSessionRow.owner_user_id == user_id)
            .order_by(AvatarSessionRow.created_at.desc())
        )

    async def list_public(self, limit: int) -> tuple[AvatarSession, ...]:
        return await self._list(
            select(AvatarSessionRow)
            .where(AvatarSessionRow.visibility == SessionVisibility.PUBLIC.value)
            .order_by(AvatarSessionRow.created_at.desc())
            .limit(max(0, limit))
        )

    async def _list(
        self,
        statement: Select[tuple[AvatarSessionRow]],
    ) -> tuple[AvatarSession, ...]:
        async with self._sessions() as database:
            rows = list((await database.scalars(statement)).all())
        return tuple(
            AvatarSession(
                session_id=row.session_id,
                character_id=row.character_id,
                owner_user_id=row.owner_user_id,
                visibility=SessionVisibility(row.visibility),
                status=SessionStatus(row.status),
                avatar_state=AvatarState(row.avatar_state),
                created_at=row.created_at,
                updated_at=row.updated_at,
                version=row.version,
            )
            for row in rows
        )


class SqlAlchemyConversationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def append(self, message: ChatMessage) -> ChatMessage:
        async with self._sessions() as database:
            await database.scalar(
                select(AvatarSessionRow)
                .where(AvatarSessionRow.session_id == message.session_id)
                .with_for_update()
            )
            last_sequence = await database.scalar(
                select(ChatMessageRow.sequence)
                .where(ChatMessageRow.session_id == message.session_id)
                .order_by(ChatMessageRow.sequence.desc())
                .limit(1)
            )
            stored = replace(
                message,
                sequence=(last_sequence or 0) + 1,
                updated_at=message.updated_at or message.created_at,
            )
            database.add(
                ChatMessageRow(
                    message_id=stored.message_id,
                    session_id=stored.session_id,
                    role=stored.role.value,
                    content=stored.content,
                    sequence=stored.sequence,
                    revision=stored.revision,
                    message_status=stored.status.value,
                    author_user_id=stored.author_user_id,
                    created_at=stored.created_at,
                    updated_at=stored.updated_at,
                )
            )
            await database.commit()
            return stored

    async def update(
        self,
        message: ChatMessage,
        expected_revision: int,
    ) -> ChatMessage:
        async with self._sessions() as database:
            result = await database.execute(
                update(ChatMessageRow)
                .where(
                    ChatMessageRow.message_id == message.message_id,
                    ChatMessageRow.session_id == message.session_id,
                    ChatMessageRow.revision == expected_revision,
                )
                .values(
                    content=message.content,
                    revision=message.revision,
                    message_status=message.status.value,
                    updated_at=message.updated_at or message.created_at,
                )
            )
            if not isinstance(result, CursorResult) or result.rowcount != 1:
                await database.rollback()
                raise ConcurrentMessageUpdate(message.message_id)
            await database.commit()
            return message

    async def list_recent(self, session_id: str, limit: int) -> tuple[ChatMessage, ...]:
        statement = (
            select(ChatMessageRow)
            .where(ChatMessageRow.session_id == session_id)
            .order_by(ChatMessageRow.sequence.desc())
            .limit(max(0, limit))
        )
        async with self._sessions() as database:
            rows = list((await database.scalars(statement)).all())
        rows.reverse()
        return tuple(_message_from_row(row) for row in rows)

    async def list_after(
        self,
        session_id: str,
        sequence: int,
        revision: int,
        limit: int,
    ) -> tuple[ChatMessage, ...]:
        statement = (
            select(ChatMessageRow)
            .where(
                ChatMessageRow.session_id == session_id,
                or_(
                    ChatMessageRow.sequence > sequence,
                    and_(
                        ChatMessageRow.sequence == sequence,
                        ChatMessageRow.revision > revision,
                    ),
                ),
            )
            .order_by(ChatMessageRow.sequence.asc())
            .limit(max(0, limit))
        )
        async with self._sessions() as database:
            rows = list((await database.scalars(statement)).all())
        return tuple(_message_from_row(row) for row in rows)


class SqlAlchemyUserRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, user_id: str) -> User | None:
        async with self._sessions() as database:
            return _user_from_row(await database.get(UserRow, user_id))

    async def get_by_email(self, normalized_email: str) -> User | None:
        return await self._find(UserRow.normalized_email == normalized_email)

    async def get_by_username(self, normalized_username: str) -> User | None:
        return await self._find(UserRow.normalized_username == normalized_username)

    async def _find(self, condition: ColumnElement[bool]) -> User | None:
        async with self._sessions() as database:
            row = await database.scalar(select(UserRow).where(condition))
            return _user_from_row(row)

    async def save(self, user: User) -> None:
        async with self._sessions() as database:
            existing = await database.get(UserRow, user.user_id)
            if existing is None:
                database.add(
                    UserRow(
                        user_id=user.user_id,
                        email=user.email,
                        normalized_email=user.normalized_email,
                        username=user.username,
                        normalized_username=user.normalized_username,
                        password_hash=user.password_hash,
                        status=user.status.value,
                        created_at=user.created_at,
                        updated_at=user.updated_at,
                    )
                )
            else:
                existing.email = user.email
                existing.normalized_email = user.normalized_email
                existing.username = user.username
                existing.normalized_username = user.normalized_username
                existing.password_hash = user.password_hash
                existing.status = user.status.value
                existing.updated_at = user.updated_at
            await database.commit()


class SqlAlchemyAuthSessionRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, auth_session_id: str) -> AuthSession | None:
        async with self._sessions() as database:
            row = await database.get(AuthSessionRow, auth_session_id)
            return _auth_session_from_row(row)

    async def get_by_token_hash(self, token_hash: str) -> AuthSession | None:
        async with self._sessions() as database:
            row = await database.scalar(
                select(AuthSessionRow).where(AuthSessionRow.token_hash == token_hash)
            )
            return _auth_session_from_row(row)

    async def save(self, session: AuthSession) -> None:
        async with self._sessions() as database:
            database.add(
                AuthSessionRow(
                    auth_session_id=session.auth_session_id,
                    user_id=session.user_id,
                    family_id=session.family_id,
                    token_hash=session.token_hash,
                    csrf_token=session.csrf_token,
                    expires_at=session.expires_at,
                    created_at=session.created_at,
                    last_seen_at=session.last_seen_at,
                    revoked_at=session.revoked_at,
                    replaced_by_session_id=session.replaced_by_session_id,
                )
            )
            await database.commit()

    async def replace(
        self,
        auth_session_id: str,
        expected_token_hash: str,
        replacement: AuthSession,
        replaced_at: datetime,
    ) -> bool:
        async with self._sessions() as database:
            result = await database.execute(
                update(AuthSessionRow)
                .where(
                    AuthSessionRow.auth_session_id == auth_session_id,
                    AuthSessionRow.token_hash == expected_token_hash,
                    AuthSessionRow.revoked_at.is_(None),
                )
                .values(
                    revoked_at=replaced_at,
                    replaced_by_session_id=replacement.auth_session_id,
                )
            )
            if not isinstance(result, CursorResult) or result.rowcount != 1:
                await database.rollback()
                return False
            database.add(
                AuthSessionRow(
                    auth_session_id=replacement.auth_session_id,
                    user_id=replacement.user_id,
                    family_id=replacement.family_id,
                    token_hash=replacement.token_hash,
                    csrf_token=replacement.csrf_token,
                    expires_at=replacement.expires_at,
                    created_at=replacement.created_at,
                    last_seen_at=replacement.last_seen_at,
                    revoked_at=replacement.revoked_at,
                    replaced_by_session_id=replacement.replaced_by_session_id,
                )
            )
            await database.commit()
            return True

    async def revoke(
        self,
        auth_session_id: str,
        revoked_at: datetime,
        replaced_by_session_id: str | None = None,
    ) -> None:
        async with self._sessions() as database:
            await database.execute(
                update(AuthSessionRow)
                .where(AuthSessionRow.auth_session_id == auth_session_id)
                .values(
                    revoked_at=revoked_at,
                    replaced_by_session_id=replaced_by_session_id,
                )
            )
            await database.commit()

    async def revoke_all(self, user_id: str, revoked_at: datetime) -> None:
        async with self._sessions() as database:
            await database.execute(
                update(AuthSessionRow)
                .where(AuthSessionRow.user_id == user_id)
                .values(revoked_at=revoked_at)
            )
            await database.commit()


class SqlAlchemySessionShareRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, session_id: str, user_id: str) -> SessionShare | None:
        async with self._sessions() as database:
            row = await database.scalar(
                select(SessionShareRow).where(
                    SessionShareRow.session_id == session_id,
                    SessionShareRow.grantee_user_id == user_id,
                )
            )
            return _share_from_row(row)

    async def list_for_session(self, session_id: str) -> tuple[SessionShare, ...]:
        return await self._list(SessionShareRow.session_id == session_id)

    async def list_for_user(self, user_id: str) -> tuple[SessionShare, ...]:
        return await self._list(SessionShareRow.grantee_user_id == user_id)

    async def _list(self, condition: ColumnElement[bool]) -> tuple[SessionShare, ...]:
        async with self._sessions() as database:
            rows = list(
                (await database.scalars(select(SessionShareRow).where(condition))).all()
            )
        return tuple(share for row in rows if (share := _share_from_row(row)) is not None)

    async def save(self, share: SessionShare) -> None:
        async with self._sessions() as database:
            existing = await database.scalar(
                select(SessionShareRow).where(
                    SessionShareRow.session_id == share.session_id,
                    SessionShareRow.grantee_user_id == share.grantee_user_id,
                )
            )
            if existing is None:
                database.add(
                    SessionShareRow(
                        share_id=share.share_id,
                        session_id=share.session_id,
                        grantee_user_id=share.grantee_user_id,
                        permission=share.permission.value,
                        created_by_user_id=share.created_by_user_id,
                        created_at=share.created_at,
                    )
                )
            else:
                existing.permission = share.permission.value
            await database.commit()

    async def delete(self, session_id: str, user_id: str) -> None:
        async with self._sessions() as database:
            await database.execute(
                delete(SessionShareRow).where(
                    SessionShareRow.session_id == session_id,
                    SessionShareRow.grantee_user_id == user_id,
                )
            )
            await database.commit()


class SqlAlchemySessionMutationUnitOfWork:
    """Commit an administration mutation, its audit record and outbox atomically."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save_session(
        self,
        session: AvatarSession,
        expected_version: int,
        audit: AuditLog,
        outbox: RealtimeOutboxEvent,
    ) -> None:
        async with self._sessions() as database:
            result = await database.execute(
                update(AvatarSessionRow)
                .where(
                    AvatarSessionRow.session_id == session.session_id,
                    AvatarSessionRow.version == expected_version,
                )
                .values(
                    character_id=session.character_id,
                    owner_user_id=session.owner_user_id,
                    visibility=session.visibility.value,
                    status=session.status.value,
                    avatar_state=session.avatar_state.value,
                    version=expected_version + 1,
                    updated_at=session.updated_at,
                )
            )
            if not isinstance(result, CursorResult) or result.rowcount != 1:
                await database.rollback()
                raise ConcurrentSessionUpdate(session.session_id)
            database.add(_audit_row(audit))
            database.add(_outbox_row(outbox))
            await database.commit()
            session.version = expected_version + 1

    async def save_share(
        self,
        share: SessionShare,
        audit: AuditLog,
        outbox: RealtimeOutboxEvent,
    ) -> None:
        async with self._sessions() as database:
            existing = await database.scalar(
                select(SessionShareRow).where(
                    SessionShareRow.session_id == share.session_id,
                    SessionShareRow.grantee_user_id == share.grantee_user_id,
                )
            )
            if existing is None:
                database.add(
                    SessionShareRow(
                        share_id=share.share_id,
                        session_id=share.session_id,
                        grantee_user_id=share.grantee_user_id,
                        permission=share.permission.value,
                        created_by_user_id=share.created_by_user_id,
                        created_at=share.created_at,
                    )
                )
            else:
                existing.permission = share.permission.value
            database.add(_audit_row(audit))
            database.add(_outbox_row(outbox))
            await database.commit()

    async def delete_share(
        self,
        session_id: str,
        user_id: str,
        audit: AuditLog,
        outbox: RealtimeOutboxEvent,
    ) -> None:
        async with self._sessions() as database:
            await database.execute(
                delete(SessionShareRow).where(
                    SessionShareRow.session_id == session_id,
                    SessionShareRow.grantee_user_id == user_id,
                )
            )
            database.add(_audit_row(audit))
            database.add(_outbox_row(outbox))
            await database.commit()

    async def list_pending(self, limit: int = 100) -> tuple[RealtimeOutboxEvent, ...]:
        async with self._sessions() as database:
            rows = list(
                (
                    await database.scalars(
                        select(RealtimeOutboxRow)
                        .where(RealtimeOutboxRow.published_at.is_(None))
                        .order_by(RealtimeOutboxRow.created_at)
                        .limit(max(0, limit))
                    )
                ).all()
            )
        return tuple(_outbox_from_row(row) for row in rows)

    async def mark_published(self, event_id: str, published_at: datetime) -> None:
        async with self._sessions() as database:
            await database.execute(
                update(RealtimeOutboxRow)
                .where(
                    RealtimeOutboxRow.event_id == event_id,
                    RealtimeOutboxRow.published_at.is_(None),
                )
                .values(published_at=published_at)
            )
            await database.commit()


class SqlAlchemyAuditLogRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def append(self, event: AuditLog) -> None:
        async with self._sessions() as database:
            database.add(
                AuditLogRow(
                    audit_id=event.audit_id,
                    actor_user_id=event.actor_user_id,
                    action=event.action,
                    resource_type=event.resource_type,
                    resource_id=event.resource_id,
                    created_at=event.created_at,
                )
            )
            await database.commit()


def _user_from_row(row: UserRow | None) -> User | None:
    if row is None:
        return None
    return User(
        user_id=row.user_id,
        email=row.email,
        normalized_email=row.normalized_email,
        username=row.username,
        normalized_username=row.normalized_username,
        password_hash=row.password_hash,
        status=UserStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _message_from_row(row: ChatMessageRow) -> ChatMessage:
    return ChatMessage(
        message_id=row.message_id,
        session_id=row.session_id,
        role=ChatRole(row.role),
        content=row.content,
        created_at=row.created_at,
        author_user_id=row.author_user_id,
        sequence=row.sequence,
        revision=row.revision,
        status=ChatMessageStatus(row.message_status),
        updated_at=row.updated_at,
    )


def _auth_session_from_row(row: AuthSessionRow | None) -> AuthSession | None:
    if row is None:
        return None
    return AuthSession(
        auth_session_id=row.auth_session_id,
        user_id=row.user_id,
        family_id=row.family_id,
        token_hash=row.token_hash,
        csrf_token=row.csrf_token,
        expires_at=row.expires_at,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        revoked_at=row.revoked_at,
        replaced_by_session_id=row.replaced_by_session_id,
    )


def _share_from_row(row: SessionShareRow | None) -> SessionShare | None:
    if row is None:
        return None
    return SessionShare(
        share_id=row.share_id,
        session_id=row.session_id,
        grantee_user_id=row.grantee_user_id,
        permission=SharePermission(row.permission),
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
    )


def _audit_row(event: AuditLog) -> AuditLogRow:
    return AuditLogRow(
        audit_id=event.audit_id,
        actor_user_id=event.actor_user_id,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        created_at=event.created_at,
    )


def _outbox_row(item: RealtimeOutboxEvent) -> RealtimeOutboxRow:
    event = item.event
    return RealtimeOutboxRow(
        event_id=item.event_id,
        session_id=item.session_id,
        payload={
            "type": event.type.value,
            "content": event.content,
            "avatar_state": event.avatar_state.value if event.avatar_state else None,
            "message_id": event.message_id,
            "sequence": event.sequence,
            "revision": event.revision,
            "author_user_id": event.author_user_id,
            "session_version": event.session_version,
        },
        created_at=item.created_at,
        published_at=None,
    )


def _outbox_from_row(row: RealtimeOutboxRow) -> RealtimeOutboxEvent:
    payload = row.payload
    avatar_state = payload.get("avatar_state")
    return RealtimeOutboxEvent(
        event_id=row.event_id,
        session_id=row.session_id,
        event=ChatOutputEvent(
            type=ChatEventType(str(payload["type"])),
            content=_optional_string(payload.get("content")),
            avatar_state=AvatarState(str(avatar_state)) if avatar_state else None,
            message_id=_optional_string(payload.get("message_id")),
            sequence=_optional_int(payload.get("sequence")),
            revision=_optional_int(payload.get("revision")),
            author_user_id=_optional_string(payload.get("author_user_id")),
            session_version=_optional_int(payload.get("session_version")),
        ),
        created_at=row.created_at,
    )


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: object) -> int | None:
    return int(str(value)) if value is not None else None
