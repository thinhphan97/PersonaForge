import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from personaforge_chat.application.dto import AuthContext, AuthenticatedUser, AuthResult
from personaforge_chat.application.ports import (
    AuthSessionRepository,
    Clock,
    IdGenerator,
    JwtTokenPort,
    PasswordHasherPort,
    SecureTokenPort,
    UserRepository,
)
from personaforge_chat.domain.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    InvalidInput,
    UserAlreadyExists,
)
from personaforge_chat.domain.models import AuthSession, User, UserStatus


@dataclass(slots=True)
class RegisterUser:
    users: UserRepository
    auth_sessions: AuthSessionRepository
    passwords: PasswordHasherPort
    jwt_tokens: JwtTokenPort
    secure_tokens: SecureTokenPort
    ids: IdGenerator
    clock: Clock
    access_ttl: timedelta
    refresh_ttl: timedelta

    async def execute(self, email: str, username: str, password: str) -> AuthResult:
        normalized_email = _normalize_email(email)
        normalized_username = _normalize_username(username)
        _validate_password(password)
        if (
            await self.users.get_by_email(normalized_email) is not None
            or await self.users.get_by_username(normalized_username) is not None
        ):
            raise UserAlreadyExists()
        now = self.clock.now()
        user = User(
            user_id=self.ids.new_id(),
            email=email.strip(),
            normalized_email=normalized_email,
            username=username.strip(),
            normalized_username=normalized_username,
            password_hash=self.passwords.hash(password),
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        await self.users.save(user)
        return await _new_auth_result(
            user,
            self.auth_sessions,
            self.jwt_tokens,
            self.secure_tokens,
            self.ids,
            self.clock,
            self.access_ttl,
            self.refresh_ttl,
        )


@dataclass(slots=True)
class LoginUser:
    users: UserRepository
    auth_sessions: AuthSessionRepository
    passwords: PasswordHasherPort
    jwt_tokens: JwtTokenPort
    secure_tokens: SecureTokenPort
    ids: IdGenerator
    clock: Clock
    access_ttl: timedelta
    refresh_ttl: timedelta
    dummy_hash: str

    async def execute(self, identifier: str, password: str) -> AuthResult:
        normalized = identifier.strip().casefold()
        user = (
            await self.users.get_by_email(normalized)
            if "@" in normalized
            else await self.users.get_by_username(normalized)
        )
        password_hash = user.password_hash if user is not None else self.dummy_hash
        valid = self.passwords.verify(password, password_hash)
        if user is None or not valid or user.status is not UserStatus.ACTIVE:
            raise AuthenticationFailed()
        return await _new_auth_result(
            user,
            self.auth_sessions,
            self.jwt_tokens,
            self.secure_tokens,
            self.ids,
            self.clock,
            self.access_ttl,
            self.refresh_ttl,
        )


@dataclass(slots=True)
class AuthenticateAccessToken:
    users: UserRepository
    auth_sessions: AuthSessionRepository
    jwt_tokens: JwtTokenPort
    clock: Clock

    async def execute(self, token: str | None) -> AuthContext:
        if not token:
            raise AuthenticationRequired()
        claims = self.jwt_tokens.decode_access(token)
        auth_session = await self.auth_sessions.get(claims.session_id)
        now = self.clock.now()
        if (
            auth_session is None
            or auth_session.user_id != claims.user_id
            or auth_session.revoked_at is not None
            or _as_utc(auth_session.expires_at) <= now
        ):
            raise AuthenticationRequired()
        user = await self.users.get(claims.user_id)
        if user is None or user.status is not UserStatus.ACTIVE:
            raise AuthenticationRequired()
        return AuthContext(_public_user(user), auth_session)


@dataclass(slots=True)
class RefreshAccessToken:
    users: UserRepository
    auth_sessions: AuthSessionRepository
    jwt_tokens: JwtTokenPort
    secure_tokens: SecureTokenPort
    ids: IdGenerator
    clock: Clock
    access_ttl: timedelta
    refresh_ttl: timedelta

    async def execute(
        self,
        raw_refresh_token: str | None,
        provided_csrf_token: str | None,
    ) -> AuthResult:
        if not raw_refresh_token:
            raise AuthenticationRequired()
        claims = self.jwt_tokens.decode_refresh(raw_refresh_token)
        old_session = await self.auth_sessions.get(claims.session_id)
        token_hash = self.secure_tokens.hash(raw_refresh_token)
        valid = (
            old_session is not None
            and old_session.user_id == claims.user_id
            and old_session.family_id == claims.family_id
            and hmac.compare_digest(old_session.token_hash, token_hash)
            and old_session.revoked_at is None
            and _as_utc(old_session.expires_at) > self.clock.now()
        )
        if not valid:
            # A valid, signed refresh JWT that no longer matches its server-side
            # record indicates replay. Revoke all sessions for this account.
            await self.auth_sessions.revoke_all(claims.user_id, self.clock.now())
            raise AuthenticationRequired()
        assert old_session is not None
        _require_csrf_token(old_session, provided_csrf_token)
        user = await self.users.get(claims.user_id)
        if user is None or user.status is not UserStatus.ACTIVE:
            raise AuthenticationRequired()
        result, replacement = _build_auth_result(
            user,
            old_session.family_id,
            self.jwt_tokens,
            self.secure_tokens,
            self.ids,
            self.clock,
            self.access_ttl,
            self.refresh_ttl,
        )
        replaced = await self.auth_sessions.replace(
            old_session.auth_session_id,
            token_hash,
            replacement,
            self.clock.now(),
        )
        if not replaced:
            await self.auth_sessions.revoke_all(claims.user_id, self.clock.now())
            raise AuthenticationRequired()
        return result


@dataclass(slots=True)
class LogoutUser:
    auth_sessions: AuthSessionRepository
    clock: Clock

    async def execute(self, context: AuthContext) -> None:
        await self.auth_sessions.revoke(
            context.auth_session.auth_session_id,
            self.clock.now(),
        )


@dataclass(slots=True)
class LogoutAll:
    auth_sessions: AuthSessionRepository
    clock: Clock

    async def execute(self, context: AuthContext) -> None:
        await self.auth_sessions.revoke_all(context.user.user_id, self.clock.now())


def require_csrf(context: AuthContext, provided_token: str | None) -> None:
    _require_csrf_token(context.auth_session, provided_token)


def _require_csrf_token(
    auth_session: AuthSession,
    provided_token: str | None,
) -> None:
    if not provided_token or not hmac.compare_digest(
        auth_session.csrf_token,
        provided_token,
    ):
        raise AuthenticationRequired()


async def _new_auth_result(
    user: User,
    auth_sessions: AuthSessionRepository,
    jwt_tokens: JwtTokenPort,
    secure_tokens: SecureTokenPort,
    ids: IdGenerator,
    clock: Clock,
    access_ttl: timedelta,
    refresh_ttl: timedelta,
) -> AuthResult:
    result, session = _build_auth_result(
        user,
        ids.new_id(),
        jwt_tokens,
        secure_tokens,
        ids,
        clock,
        access_ttl,
        refresh_ttl,
    )
    await auth_sessions.save(session)
    return result


def _build_auth_result(
    user: User,
    family_id: str,
    jwt_tokens: JwtTokenPort,
    secure_tokens: SecureTokenPort,
    ids: IdGenerator,
    clock: Clock,
    access_ttl: timedelta,
    refresh_ttl: timedelta,
) -> tuple[AuthResult, AuthSession]:
    now = clock.now()
    session_id = ids.new_id()
    csrf_token = secure_tokens.generate()
    refresh_token = jwt_tokens.issue_refresh(
        user.user_id,
        session_id,
        family_id,
        now,
    )
    session = AuthSession(
        auth_session_id=session_id,
        user_id=user.user_id,
        family_id=family_id,
        token_hash=secure_tokens.hash(refresh_token),
        csrf_token=csrf_token,
        expires_at=now + refresh_ttl,
        created_at=now,
        last_seen_at=now,
    )
    result = AuthResult(
        user=_public_user(user),
        access_token=jwt_tokens.issue_access(user.user_id, session_id, now),
        refresh_token=refresh_token,
        csrf_token=csrf_token,
        access_expires_in=int(access_ttl.total_seconds()),
    )
    return result, session


def _public_user(user: User) -> AuthenticatedUser:
    return AuthenticatedUser(user.user_id, user.email, user.username)


def _normalize_email(email: str) -> str:
    normalized = email.strip().casefold()
    if not normalized or "@" not in normalized or len(normalized) > 320:
        raise InvalidInput("Invalid email")
    return normalized


def _normalize_username(username: str) -> str:
    normalized = username.strip().casefold()
    if not 3 <= len(normalized) <= 64 or not all(
        character.isalnum() or character in {"-", "_", "."}
        for character in normalized
    ):
        raise InvalidInput("Invalid username")
    return normalized


def _validate_password(password: str) -> None:
    if not 12 <= len(password) <= 128:
        raise InvalidInput("Password must be between 12 and 128 characters")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
