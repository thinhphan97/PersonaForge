from datetime import datetime

from sqlalchemy import DateTime, String, delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from avatar_chat.domain.models import AvatarSession, AvatarState, ChatMessage, ChatRole


class Base(DeclarativeBase):
    pass


class AvatarSessionRow(Base):
    __tablename__ = "avatar_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    character_id: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChatMessageRow(Base):
    __tablename__ = "chat_messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SqlAlchemyDatabase:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def startup(self) -> None:
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
                state=AvatarState(row.state),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    async def save(self, session: AvatarSession) -> None:
        async with self._sessions() as database:
            await database.merge(
                AvatarSessionRow(
                    session_id=session.session_id,
                    character_id=session.character_id,
                    state=session.state.value,
                    created_at=session.created_at,
                    updated_at=session.updated_at,
                )
            )
            await database.commit()

    async def delete(self, session_id: str) -> None:
        async with self._sessions() as database:
            await database.execute(
                delete(AvatarSessionRow).where(AvatarSessionRow.session_id == session_id)
            )
            await database.commit()


class SqlAlchemyConversationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def append(self, message: ChatMessage) -> None:
        async with self._sessions() as database:
            database.add(
                ChatMessageRow(
                    message_id=message.message_id,
                    session_id=message.session_id,
                    role=message.role.value,
                    content=message.content,
                    created_at=message.created_at,
                )
            )
            await database.commit()

    async def list_recent(self, session_id: str, limit: int) -> tuple[ChatMessage, ...]:
        statement = (
            select(ChatMessageRow)
            .where(ChatMessageRow.session_id == session_id)
            .order_by(ChatMessageRow.created_at.desc(), ChatMessageRow.message_id.desc())
            .limit(max(0, limit))
        )
        async with self._sessions() as database:
            rows = list((await database.scalars(statement)).all())
        rows.reverse()
        return tuple(
            ChatMessage(
                message_id=row.message_id,
                session_id=row.session_id,
                role=ChatRole(row.role),
                content=row.content,
                created_at=row.created_at,
            )
            for row in rows
        )
