from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from avatar_chat.application.dto import ChatOutputEvent, WebRtcOffer
from avatar_chat.bootstrap.container import Container, build_container
from avatar_chat.bootstrap.settings import Settings
from avatar_chat.domain.errors import ConcurrentSessionUpdate, DomainError, SessionBusy
from avatar_chat.domain.models import AvatarState, ChatMessage


class CreateSessionRequest(BaseModel):
    character_id: str = Field(default="default", min_length=1, max_length=100)


class CreateSessionResponse(BaseModel):
    session_id: str
    character_id: str
    offer_url: str
    media_provider: str
    media_access_url: str | None
    chat_url: str


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


class ChatRequest(BaseModel):
    type: str = "user_message"
    content: str = Field(min_length=1, max_length=10_000)


async def get_container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


ContainerDependency = Annotated[Container, Depends(get_container)]


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = build_container(resolved)
        await app.state.container.startup()
        yield
        await app.state.container.shutdown()

    app = FastAPI(title="Avatar Chat WebRTC", version="0.1.0", lifespan=lifespan)
    router = APIRouter(prefix="/api")

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        status = (
            409
            if isinstance(exc, (SessionBusy, ConcurrentSessionUpdate))
            else 400
        )
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.post("/sessions", response_model=CreateSessionResponse, status_code=201)
    async def create_session(
        body: CreateSessionRequest,
        container: ContainerDependency,
    ) -> CreateSessionResponse:
        result = await container.create_session.execute(body.character_id)
        return CreateSessionResponse(
            session_id=result.session_id,
            character_id=result.character_id,
            offer_url=f"/api/sessions/{result.session_id}/webrtc/offer",
            media_provider=container.media_provider,
            media_access_url=(
                f"/api/sessions/{result.session_id}/media/access"
                if container.media_provider == "livekit"
                else None
            ),
            chat_url=f"/api/sessions/{result.session_id}/chat",
        )

    @router.post(
        "/sessions/{session_id}/media/access",
        response_model=MediaAccessResponse,
    )
    async def provision_media(
        session_id: str,
        container: ContainerDependency,
    ) -> MediaAccessResponse | JSONResponse:
        if container.provision_media is None:
            return JSONResponse(
                status_code=409,
                content={"detail": "Media access endpoint requires livekit mode"},
            )
        access = await container.provision_media.execute(session_id)
        return MediaAccessResponse(
            provider=access.provider,
            server_url=access.server_url,
            access_token=access.access_token,
            room_name=access.room_name,
            avatar_state=AvatarState.IDLE.value,
        )

    @router.post(
        "/sessions/{session_id}/webrtc/offer",
        response_model=SessionDescriptionResponse,
    )
    async def negotiate(
        session_id: str,
        body: SessionDescriptionRequest,
        container: ContainerDependency,
    ) -> SessionDescriptionResponse | JSONResponse:
        if container.media_provider != "aiortc":
            return JSONResponse(
                status_code=409,
                content={"detail": "SDP offer endpoint requires aiortc mode"},
            )
        answer = await container.negotiate_stream.execute(
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
        container: ContainerDependency,
        limit: int = 50,
    ) -> list[MessageResponse]:
        history = await container.get_conversation.execute(session_id, min(limit, 100))
        return [_message_response(message) for message in history]

    @router.delete("/sessions/{session_id}", status_code=204)
    async def close_session(session_id: str, container: ContainerDependency) -> None:
        await container.close_session.execute(session_id)

    @app.websocket("/api/sessions/{session_id}/chat")
    async def chat(websocket: WebSocket, session_id: str) -> None:
        container: Container = websocket.app.state.container
        await websocket.accept()
        try:
            while True:
                request = ChatRequest.model_validate(await websocket.receive_json())
                async for event in container.handle_message.execute(
                    session_id,
                    request.content,
                ):
                    await websocket.send_json(_event_payload(event))
        except WebSocketDisconnect:
            await container.close_session.execute(session_id)
        except DomainError as exc:
            await websocket.send_json({"type": "error", "detail": str(exc)})
        except Exception:
            await websocket.send_json(
                {"type": "error", "detail": "Internal chat processing error"}
            )
            raise

    app.include_router(router)
    frontend = resolved.project_root / "frontend"
    if frontend.is_dir():
        index_html = frontend.joinpath("index.html").read_text()
        app_javascript = frontend.joinpath("app.js").read_text()
        stylesheet = frontend.joinpath("styles.css").read_text()

        @app.get("/", include_in_schema=False)
        async def frontend_index() -> HTMLResponse:
            return HTMLResponse(index_html)

        @app.get("/app.js", include_in_schema=False)
        async def frontend_javascript() -> Response:
            return Response(app_javascript, media_type="text/javascript")

        @app.get("/styles.css", include_in_schema=False)
        async def frontend_stylesheet() -> Response:
            return Response(stylesheet, media_type="text/css")
    return app


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
    )
