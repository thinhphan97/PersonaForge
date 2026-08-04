import os
from dataclasses import dataclass
from pathlib import Path

from avatar_chat.domain.models import AvatarState


def _project_root() -> Path:
    configured = os.getenv("PROJECT_ROOT")
    if configured:
        return Path(configured).resolve()
    working_directory = Path.cwd()
    if working_directory.joinpath("pyproject.toml").is_file():
        return working_directory
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    host: str
    port: int
    database_url: str | None
    auto_create_schema: bool
    database_pool_size: int
    database_max_overflow: int
    refresh_cookie_name: str
    auth_cookie_secure: bool
    jwt_secret: str
    jwt_issuer: str
    jwt_audience: str
    access_token_ttl_minutes: int
    refresh_token_ttl_days: int
    allowed_origins: tuple[str, ...]
    reply_provider: str
    reply_chunk_delay_seconds: float
    thinking_min_duration_seconds: float
    reply_http_url: str | None
    reply_http_token: str | None
    agent_runtime_provider: str
    agent_grpc_target: str
    agent_turn_timeout_seconds: float
    agent_idle_timeout_seconds: float
    agent_delegation_secret: str
    agent_delegation_issuer: str
    agent_delegation_audience: str
    session_turn_lock_timeout_seconds: float
    ice_server_urls: tuple[str, ...]
    ice_username: str | None
    ice_credential: str | None
    av_sync_debug: bool
    av_sync_interval_seconds: float
    av_sync_pulse_duration_seconds: float
    media_provider: str
    livekit_url: str | None
    livekit_api_url: str | None
    livekit_worker_url: str | None
    livekit_api_key: str | None
    livekit_api_secret: str | None
    redis_url: str
    realtime_event_bus: str
    render_worker_id: str
    characters_root: Path
    avatar_clips: dict[AvatarState, Path]

    @classmethod
    def from_env(cls) -> "Settings":
        root = _project_root()
        default_assets = root / "assets" / "characters" / "default"
        ice_urls = tuple(
            value.strip()
            for value in os.getenv("WEBRTC_ICE_SERVERS", "").split(",")
            if value.strip()
        )
        return cls(
            project_root=root,
            host=os.getenv("APP_HOST", "0.0.0.0"),
            port=int(os.getenv("APP_PORT", "8000")),
            database_url=os.getenv("DATABASE_URL") or None,
            auto_create_schema=os.getenv("AUTO_CREATE_SCHEMA", "true").lower()
            in {"1", "true", "yes", "on"},
            database_pool_size=int(os.getenv("DATABASE_POOL_SIZE", "20")),
            database_max_overflow=int(os.getenv("DATABASE_MAX_OVERFLOW", "10")),
            refresh_cookie_name=os.getenv("REFRESH_COOKIE_NAME", "avatar_refresh"),
            auth_cookie_secure=os.getenv("AUTH_COOKIE_SECURE", "false").lower()
            in {"1", "true", "yes", "on"},
            jwt_secret=os.getenv(
                "JWT_SECRET",
                "development-only-jwt-secret-change-me-32-bytes",
            ),
            jwt_issuer=os.getenv("JWT_ISSUER", "avatar-chat"),
            jwt_audience=os.getenv("JWT_AUDIENCE", "avatar-chat-browser"),
            access_token_ttl_minutes=int(os.getenv("ACCESS_TOKEN_TTL_MINUTES", "15")),
            refresh_token_ttl_days=int(os.getenv("REFRESH_TOKEN_TTL_DAYS", "7")),
            allowed_origins=tuple(
                origin.strip()
                for origin in os.getenv(
                    "ALLOWED_ORIGINS",
                    "http://localhost:8000,http://localhost:18000",
                ).split(",")
                if origin.strip()
            ),
            reply_provider=os.getenv("REPLY_PROVIDER", "scripted"),
            reply_chunk_delay_seconds=float(os.getenv("REPLY_CHUNK_DELAY", "0.25")),
            thinking_min_duration_seconds=float(
                os.getenv("THINKING_MIN_DURATION", "1.0")
            ),
            reply_http_url=os.getenv("REPLY_HTTP_URL"),
            reply_http_token=os.getenv("REPLY_HTTP_TOKEN"),
            agent_runtime_provider=os.getenv("AGENT_RUNTIME_PROVIDER", "local").lower(),
            agent_grpc_target=os.getenv("AGENT_GRPC_TARGET", "personaforge-agent:50051"),
            agent_turn_timeout_seconds=float(os.getenv("AGENT_TURN_TIMEOUT", "120")),
            agent_idle_timeout_seconds=float(os.getenv("AGENT_IDLE_TIMEOUT", "15")),
            agent_delegation_secret=os.getenv(
                "AGENT_DELEGATION_SECRET",
                "development-agent-delegation-secret-change-me",
            ),
            agent_delegation_issuer=os.getenv(
                "AGENT_DELEGATION_ISSUER", "personaforge-api"
            ),
            agent_delegation_audience=os.getenv(
                "AGENT_DELEGATION_AUDIENCE", "personaforge-agent"
            ),
            session_turn_lock_timeout_seconds=float(
                os.getenv("SESSION_TURN_LOCK_TIMEOUT", "0")
            ),
            ice_server_urls=ice_urls,
            ice_username=os.getenv("WEBRTC_ICE_USERNAME"),
            ice_credential=os.getenv("WEBRTC_ICE_CREDENTIAL"),
            av_sync_debug=os.getenv("AV_SYNC_DEBUG", "true").lower()
            in {"1", "true", "yes", "on"},
            av_sync_interval_seconds=float(os.getenv("AV_SYNC_INTERVAL", "2.0")),
            av_sync_pulse_duration_seconds=float(
                os.getenv("AV_SYNC_PULSE_DURATION", "0.1")
            ),
            media_provider=os.getenv("MEDIA_PROVIDER", "aiortc").lower(),
            livekit_url=os.getenv("LIVEKIT_URL") or None,
            livekit_api_url=os.getenv("LIVEKIT_API_URL") or None,
            livekit_worker_url=os.getenv("LIVEKIT_WORKER_URL") or None,
            livekit_api_key=os.getenv("LIVEKIT_API_KEY") or None,
            livekit_api_secret=os.getenv("LIVEKIT_API_SECRET") or None,
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            realtime_event_bus=os.getenv("REALTIME_EVENT_BUS", "auto").lower(),
            render_worker_id=os.getenv("RENDER_WORKER_ID", "avatar-worker-1"),
            characters_root=Path(
                os.getenv("AVATAR_CHARACTERS_ROOT", root / "assets" / "characters")
            ),
            avatar_clips={
                AvatarState.IDLE: Path(
                    os.getenv("AVATAR_IDLE_VIDEO", default_assets / "idle.mp4")
                ),
                AvatarState.THINKING: Path(
                    os.getenv("AVATAR_THINKING_VIDEO", default_assets / "thinking.mp4")
                ),
                AvatarState.SPEAKING: Path(
                    os.getenv("AVATAR_SPEAKING_VIDEO", default_assets / "speaking.mp4")
                ),
            },
        )
