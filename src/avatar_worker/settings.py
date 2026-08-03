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
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    worker_id: str
    redis_url: str
    video_width: int
    video_height: int
    video_fps: float
    video_bitrate: int
    audio_sample_rate: int
    audio_frame_duration_seconds: float
    video_queue_size_ms: float
    av_sync_debug: bool
    av_sync_interval_seconds: float
    av_sync_pulse_duration_seconds: float
    avatar_clips: dict[AvatarState, Path]

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        default_assets = _project_root() / "assets" / "characters" / "default"
        return cls(
            worker_id=os.getenv("WORKER_ID", "avatar-worker-1"),
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            video_width=int(os.getenv("WORKER_VIDEO_WIDTH", "640")),
            video_height=int(os.getenv("WORKER_VIDEO_HEIGHT", "360")),
            video_fps=float(os.getenv("WORKER_VIDEO_FPS", "25")),
            video_bitrate=int(os.getenv("WORKER_VIDEO_BITRATE", "1500000")),
            audio_sample_rate=int(os.getenv("WORKER_AUDIO_SAMPLE_RATE", "48000")),
            audio_frame_duration_seconds=float(
                os.getenv("WORKER_AUDIO_FRAME_DURATION", "0.02")
            ),
            video_queue_size_ms=float(os.getenv("WORKER_VIDEO_QUEUE_MS", "80")),
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
