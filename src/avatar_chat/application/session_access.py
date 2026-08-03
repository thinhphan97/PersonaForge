import logging
from dataclasses import dataclass

from avatar_chat.application.dto import SessionSummary, ShareSummary
from avatar_chat.application.ports import (
    AuditLogRepository,
    Clock,
    IdGenerator,
    MediaRoomPort,
    SessionRepository,
    SessionShareRepository,
    UserRepository,
)
from avatar_chat.domain.errors import (
    AccessDenied,
    SessionClosed,
    SessionNotFound,
    UserNotFound,
)
from avatar_chat.domain.models import (
    AuditLog,
    AvatarSession,
    SessionAction,
    SessionShare,
    SessionStatus,
    SessionVisibility,
    SharePermission,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SessionAccessPolicy:
    shares: SessionShareRepository

    async def require(
        self,
        user_id: str,
        session: AvatarSession,
        action: SessionAction,
    ) -> None:
        if session.owner_user_id == user_id:
            return
        share = await self.shares.get(session.session_id, user_id)
        if action is SessionAction.VIEW:
            if session.visibility is SessionVisibility.PUBLIC or share is not None:
                return
        elif (
            action is SessionAction.CHAT
            and share is not None
            and share.permission is SharePermission.INTERACTOR
        ):
            return
        raise AccessDenied()


@dataclass(slots=True)
class ListOwnedSessions:
    sessions: SessionRepository

    async def execute(self, user_id: str) -> tuple[SessionSummary, ...]:
        return tuple(
            _summary(session, "owner", True)
            for session in await self.sessions.list_for_user(user_id)
        )


@dataclass(slots=True)
class ListSharedSessions:
    sessions: SessionRepository
    shares: SessionShareRepository

    async def execute(self, user_id: str) -> tuple[SessionSummary, ...]:
        result: list[SessionSummary] = []
        for share in await self.shares.list_for_user(user_id):
            session = await self.sessions.get(share.session_id)
            if session is not None:
                result.append(
                    _summary(
                        session,
                        share.permission.value,
                        share.permission is SharePermission.INTERACTOR,
                    )
                )
        return tuple(result)


@dataclass(slots=True)
class ListPublicSessions:
    sessions: SessionRepository
    shares: SessionShareRepository

    async def execute(
        self,
        user_id: str,
        limit: int = 50,
    ) -> tuple[SessionSummary, ...]:
        result: list[SessionSummary] = []
        for session in await self.sessions.list_public(limit):
            if session.owner_user_id == user_id:
                result.append(_summary(session, "owner", True))
                continue
            share = await self.shares.get(session.session_id, user_id)
            if share is None:
                result.append(_summary(session, "public", False))
            else:
                result.append(
                    _summary(
                        session,
                        share.permission.value,
                        share.permission is SharePermission.INTERACTOR,
                    )
                )
        return tuple(result)


@dataclass(slots=True)
class GetSessionSummary:
    sessions: SessionRepository
    shares: SessionShareRepository
    access: SessionAccessPolicy

    async def execute(self, user_id: str, session_id: str) -> SessionSummary:
        session = await _session(self.sessions, session_id)
        await self.access.require(user_id, session, SessionAction.VIEW)
        if session.owner_user_id == user_id:
            return _summary(session, "owner", True)
        share = await self.shares.get(session_id, user_id)
        if share is not None:
            return _summary(
                session,
                share.permission.value,
                share.permission is SharePermission.INTERACTOR,
            )
        return _summary(session, "public", False)


@dataclass(slots=True)
class ChangeSessionVisibility:
    sessions: SessionRepository
    access: SessionAccessPolicy
    audit: AuditLogRepository
    ids: IdGenerator
    clock: Clock
    shares: SessionShareRepository
    rooms: MediaRoomPort | None = None

    async def execute(
        self,
        actor_user_id: str,
        session_id: str,
        visibility: SessionVisibility,
    ) -> SessionSummary:
        session = await _session(self.sessions, session_id)
        await self.access.require(actor_user_id, session, SessionAction.MANAGE)
        _require_open_session(session)
        expected_version = session.version
        session.visibility = visibility
        session.updated_at = self.clock.now()
        await self.sessions.save(session, expected_version)
        if visibility is SessionVisibility.PRIVATE and self.rooms is not None:
            allowed = (session.owner_user_id,) + tuple(
                share.grantee_user_id
                for share in await self.shares.list_for_session(session_id)
            )
            try:
                await self.rooms.revoke_participants_except(session_id, allowed)
            except Exception:
                logger.exception(
                    "Could not revoke public media viewers session=%s",
                    session_id,
                )
        await _audit(
            self.audit,
            self.ids,
            self.clock,
            actor_user_id,
            "session.visibility.changed",
            session_id,
        )
        return _summary(session, "owner", True)


@dataclass(slots=True)
class ShareSession:
    sessions: SessionRepository
    users: UserRepository
    shares: SessionShareRepository
    access: SessionAccessPolicy
    audit: AuditLogRepository
    ids: IdGenerator
    clock: Clock

    async def execute(
        self,
        actor_user_id: str,
        session_id: str,
        username: str,
        permission: SharePermission,
    ) -> ShareSummary:
        session = await _session(self.sessions, session_id)
        await self.access.require(actor_user_id, session, SessionAction.MANAGE)
        _require_open_session(session)
        user = await self.users.get_by_username(username.strip().casefold())
        if user is None:
            raise UserNotFound(username)
        if user.user_id == actor_user_id:
            raise AccessDenied()
        share = SessionShare(
            share_id=self.ids.new_id(),
            session_id=session_id,
            grantee_user_id=user.user_id,
            permission=permission,
            created_by_user_id=actor_user_id,
            created_at=self.clock.now(),
        )
        await self.shares.save(share)
        await _audit(
            self.audit,
            self.ids,
            self.clock,
            actor_user_id,
            "session.shared",
            session_id,
        )
        return ShareSummary(user.user_id, user.username, permission)


@dataclass(slots=True)
class ListSessionShares:
    sessions: SessionRepository
    users: UserRepository
    shares: SessionShareRepository
    access: SessionAccessPolicy

    async def execute(
        self,
        actor_user_id: str,
        session_id: str,
    ) -> tuple[ShareSummary, ...]:
        session = await _session(self.sessions, session_id)
        await self.access.require(actor_user_id, session, SessionAction.MANAGE)
        result: list[ShareSummary] = []
        for share in await self.shares.list_for_session(session_id):
            user = await self.users.get(share.grantee_user_id)
            if user is not None:
                result.append(ShareSummary(user.user_id, user.username, share.permission))
        return tuple(result)


@dataclass(slots=True)
class RevokeSessionShare:
    sessions: SessionRepository
    shares: SessionShareRepository
    access: SessionAccessPolicy
    audit: AuditLogRepository
    ids: IdGenerator
    clock: Clock
    rooms: MediaRoomPort | None = None

    async def execute(
        self,
        actor_user_id: str,
        session_id: str,
        user_id: str,
    ) -> None:
        session = await _session(self.sessions, session_id)
        await self.access.require(actor_user_id, session, SessionAction.MANAGE)
        _require_open_session(session)
        await self.shares.delete(session_id, user_id)
        if self.rooms is not None:
            try:
                await self.rooms.revoke_participant(session_id, user_id)
            except Exception:
                logger.exception(
                    "Could not revoke media participant session=%s user=%s",
                    session_id,
                    user_id,
                )
        await _audit(
            self.audit,
            self.ids,
            self.clock,
            actor_user_id,
            "session.share.revoked",
            session_id,
        )


def _summary(
    session: AvatarSession,
    access_role: str,
    can_chat: bool,
) -> SessionSummary:
    return SessionSummary(
        session.session_id,
        session.character_id,
        session.owner_user_id,
        session.visibility,
        session.status,
        access_role,
        can_chat and session.status is not SessionStatus.CLOSED,
        session.version,
    )


async def _session(sessions: SessionRepository, session_id: str) -> AvatarSession:
    session = await sessions.get(session_id)
    if session is None:
        raise SessionNotFound(session_id)
    return session


def _require_open_session(session: AvatarSession) -> None:
    if session.status is SessionStatus.CLOSED:
        raise SessionClosed(session.session_id)


async def _audit(
    repository: AuditLogRepository,
    ids: IdGenerator,
    clock: Clock,
    actor_user_id: str,
    action: str,
    resource_id: str,
) -> None:
    await repository.append(
        AuditLog(
            audit_id=ids.new_id(),
            actor_user_id=actor_user_id,
            action=action,
            resource_type="avatar_session",
            resource_id=resource_id,
            created_at=clock.now(),
        )
    )
