import asyncio
import logging
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

from personaforge_chat.application.auth_use_cases import require_csrf
from personaforge_chat.application.dto import (
    AuthContext,
    AuthResult,
    ChatOutputEvent,
    SessionSummary,
    ShareSummary,
    WebRtcOffer,
)
from personaforge_chat.bootstrap.container import Container, build_container
from personaforge_chat.bootstrap.settings import Settings
from personaforge_chat.domain.errors import (
    AccessDenied,
    AuthenticationFailed,
    AuthenticationRequired,
    ConcurrentSessionUpdate,
    DomainError,
    InvalidInput,
    MediaCapacityUnavailable,
    RateLimitExceeded,
    SessionBusy,
    SessionClosed,
    SessionNotFound,
    UserAlreadyExists,
    UserNotFound,
)
from personaforge_chat.domain.models import (
    AvatarState,
    ChatMessage,
    SessionVisibility,
    SharePermission,
)
from personaforge_observability import (
    bind_context,
    new_id,
    parse_traceparent,
)
from personaforge_observability.http import install_http_observability

logger = logging.getLogger(__name__)


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
    voice_id: str = Field(default="system-vi-minh-duc", min_length=1, max_length=100)
    visibility: SessionVisibility = SessionVisibility.PRIVATE


class CreateSessionResponse(BaseModel):
    session_id: str
    character_id: str
    voice_id: str
    owner_user_id: str
    visibility: SessionVisibility
    offer_url: str
    media_provider: str
    media_access_url: str | None
    chat_url: str
    public_url: str | None
    version: int


class SessionResponse(BaseModel):
    session_id: str
    character_id: str
    voice_id: str
    owner_user_id: str
    visibility: SessionVisibility
    status: str
    access_role: str
    can_chat: bool
    media_provider: str
    public_url: str | None
    version: int


class PublicSessionResponse(BaseModel):
    session_id: str
    character_id: str
    voice_id: str
    status: str
    media_provider: str
    version: int


class PublicMessageResponse(BaseModel):
    message_id: str
    role: str
    content: str
    created_at: datetime
    sequence: int
    revision: int
    status: str


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


class VoiceResponse(BaseModel):
    voice_id: str
    display_name: str
    description: str
    language: str
    is_default: bool


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
    sequence: int
    revision: int
    status: str


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
            except (AuthenticationRequired, AuthenticationFailed):
                self.remove(session_id, websocket)
                await _close_websocket_safely(
                    websocket,
                    code=1008,
                    reason="Authentication expired",
                )
                continue
            try:
                await container.get_conversation.execute(
                    auth.user.user_id,
                    session_id,
                    1,
                )
            except (AccessDenied, SessionNotFound):
                self.remove(session_id, websocket)
                await _close_websocket_safely(
                    websocket,
                    code=1008,
                    reason="Access revoked",
                )
                continue
            try:
                await websocket.send_json(payload)
            except Exception:
                self.remove(session_id, websocket)
                await _close_websocket_safely(
                    websocket,
                    code=1011,
                    reason="Connection interrupted",
                )


async def get_container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


