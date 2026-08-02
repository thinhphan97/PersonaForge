import os
from dataclasses import dataclass
from pathlib import Path

from avatar_chat.domain.models import AvatarState


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    host: str
    port: int
    database_url: str | None
    reply_provider: str
    reply_chunk_delay_seconds: float
    thinking_min_duration_seconds: float
    reply_http_url: str | None
    reply_http_token: str | None
    ice_server_urls: tuple[str, ...]
    ice_username: str | None
    ice_credential: str | None
    av_sync_debug: bool
    av_sync_interval_seconds: float
    av_sync_pulse_duration_seconds: float
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
            reply_provider=os.getenv("REPLY_PROVIDER", "scripted"),
            reply_chunk_delay_seconds=float(os.getenv("REPLY_CHUNK_DELAY", "0.25")),
            thinking_min_duration_seconds=float(
                os.getenv("THINKING_MIN_DURATION", "1.0")
            ),
            reply_http_url=os.getenv("REPLY_HTTP_URL"),
            reply_http_token=os.getenv("REPLY_HTTP_TOKEN"),
            ice_server_urls=ice_urls,
            ice_username=os.getenv("WEBRTC_ICE_USERNAME"),
            ice_credential=os.getenv("WEBRTC_ICE_CREDENTIAL"),
            av_sync_debug=os.getenv("AV_SYNC_DEBUG", "true").lower()
            in {"1", "true", "yes", "on"},
            av_sync_interval_seconds=float(os.getenv("AV_SYNC_INTERVAL", "2.0")),
            av_sync_pulse_duration_seconds=float(
                os.getenv("AV_SYNC_PULSE_DURATION", "0.1")
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
