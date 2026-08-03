import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from avatar_chat.application.auth_use_cases import require_csrf
from avatar_chat.application.dto import (
    AuthContext,
    AuthResult,
    ChatOutputEvent,
    SessionSummary,
    ShareSummary,
    WebRtcOffer,
)
from avatar_chat.bootstrap.container import Container, build_container
from avatar_chat.bootstrap.settings import Settings
from avatar_chat.domain.errors import (
    AccessDenied,
    AuthenticationFailed,
    AuthenticationRequired,
    ConcurrentSessionUpdate,
    DomainError,
    RateLimitExceeded,
    SessionBusy,
    SessionNotFound,
    UserAlreadyExists,
    UserNotFound,
)
from avatar_chat.domain.models import (
    AvatarState,
    ChatMessage,
    SessionVisibility,
    SharePermission,
)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=12, max_length=128)


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class AuthResponse(BaseModel):
    user_id: str
    email: str
    username: str
    csrf_token: str
    access_token: str | None = None
    token_type: str = "Bearer"
    expires_in: int | None = None


class CreateSessionRequest(BaseModel):
    character_id: str = Field(default="default", min_length=1, max_length=100)
    visibility: SessionVisibility = SessionVisibility.PRIVATE


class CreateSessionResponse(BaseModel):
    session_id: str
    character_id: str
    owner_user_id: str
    visibility: SessionVisibility
    offer_url: str
    media_provider: str
    media_access_url: str | None
    chat_url: str
    public_url: str | None


class SessionResponse(BaseModel):
    session_id: str
    character_id: str
    owner_user_id: str
    visibility: SessionVisibility
    status: str
    access_role: str
    can_chat: bool
    media_provider: str
    public_url: str | None


class PublicSessionResponse(BaseModel):
    session_id: str
    character_id: str
    status: str
    media_provider: str


class PublicMessageResponse(BaseModel):
    message_id: str
    role: str
    content: str
    created_at: datetime


class VisibilityRequest(BaseModel):
    visibility: SessionVisibility


class ShareRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    permission: SharePermission


class ShareResponse(BaseModel):
    user_id: str
    username: str
    permission: SharePermission


class CharacterResponse(BaseModel):
    character_id: str
    display_name: str


class SessionDescriptionRequest(BaseModel):
    sdp: str
    type: str


class SessionDescriptionResponse(BaseModel):
    sdp: str
    type: str
    avatar_state: str


class MediaAccessResponse(BaseModel):
    provider: str
    server_url: str
    access_token: str
    room_name: str
    avatar_state: str


class MessageResponse(BaseModel):
    message_id: str
    role: str
    content: str
    created_at: datetime
    author_user_id: str | None


class ChatRequest(BaseModel):
    type: str = "user_message"
    content: str = Field(min_length=1, max_length=10_000)


class LoginRateLimiter:
    def __init__(self, limit: int = 5, window_seconds: float = 60) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        async with self._lock:
            attempts = self._active_attempts(key)
            if len(attempts) >= self._limit:
                raise RateLimitExceeded()

    async def failed(self, key: str) -> None:
        async with self._lock:
            attempts = self._active_attempts(key)
            attempts.append(time.monotonic())
            self._attempts[key] = attempts

    async def succeeded(self, key: str) -> None:
        async with self._lock:
            self._attempts.pop(key, None)

    def _active_attempts(self, key: str) -> list[float]:
        threshold = time.monotonic() - self._window_seconds
        return [value for value in self._attempts.get(key, []) if value >= threshold]


class ChatConnectionHub:
    def __init__(self) -> None:
        self._connections: dict[str, dict[WebSocket, str]] = {}

    def add(self, session_id: str, websocket: WebSocket, access_token: str) -> None:
        self._connections.setdefault(session_id, {})[websocket] = access_token

    def remove(self, session_id: str, websocket: WebSocket) -> None:
        connections = self._connections.get(session_id)
        if connections is None:
            return
        connections.pop(websocket, None)
        if not connections:
            self._connections.pop(session_id, None)

    async def broadcast(
        self,
        session_id: str,
        event: ChatOutputEvent,
        container: Container,
    ) -> None:
        payload = _event_payload(event)
        connections = tuple(self._connections.get(session_id, {}).items())
        for websocket, access_token in connections:
            try:
                auth = await container.authenticate_token.execute(access_token)
                await container.get_conversation.execute(
                    auth.user.user_id,
                    session_id,
                    1,
                )
                await websocket.send_json(payload)
            except Exception:
                self.remove(session_id, websocket)
                try:
                    await websocket.close(code=1008, reason="Access revoked")
                except Exception:
                    pass


