from datetime import datetime, timedelta, timezone

import pytest

from avatar_chat.adapters.outbound.jwt_tokens import PyJwtTokenAdapter
from avatar_chat.adapters.outbound.memory import (
    InMemoryAuthSessionRepository,
    InMemorySessionShareRepository,
    InMemoryUserRepository,
)
from avatar_chat.adapters.outbound.system import Argon2PasswordHasher, SecureTokenGenerator
from avatar_chat.application.auth_use_cases import (
    AuthenticateAccessToken,
    LoginUser,
    LogoutUser,
    RefreshAccessToken,
    RegisterUser,
)
from avatar_chat.application.session_access import SessionAccessPolicy
from avatar_chat.domain.errors import AccessDenied, AuthenticationRequired, UserAlreadyExists
from avatar_chat.domain.models import (
    AvatarSession,
    AvatarState,
    SessionAction,
    SessionShare,
    SessionStatus,
    SessionVisibility,
    SharePermission,
)


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self.value


class Ids:
    def __init__(self) -> None:
        self.current = 0

    def new_id(self) -> str:
        self.current += 1
        return f"id-{self.current}"


@pytest.mark.asyncio
async def test_register_refresh_rotation_replay_detection_and_logout() -> None:
    users = InMemoryUserRepository()
    sessions = InMemoryAuthSessionRepository()
    passwords = Argon2PasswordHasher()
    tokens = SecureTokenGenerator()
    ids = Ids()
    clock = FixedClock()
    access_ttl = timedelta(minutes=15)
    refresh_ttl = timedelta(days=7)
    jwt_tokens = PyJwtTokenAdapter(
        "unit-test-secret-with-at-least-thirty-two-bytes",
        "test-issuer",
        "test-audience",
        access_ttl,
        refresh_ttl,
    )
    register = RegisterUser(
        users,
        sessions,
        passwords,
        jwt_tokens,
        tokens,
        ids,
        clock,
        access_ttl,
        refresh_ttl,
    )

    registered = await register.execute(
        "User@Example.com",
        "Tester",
        "a-secure-password-123",
    )
    authenticate = AuthenticateAccessToken(users, sessions, jwt_tokens, clock)
    await authenticate.execute(registered.access_token)
    refresh = RefreshAccessToken(
        users,
        sessions,
        jwt_tokens,
        tokens,
        ids,
        clock,
        access_ttl,
        refresh_ttl,
    )
    refreshed = await refresh.execute(registered.refresh_token, registered.csrf_token)
    with pytest.raises(AuthenticationRequired):
        await authenticate.execute(registered.access_token)
    assert (await authenticate.execute(refreshed.access_token)).user == registered.user
    with pytest.raises(AuthenticationRequired):
        await refresh.execute(registered.refresh_token, registered.csrf_token)
    with pytest.raises(AuthenticationRequired):
        await authenticate.execute(refreshed.access_token)

    logged_in = await LoginUser(
        users,
        sessions,
        passwords,
        jwt_tokens,
        tokens,
        ids,
        clock,
        access_ttl,
        refresh_ttl,
        passwords.hash("dummy-password-value"),
    ).execute("tester", "a-secure-password-123")
    login_context = await authenticate.execute(logged_in.access_token)
    await LogoutUser(sessions, clock).execute(login_context)

    assert logged_in.user.user_id == registered.user.user_id
    with pytest.raises(AuthenticationRequired):
        await authenticate.execute(logged_in.access_token)
    with pytest.raises(UserAlreadyExists):
        await register.execute(
            "user@example.com",
            "another-name",
            "another-password-123",
        )


@pytest.mark.asyncio
async def test_session_access_policy_denies_by_default_and_honors_shares() -> None:
    shares = InMemorySessionShareRepository()
    policy = SessionAccessPolicy(shares)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session = AvatarSession(
        "session-1",
        "default",
        "owner",
        SessionVisibility.PRIVATE,
        SessionStatus.READY,
        AvatarState.IDLE,
        now,
        now,
    )

    with pytest.raises(AccessDenied):
        await policy.require("guest", session, SessionAction.VIEW)
    await shares.save(
        SessionShare(
            "share-1",
            session.session_id,
            "guest",
            SharePermission.VIEWER,
            "owner",
            now,
        )
    )
    await policy.require("guest", session, SessionAction.VIEW)
    with pytest.raises(AccessDenied):
        await policy.require("guest", session, SessionAction.CHAT)

    await shares.save(
        SessionShare(
            "share-1",
            session.session_id,
            "guest",
            SharePermission.INTERACTOR,
            "owner",
            now,
        )
    )
    await policy.require("guest", session, SessionAction.CHAT)
    session.visibility = SessionVisibility.PUBLIC
    await policy.require("anonymous-user", session, SessionAction.VIEW)