async def _close_websocket_safely(
    websocket: WebSocket,
    code: int,
    reason: str,
) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass


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
        logger.info(
            "Chat service started",
            extra={"event": "chat.service.started"},
        )
        try:
            await app.state.container.realtime_events.subscribe(
                lambda session_id, event: chat_hub.broadcast(
                    session_id,
                    event,
                    app.state.container,
                )
            )
            await app.state.container.outbox_dispatcher.startup()
            yield
        finally:
            logger.info(
                "Chat service is stopping",
                extra={"event": "chat.service.stopping"},
            )
            await app.state.container.shutdown()

    app = FastAPI(title="Avatar Chat WebRTC", version="0.2.0", lifespan=lifespan)
    install_http_observability(app)
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
        if isinstance(exc, AuthenticationRequired | AuthenticationFailed):
            status = 401
            headers["WWW-Authenticate"] = "Bearer"
        elif isinstance(exc, AccessDenied):
            status = 403
        elif isinstance(exc, RateLimitExceeded):
            status = 429
        elif isinstance(exc, MediaCapacityUnavailable):
            status = 503
            headers["Retry-After"] = "5"
        elif isinstance(exc, SessionNotFound | UserNotFound):
            status = 404
        elif isinstance(
            exc,
            SessionBusy | SessionClosed | ConcurrentSessionUpdate | UserAlreadyExists,
        ):
            status = 409
        else:
            status = 400
        content = {"detail": str(exc)}
        if isinstance(exc, MediaCapacityUnavailable):
            content["code"] = "media_capacity_unavailable"
        return JSONResponse(status_code=status, content=content, headers=headers)

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
            voice_id=session.voice_id,
            status=session.status.value,
            media_provider=container.media_provider,
            version=session.version,
        )

    @router.get(
        "/public/sessions/{session_id}/messages",
        response_model=list[PublicMessageResponse],
    )
    async def public_messages(
        session_id: str,
        container: ContainerDependency,
        limit: int = 100,
        after: str | None = None,
    ) -> list[PublicMessageResponse]:
        assert container.get_public_conversation is not None
        normalized_limit = max(1, min(limit, 100))
        if after is None:
            history = await container.get_public_conversation.execute(
                session_id,
                normalized_limit,
            )
        else:
            sequence, revision = _parse_cursor(after)
            history = await container.get_public_conversation.execute_after(
                session_id,
                sequence,
                revision,
                normalized_limit,
            )
        return [
            PublicMessageResponse(
                message_id=message.message_id,
                role=message.role.value,
                content=message.content,
                created_at=message.created_at,
                sequence=message.sequence,
                revision=message.revision,
                status=message.status.value,
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

    @router.get("/voices", response_model=list[VoiceResponse])
    async def list_voices(
        _auth: AuthDependency,
        container: ContainerDependency,
    ) -> list[VoiceResponse]:
        return [
            VoiceResponse(
                voice_id=voice.voice_id,
                display_name=voice.display_name,
                description=voice.description,
                language=voice.language,
                is_default=voice.is_default,
            )
            for voice in await container.list_voices.execute()
        ]

    @router.post("/sessions", response_model=CreateSessionResponse, status_code=201)
    async def create_session(
        body: CreateSessionRequest,
        auth: MutationAuthDependency,
        container: ContainerDependency,
    ) -> CreateSessionResponse:
        result = await container.create_session.execute(
            owner_user_id=auth.user.user_id,
            character_id=body.character_id,
            visibility=body.visibility,
            voice_id=body.voice_id,
        )
        return _create_session_response(
            result.session_id,
            result.character_id,
            result.voice_id,
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

    @router.get("/sessions/{session_id}", response_model=SessionResponse)
    async def session_snapshot(
        session_id: str,
        auth: AuthDependency,
        container: ContainerDependency,
    ) -> SessionResponse:
        return _session_response(
            await container.get_session_summary.execute(
                auth.user.user_id,
                session_id,
            ),
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
        await container.outbox_dispatcher.flush()
        return _session_response(summary, container.media_provider)

    @router.post("/sessions/{session_id}/shares", response_model=ShareResponse)
    async def share_session(
        session_id: str,
        body: ShareRequest,
        auth: MutationAuthDependency,
        container: ContainerDependency,
    ) -> ShareResponse:
        share = await container.share_session.execute(
            auth.user.user_id,
            session_id,
            body.username,
            body.permission,
        )
        await container.outbox_dispatcher.flush()
        return _share_response(share)

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
        await container.outbox_dispatcher.flush()

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
        after: str | None = None,
    ) -> list[MessageResponse]:
        normalized_limit = max(1, min(limit, 100))
        if after is None:
            history = await container.get_conversation.execute(
                auth.user.user_id,
                session_id,
                normalized_limit,
            )
        else:
            sequence, revision = _parse_cursor(after)
            history = await container.get_conversation.execute_after(
                auth.user.user_id,
                session_id,
                sequence,
                revision,
                normalized_limit,
            )
        return [_message_response(message) for message in history]

    @router.delete("/sessions/{session_id}", status_code=204)
    async def close_session(
        session_id: str,
        auth: MutationAuthDependency,
        container: ContainerDependency,
    ) -> None:
        await container.close_session.execute(auth.user.user_id, session_id)
        await container.outbox_dispatcher.flush()

    @app.websocket("/api/sessions/{session_id}/chat")
    async def chat(websocket: WebSocket, session_id: str) -> None:
        connection_id = new_id()
        request_id = websocket.headers.get("x-request-id") or new_id()
        trace_id, span_id, trace_flags = parse_traceparent(websocket.headers.get("traceparent"))
        with bind_context(
            connection_id=connection_id,
            request_id=request_id,
            trace_id=trace_id,
            span_id=span_id,
            trace_flags=trace_flags,
            session_id=session_id,
        ):
            await _serve_chat_websocket(websocket, session_id, chat_hub)

    async def _serve_chat_websocket(
        websocket: WebSocket,
        session_id: str,
        chat_hub: ChatConnectionHub,
    ) -> None:
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
        except AccessDenied:
            await websocket.close(code=1008, reason="Access revoked")
            return
        except DomainError:
            await websocket.close(code=1008, reason="Authentication required")
            return
        await websocket.accept(subprotocol="avatar-chat")
        assert raw_token is not None
        chat_hub.add(session_id, websocket, raw_token)
        logger.info(
            "Chat WebSocket connected",
            extra={"event": "chat.websocket.connected"},
        )
        try:
            while True:
                request = ChatRequest.model_validate(await websocket.receive_json())
                message_request_id = new_id()
                message_trace_id, message_span_id, message_flags = parse_traceparent(None)
                with bind_context(
                    request_id=message_request_id,
                    trace_id=message_trace_id,
                    span_id=message_span_id,
                    trace_flags=message_flags,
                ):
                    auth = await container.authenticate_token.execute(raw_token)
                    logger.info(
                        "Chat message received",
                        extra={
                            "event": "chat.message.received",
                            "user_id": auth.user.user_id,
                            "input_chars": len(request.content),
                        },
                    )
                    async for event in container.handle_message.execute(
                        auth.user.user_id,
                        session_id,
                        request.content,
                    ):
                        await container.realtime_events.publish(session_id, event)
        except WebSocketDisconnect:
            logger.info(
                "Chat WebSocket disconnected",
                extra={"event": "chat.websocket.disconnected", "outcome": "client_closed"},
            )
        except DomainError as exc:
            logger.warning(
                "Chat WebSocket request was rejected",
                extra={"event": "chat.websocket.rejected", "reason": type(exc).__name__},
            )
            await websocket.send_json({"type": "error", "detail": str(exc)})
            await websocket.close(code=1008)
        except Exception:
            logger.exception(
                "Chat WebSocket failed",
                extra={"event": "chat.websocket.failed", "outcome": "failure"},
            )
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
    voice_id: str,
    owner_user_id: str,
    visibility: SessionVisibility,
    container: Container,
) -> CreateSessionResponse:
    return CreateSessionResponse(
        session_id=session_id,
        character_id=character_id,
        voice_id=voice_id,
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
        public_url=(f"/watch/{session_id}" if visibility is SessionVisibility.PUBLIC else None),
        version=0,
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
        voice_id=summary.voice_id,
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
        version=summary.version,
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
        "message_id": event.message_id,
        "sequence": event.sequence,
        "revision": event.revision,
        "author_user_id": event.author_user_id,
        "session_version": event.session_version,
    }


def _message_response(message: ChatMessage) -> MessageResponse:
    return MessageResponse(
        message_id=message.message_id,
        role=message.role.value,
        content=message.content,
        created_at=message.created_at,
        author_user_id=message.author_user_id,
        sequence=message.sequence,
        revision=message.revision,
        status=message.status.value,
    )


def _parse_cursor(cursor: str) -> tuple[int, int]:
    try:
        sequence_text, revision_text = cursor.split(":", 1)
        sequence = int(sequence_text)
        revision = int(revision_text)
    except (TypeError, ValueError) as exc:
        raise InvalidInput("Invalid message cursor") from exc
    if sequence < 0 or revision < 0:
        raise InvalidInput("Invalid message cursor")
    return sequence, revision