async def get_container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


ContainerDependency = Annotated[Container, Depends(get_container)]


async def get_auth_context(
    container: ContainerDependency,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthContext:
    return await container.authenticate_token.execute(_bearer_token(authorization))


AuthDependency = Annotated[AuthContext, Depends(get_auth_context)]


async def get_mutation_auth(
    auth: AuthDependency,
    x_csrf_token: Annotated[str | None, Header()] = None,
) -> AuthContext:
    require_csrf(auth, x_csrf_token)
    return auth


MutationAuthDependency = Annotated[AuthContext, Depends(get_mutation_auth)]


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = build_container(resolved)
        await app.state.container.startup()
        yield
        await app.state.container.shutdown()

    app = FastAPI(title="Avatar Chat WebRTC", version="0.2.0", lifespan=lifespan)
    router = APIRouter(prefix="/api")
    login_limiter = LoginRateLimiter()
    chat_hub = ChatConnectionHub()

    @app.middleware("http")
    async def security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
            "style-src 'self'; connect-src 'self' ws: wss:; media-src 'self' blob:; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'"
        )
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        headers: dict[str, str] = {}
        if isinstance(exc, (AuthenticationRequired, AuthenticationFailed)):
            status = 401
            headers["WWW-Authenticate"] = "Bearer"
        elif isinstance(exc, AccessDenied):
            status = 403
        elif isinstance(exc, RateLimitExceeded):
            status = 429
        elif isinstance(exc, (SessionNotFound, UserNotFound)):
            status = 404
        elif isinstance(
            exc,
            (SessionBusy, ConcurrentSessionUpdate, UserAlreadyExists),
        ):
            status = 409
        else:
            status = 400
        return JSONResponse(
            status_code=status,
            content={"detail": str(exc)},
            headers=headers,
        )

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get(
        "/public/sessions/{session_id}",
        response_model=PublicSessionResponse,
    )
    async def public_session(
        session_id: str,
        container: ContainerDependency,
    ) -> PublicSessionResponse:
        assert container.get_public_session is not None
        session = await container.get_public_session.execute(session_id)
        return PublicSessionResponse(
            session_id=session.session_id,
            character_id=session.character_id,
            status=session.status.value,
            media_provider=container.media_provider,
        )

    @router.get(
        "/public/sessions/{session_id}/messages",
        response_model=list[PublicMessageResponse],
    )
    async def public_messages(
        session_id: str,
        container: ContainerDependency,
        limit: int = 100,
    ) -> list[PublicMessageResponse]:
        assert container.get_public_conversation is not None
        history = await container.get_public_conversation.execute(
            session_id,
            max(1, min(limit, 100)),
        )
        return [
            PublicMessageResponse(
                message_id=message.message_id,
                role=message.role.value,
                content=message.content,
                created_at=message.created_at,
            )
            for message in history
        ]

    @router.post(
        "/public/sessions/{session_id}/media/access",
        response_model=MediaAccessResponse,
    )
    async def public_media_access(
        session_id: str,
        container: ContainerDependency,
    ) -> MediaAccessResponse | JSONResponse:
        if container.provision_public_media is None:
            return JSONResponse(
                status_code=409,
                content={"detail": "Public viewing requires livekit mode"},
            )
        access = await container.provision_public_media.execute(session_id)
        return _media_access_response(access)

    @router.post("/auth/register", response_model=AuthResponse, status_code=201)
    async def register(
        body: RegisterRequest,
        response: Response,
        container: ContainerDependency,
    ) -> AuthResponse:
        result = await container.register_user.execute(
            body.email,
            body.username,
            body.password,
        )
        _set_auth_cookies(response, container, result)
        return _auth_response(result)

    @router.post("/auth/login", response_model=AuthResponse)
    async def login(
        body: LoginRequest,
        request: Request,
        response: Response,
        container: ContainerDependency,
    ) -> AuthResponse:
        client_host = request.client.host if request.client else "unknown"
        rate_key = f"{client_host}:{body.identifier.strip().casefold()}"
        await login_limiter.check(rate_key)
        try:
            result = await container.login_user.execute(body.identifier, body.password)
        except AuthenticationFailed:
            await login_limiter.failed(rate_key)
            raise
        await login_limiter.succeeded(rate_key)
        _set_auth_cookies(response, container, result)
        return _auth_response(result)

    @router.post("/auth/refresh", response_model=AuthResponse)
    async def refresh(
        request: Request,
        response: Response,
        container: ContainerDependency,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> AuthResponse:
        result = await container.refresh_access_token.execute(
            request.cookies.get(container.refresh_cookie_name),
            x_csrf_token,
        )
        _set_auth_cookies(response, container, result)
        return _auth_response(result)

    @router.get("/auth/me", response_model=AuthResponse)
    async def me(auth: AuthDependency) -> AuthResponse:
        return AuthResponse(
            user_id=auth.user.user_id,
            email=auth.user.email,
            username=auth.user.username,
            csrf_token=auth.auth_session.csrf_token,
        )

    @router.post("/auth/logout", status_code=204)
    async def logout(
        response: Response,
        auth: MutationAuthDependency,
        container: ContainerDependency,
    ) -> None:
        await container.logout_user.execute(auth)
        _clear_auth_cookies(response, container)

    @router.post("/auth/logout-all", status_code=204)
    async def logout_all(
        response: Response,
        auth: MutationAuthDependency,
        container: ContainerDependency,
    ) -> None:
        await container.logout_all.execute(auth)
        _clear_auth_cookies(response, container)

    @router.get("/characters", response_model=list[CharacterResponse])
    async def list_characters(
        _auth: AuthDependency,
        container: ContainerDependency,
    ) -> list[CharacterResponse]:
        return [
            CharacterResponse(
                character_id=character.character_id,
                display_name=character.display_name,
            )
            for character in await container.list_characters.execute()
        ]

    @router.post("/sessions", response_model=CreateSessionResponse, status_code=201)
    async def create_session(
        body: CreateSessionRequest,
        auth: MutationAuthDependency,
        container: ContainerDependency,
    ) -> CreateSessionResponse:
        result = await container.create_session.execute(
            auth.user.user_id,
            body.character_id,
            body.visibility,
        )
        return _create_session_response(
            result.session_id,
            result.character_id,
            auth.user.user_id,
            body.visibility,
            container,
        )

    @router.get("/sessions/mine", response_model=list[SessionResponse])
    async def owned_sessions(
        auth: AuthDependency,
        container: ContainerDependency,
    ) -> list[SessionResponse]:
        return _session_responses(
            await container.list_owned_sessions.execute(auth.user.user_id),
            container.media_provider,
        )

    @router.get("/sessions/shared-with-me", response_model=list[SessionResponse])
    async def shared_sessions(
        auth: AuthDependency,
        container: ContainerDependency,
    ) -> list[SessionResponse]:
        return _session_responses(
            await container.list_shared_sessions.execute(auth.user.user_id),
            container.media_provider,
        )

    @router.get("/sessions/public", response_model=list[SessionResponse])
    async def public_sessions(
        auth: AuthDependency,
        container: ContainerDependency,
    ) -> list[SessionResponse]:
        return _session_responses(
            await container.list_public_sessions.execute(auth.user.user_id),
            container.media_provider,
        )

    @router.patch("/sessions/{session_id}/visibility", response_model=SessionResponse)
    async def change_visibility(
        session_id: str,
        body: VisibilityRequest,
        auth: MutationAuthDependency,
        container: ContainerDependency,
    ) -> SessionResponse:
        summary = await container.change_visibility.execute(
            auth.user.user_id,
            session_id,
            body.visibility,
        )
        return _session_response(summary, container.media_provider)

    @router.post("/sessions/{session_id}/shares", response_model=ShareResponse)
    async def share_session(
        session_id: str,
        body: ShareRequest,
        auth: MutationAuthDependency,
        container: ContainerDependency,
    ) -> ShareResponse:
        return _share_response(
            await container.share_session.execute(
                auth.user.user_id,
                session_id,
                body.username,
                body.permission,
            )
        )

    @router.get("/sessions/{session_id}/shares", response_model=list[ShareResponse])
    async def list_shares(
        session_id: str,
        auth: AuthDependency,
        container: ContainerDependency,
    ) -> list[ShareResponse]:
        return [
            _share_response(share)
            for share in await container.list_session_shares.execute(
                auth.user.user_id,
                session_id,
            )
        ]

    @router.delete("/sessions/{session_id}/shares/{user_id}", status_code=204)
    async def revoke_share(
        session_id: str,
        user_id: str,
        auth: MutationAuthDependency,
        container: ContainerDependency,
    ) -> None:
        await container.revoke_session_share.execute(
            auth.user.user_id,
            session_id,
            user_id,
        )

    @router.post(
        "/sessions/{session_id}/media/access",
        response_model=MediaAccessResponse,
    )
    async def provision_media(
        session_id: str,
        auth: MutationAuthDependency,
        container: ContainerDependency,
    ) -> MediaAccessResponse | JSONResponse:
        if container.provision_media is None:
            return JSONResponse(
                status_code=409,
                content={"detail": "Media access endpoint requires livekit mode"},
            )
        access = await container.provision_media.execute(auth.user.user_id, session_id)
        return _media_access_response(access)

    @router.post(
        "/sessions/{session_id}/webrtc/offer",
        response_model=SessionDescriptionResponse,
    )
    async def negotiate(
        session_id: str,
        body: SessionDescriptionRequest,
        auth: MutationAuthDependency,
        container: ContainerDependency,
    ) -> SessionDescriptionResponse | JSONResponse:
        if container.media_provider != "aiortc":
            return JSONResponse(
                status_code=409,
                content={"detail": "SDP offer endpoint requires aiortc mode"},
            )
        answer = await container.negotiate_stream.execute(
            auth.user.user_id,
            session_id,
            WebRtcOffer(sdp=body.sdp, type=body.type),
        )
        return SessionDescriptionResponse(
            sdp=answer.sdp,
            type=answer.type,
            avatar_state=AvatarState.IDLE.value,
        )

    @router.get(
        "/sessions/{session_id}/messages",
        response_model=list[MessageResponse],
    )
    async def messages(
        session_id: str,
        auth: AuthDependency,
        container: ContainerDependency,
        limit: int = 50,
    ) -> list[MessageResponse]:
        history = await container.get_conversation.execute(
            auth.user.user_id,
            session_id,
            min(limit, 100),
        )
        return [_message_response(message) for message in history]

    @router.delete("/sessions/{session_id}", status_code=204)
    async def close_session(
        session_id: str,
        auth: MutationAuthDependency,
        container: ContainerDependency,
    ) -> None:
        await container.close_session.execute(auth.user.user_id, session_id)

    @app.websocket("/api/sessions/{session_id}/chat")
    async def chat(websocket: WebSocket, session_id: str) -> None:
        container: Container = websocket.app.state.container
        if not _origin_allowed(websocket, container):
            await websocket.close(code=1008, reason="Origin is not allowed")
            return
        raw_token = _websocket_bearer_token(websocket)
        try:
            auth = await container.authenticate_token.execute(raw_token)
            await container.get_conversation.execute(
                auth.user.user_id,
                session_id,
                1,
            )
        except DomainError:
            await websocket.close(code=1008, reason="Authentication required")
            return
        await websocket.accept(subprotocol="avatar-chat")
        assert raw_token is not None
        chat_hub.add(session_id, websocket, raw_token)
        try:
            while True:
                request = ChatRequest.model_validate(await websocket.receive_json())
                auth = await container.authenticate_token.execute(raw_token)
                async for event in container.handle_message.execute(
                    auth.user.user_id,
                    session_id,
                    request.content,
                ):
                    await chat_hub.broadcast(session_id, event, container)
        except WebSocketDisconnect:
            pass
        except DomainError as exc:
            await websocket.send_json({"type": "error", "detail": str(exc)})
            await websocket.close(code=1008)
        except Exception:
            await websocket.close()
        finally:
            chat_hub.remove(session_id, websocket)

    app.include_router(router)
    frontend = resolved.project_root / "frontend"
    if frontend.is_dir():
        index_html = frontend.joinpath("index.html").read_text()
        app_javascript = frontend.joinpath("app.js").read_text()
        stylesheet = frontend.joinpath("styles.css").read_text()
        public_watch_html = frontend.joinpath("public-watch.html").read_text()
        public_watch_javascript = frontend.joinpath("public-watch.js").read_text()

        @app.get("/", include_in_schema=False)
        async def frontend_index() -> HTMLResponse:
            return HTMLResponse(index_html)

        @app.get("/app.js", include_in_schema=False)
        async def frontend_javascript() -> Response:
            return Response(app_javascript, media_type="text/javascript")

        @app.get("/styles.css", include_in_schema=False)
        async def frontend_stylesheet() -> Response:
            return Response(stylesheet, media_type="text/css")

        @app.get("/watch/{session_id}", include_in_schema=False)
        async def public_watch(session_id: str) -> HTMLResponse:
            del session_id
            return HTMLResponse(public_watch_html)

        @app.get("/public-watch.js", include_in_schema=False)
        async def public_watch_script() -> Response:
            return Response(public_watch_javascript, media_type="text/javascript")
    return app


def _set_auth_cookies(response: Response, container: Container, result: AuthResult) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        key=container.refresh_cookie_name,
        value=result.refresh_token,
        max_age=container.refresh_cookie_max_age,
        path="/api/auth",
        secure=container.auth_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        key="avatar_csrf",
        value=result.csrf_token,
        max_age=container.refresh_cookie_max_age,
        path="/",
        secure=container.auth_cookie_secure,
        httponly=False,
        samesite="strict",
    )


def _clear_auth_cookies(response: Response, container: Container) -> None:
    response.delete_cookie(container.refresh_cookie_name, path="/api/auth")
    response.delete_cookie("avatar_csrf", path="/")


def _auth_response(result: AuthResult) -> AuthResponse:
    return AuthResponse(
        user_id=result.user.user_id,
        email=result.user.email,
        username=result.user.username,
        csrf_token=result.csrf_token,
        access_token=result.access_token,
        expires_in=result.access_expires_in,
    )


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token.strip():
        return None
    return token.strip()


def _websocket_bearer_token(websocket: WebSocket) -> str | None:
    protocols = {
        protocol.strip()
        for protocol in websocket.headers.get("sec-websocket-protocol", "").split(",")
    }
    encoded = next(
        (protocol for protocol in protocols if protocol.startswith("bearer.")),
        None,
    )
    return encoded.removeprefix("bearer.") if encoded else None


def _create_session_response(
    session_id: str,
    character_id: str,
    owner_user_id: str,
    visibility: SessionVisibility,
    container: Container,
) -> CreateSessionResponse:
    return CreateSessionResponse(
        session_id=session_id,
        character_id=character_id,
        owner_user_id=owner_user_id,
        visibility=visibility,
        offer_url=f"/api/sessions/{session_id}/webrtc/offer",
        media_provider=container.media_provider,
        media_access_url=(
            f"/api/sessions/{session_id}/media/access"
            if container.media_provider == "livekit"
            else None
        ),
        chat_url=f"/api/sessions/{session_id}/chat",
        public_url=(
            f"/watch/{session_id}"
            if visibility is SessionVisibility.PUBLIC
            else None
        ),
    )


def _session_responses(
    summaries: tuple[SessionSummary, ...],
    media_provider: str,
) -> list[SessionResponse]:
    return [_session_response(summary, media_provider) for summary in summaries]


def _session_response(summary: SessionSummary, media_provider: str) -> SessionResponse:
    return SessionResponse(
        session_id=summary.session_id,
        character_id=summary.character_id,
        owner_user_id=summary.owner_user_id,
        visibility=summary.visibility,
        status=summary.status.value,
        access_role=summary.access_role,
        can_chat=summary.can_chat,
        media_provider=media_provider,
        public_url=(
            f"/watch/{summary.session_id}"
            if summary.visibility is SessionVisibility.PUBLIC
            else None
        ),
    )


def _media_access_response(access: Any) -> MediaAccessResponse:
    return MediaAccessResponse(
        provider=access.provider,
        server_url=access.server_url,
        access_token=access.access_token,
        room_name=access.room_name,
        avatar_state=AvatarState.IDLE.value,
    )


def _share_response(summary: ShareSummary) -> ShareResponse:
    return ShareResponse(
        user_id=summary.user_id,
        username=summary.username,
        permission=summary.permission,
    )


def _origin_allowed(websocket: WebSocket, container: Container) -> bool:
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not origin or not host:
        return False
    forwarded_proto = websocket.headers.get("x-forwarded-proto")
    scheme = forwarded_proto or ("https" if websocket.url.scheme == "wss" else "http")
    return origin in {*container.allowed_origins, f"{scheme}://{host}"}


def _event_payload(event: ChatOutputEvent) -> dict[str, Any]:
    return {
        "type": event.type.value,
        "content": event.content,
        "avatar_state": event.avatar_state.value if event.avatar_state else None,
    }


def _message_response(message: ChatMessage) -> MessageResponse:
    return MessageResponse(
        message_id=message.message_id,
        role=message.role.value,
        content=message.content,
        created_at=message.created_at,
        author_user_id=message.author_user_id,
    )
